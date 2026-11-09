"""决策循环：模型选一个动作 → 代码校验/执行/记账 → 观察回模型 → … 直到 finish 被接受或到上限。

唯一决策者是模型。代码在这里只做：消息配对、步数上限、重复动作提醒、把观察原样回传。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from .llm import LLMClient
from .plan import MAX_ATTEMPTS_PER_STEP, CostTable
from .schema import Request, tool_schemas
from .tools import Tools
from .trace import Trace, summarize

DEFAULT_MAX_STEPS = 15


@dataclass
class LoopResult:
    status: str  # completed | incomplete | failed
    reason: str  # finish_accepted | step_limit_reached | llm_rounds_limit | model_no_tool_call
    steps_used: int
    llm_rounds: int
    finish_args: Optional[dict[str, Any]] = None
    usage: dict[str, int] = field(default_factory=dict)


def build_system_prompt(req: Request, table: CostTable, max_steps: int) -> str:
    pro_c, pro_h = table.cost_hours("generate_script", "pro")
    lite_c, lite_h = table.cost_hours("generate_script", "lite")
    s_c, s_h = table.cost_hours("search_materials", None)
    return f"""你是内容活动方案的任务规划 Agent，帮运营人员在预算与截止时间内做出一份内容活动方案（素材引用 + 脚本），并保存。

## 你能做的动作（每轮只调用一个工具）
propose_plan（提交初始计划，只能一次）→ estimate_cost（估算）→ search_materials / generate_script（逐步执行）→ save_plan（保存）→ finish（结束）。
计划不合适时用 revise_plan 修改，改完必须重新 estimate_cost。

## 费用与工时表（模拟单位；由系统计费，你不要自己算总账）
- search_materials：费用 {s_c:g}，工时 {s_h:g}h
- generate_script pro：费用 {pro_c:g}，工时 {pro_h:g}h（完整脚本：钩子、≥3 分镜、行动号召）
- generate_script lite：费用 {lite_c:g}，工时 {lite_h:g}h（简版：标题 + 提纲）
- 计划 / 估算 / 保存 / 结束：免费
- 失败的尝试不计费。步骤按依赖顺序串行执行，工时累加推进模拟时钟。

## 本次任务
- 主题：{req.topic}
- 预算：{req.budget:g}　截止时间：{req.deadline.isoformat()}　当前时间：{req.now.isoformat()}
- 质量偏好：{req.quality_preference}（high 倾向 pro 档并可加预热等可选脚本；low 倾向 lite；这是软偏好，预算与截止时间优先）
- 必需交付物：恰好一个 deliverable.key="main" 且 required=true 的主脚本，它必须依赖至少一个搜索步骤。其他脚本（如 teaser 预热）是可选的，required=false，可以删除。
- 动作上限：{max_steps} 次（包括 finish 与被拒绝的调用）。

## 硬约束（违反会被系统拒绝，拒绝不计费）
1. 费用与时间只以 estimate_cost 的返回为准；执行计费动作前，当前计划版本必须已有估算，且估算显示在预算与截止时间内。
2. 步骤必须在计划里、依赖已完成才能执行；已完成的步骤不要重复执行（重复调用只会返回已有结果）。
3. generate_script 的 material_ids 必须来自该步骤所依赖搜索步骤的返回结果；档位取自计划，想换档位用 revise_plan。
4. 同一步骤最多尝试 {MAX_ATTEMPTS_PER_STEP} 次；之后只能改计划或停止。
5. 必须先 save_plan 成功，才能 finish(status="completed")。保存前要重新 estimate_cost（剩余为 0）。
6. 每个动作都要给一句简短的 reason（决策依据）。

## 决策指引（这些是选项，不是规则；由你根据观察判断）
- 估算超预算时，你可以：把可选脚本改为 lite、删除可选脚本、合并搜索步骤，然后重新估算；若返回的 min_feasible_cost 仍高于预算，说明没有可行方案，应以 finish(status="incomplete") 结束并在 unfinished_reason 写明缺口。
- 估算超截止时间时同理：缩短工时（换 lite、删可选步骤）或停止。
- 某步失败时，你可以：重试、换档位后重试、删除该可选步骤，或停止；已成功的步骤及其费用会保留。
- 搜索无命中时，你可以换检索词再搜（会再次计费），或停止。
- 不要编造素材或结果。

