"""决策循环：模型选一个动作 → 代码校验/执行/记账 → 观察回模型 → … 直到 finish 被接受或到上限。

唯一决策者是模型。代码在这里只做：消息配对、步数上限、重复动作提醒、把观察原样回传。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
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


PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


def load_prompt_template(name: str = "system.md") -> Template:
    """提示词模板独立存放在 src/prompts/，用 string.Template 的 $变量 占位；改措辞不需要改代码。"""
    return Template((PROMPT_DIR / name).read_text(encoding="utf-8"))


def build_system_prompt(req: Request, table: CostTable, max_steps: int, template: Optional[Template] = None) -> str:
    pro_c, pro_h = table.cost_hours("generate_script", "pro")
    lite_c, lite_h = table.cost_hours("generate_script", "lite")
    s_c, s_h = table.cost_hours("search_materials", None)
    tpl = template or load_prompt_template()
    return tpl.substitute(
        search_cost=f"{s_c:g}", search_hours=f"{s_h:g}",
        pro_cost=f"{pro_c:g}", pro_hours=f"{pro_h:g}",
        lite_cost=f"{lite_c:g}", lite_hours=f"{lite_h:g}",
        topic=req.topic, budget=f"{req.budget:g}",
        deadline=req.deadline.isoformat(), now=req.now.isoformat(),
        quality=req.quality_preference,
        max_steps=max_steps, max_attempts=MAX_ATTEMPTS_PER_STEP,
    )


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
        except AssertionError:  # MockMismatch：脚本与实际观察不符，测试应当失败
            raise
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