## 结束要求
finish(status="completed") 只在方案已保存后使用。无法完成时用 finish(status="incomplete" 或 "failed")，summary 写给运营看的一句话，unfinished_reason 写具体原因。"""


def run_loop(
    llm: LLMClient,
    tools: Tools,
    trace: Trace,
    req: Request,
    max_steps: int = DEFAULT_MAX_STEPS,
    extra_user_context: Optional[str] = None,
) -> LoopResult:
    schemas = tool_schemas()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(req, tools.table, max_steps)},
        {
            "role": "user",
            "content": "请开始。原始请求：\n" + json.dumps(
                {"topic": req.topic, "budget": req.budget, "deadline": req.deadline.isoformat(),
                 "now": req.now.isoformat(), "quality_preference": req.quality_preference},
                ensure_ascii=False,
            ) + (f"\n\n{extra_user_context}" if extra_user_context else ""),
        },
    ]
    steps_used = 0
    rounds = 0
    no_tool_streak = 0
    prev_signature: Optional[str] = None
    usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    while True:
        if steps_used >= max_steps:
            trace.event("limit_reached", reason="step_limit_reached", steps_used=steps_used, max_steps=max_steps)
            return LoopResult("incomplete", "step_limit_reached", steps_used, rounds, usage=usage)
        if rounds >= 2 * max_steps:
            trace.event("limit_reached", reason="llm_rounds_limit", steps_used=steps_used, llm_rounds=rounds)
            return LoopResult("incomplete", "llm_rounds_limit", steps_used, rounds, usage=usage)

        try:
            reply = llm.chat(messages, schemas)
        except Exception as e:  # 模型接口异常（超时、鉴权、网络）：如实记录并以 incomplete 结束，不伪造结果
            trace.event("llm_error", error=type(e).__name__, message=str(e)[:300], llm_round=rounds + 1)
            return LoopResult("incomplete", "llm_error", steps_used, rounds, usage=usage)
        rounds += 1
        for k, v in reply.usage.items():
            usage[k] = usage.get(k, 0) + v
        messages.append(reply.assistant_message)

        if not reply.tool_calls:
            no_tool_streak += 1
            trace.event("model_text", content=(reply.content or "")[:500], llm_round=rounds)
            if no_tool_streak >= 3:
                return LoopResult("incomplete", "model_no_tool_call", steps_used, rounds, usage=usage)
            messages.append({"role": "user", "content": "请调用一个工具继续；若任务无法完成，请调用 finish 并说明原因。"})
            continue
        no_tool_streak = 0

        call, extra = reply.tool_calls[0], reply.tool_calls[1:]
        steps_used += 1
        args = call.arguments()
        reason = args.get("reason") if isinstance(args, dict) else None
        trace.event("model_action", step_no=steps_used, tool=call.name, args=args if args is not None else {"_raw": call.arguments_json},
                    reason=reason, llm_round=rounds, latency_ms=reply.latency_ms)

        if args is None:
            obs: dict[str, Any] = {"ok": False, "error": "invalid_args", "retryable": False, "message": "工具参数不是合法 JSON 对象"}
        else:
            obs = tools.execute(call.name, args)

        signature = json.dumps({"tool": call.name, "args": {k: v for k, v in (args or {}).items() if k != "reason"}}, sort_keys=True, ensure_ascii=False)
        if signature == prev_signature:
            obs["warning"] = "你连续两次发出了相同的动作与参数；请根据上一条观察调整，不要原样重复。"
        prev_signature = signature
        if extra:
            obs["note"] = f"每轮只接受一个动作，另外 {len(extra)} 个调用已忽略。"
        if steps_used == max_steps - 1:
            obs["warning_steps"] = "仅剩 1 次动作额度；如无法完成，请用 finish(status=\"incomplete\") 结束并说明原因。"

        trace.event("tool_result", step_no=steps_used, tool=call.name, ok=bool(obs.get("ok")), summary=summarize(obs))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(obs, ensure_ascii=False)})
        for tc in extra:
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": json.dumps({"ok": False, "error": "ignored", "message": "每轮只接受一个动作，此调用被忽略"}, ensure_ascii=False)})

        if call.name == "finish" and obs.get("ok") and obs.get("accepted"):
            trace.event("finish_accepted", step_no=steps_used, status=obs["status"])
            return LoopResult(obs["status"], "finish_accepted", steps_used, rounds, finish_args=args, usage=usage)
        if call.name == "finish" and not obs.get("ok"):
            trace.event("finish_rejected", step_no=steps_used, reasons=obs.get("reasons") or [obs.get("message")])
