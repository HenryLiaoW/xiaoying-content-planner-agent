"""从库组装终报：金额、状态、保存结果全部取自 SQLite；模型只提供 status 意图与 summary。

同时生成"调整说明"（adjustments）与面向运营的一句话结论（user_message）。模型不写任何数字。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from .loop import LoopResult
from .schema import Request
from .store import Store
from .tools import Tools
from .trace import Trace


def _fmt_dt(dt: datetime | str) -> str:
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    return dt.strftime("%m-%d %H:%M")


def _g(x: float) -> str:
    return f"{x:g}"


def build_report(
    store: Store,
    run_id: str,
    req: Request,
    fp: str,
    tools: Tools,
    result: LoopResult,
    trace: Trace,
    mode: str,
    model_info: dict[str, Any],
    max_steps: int,
    served_from: str,
) -> dict[str, Any]:
    run = store.get_run(run_id)
    plan = store.load_plan(run_id)
    est = store.latest_estimate(run_id)
    saved = store.get_saved_plan(run_id)
    spent = store.ledger_sum(run_id)
    sim_now = datetime.fromisoformat(run["sim_now"])
    status = result.status

    # 计划与产物
    plan_view: Optional[dict[str, Any]] = None
    artifacts: dict[str, Any] = {}
    if plan:
        plan_view = tools.plan_view()
        for s in plan.steps:
            if s.status == "done":
                r = store.step_result(run_id, s.id) or {}
                if s.action == "search_materials":
                    artifacts[s.id] = {"type": "materials", "query": r.get("query"), "items": r.get("items", [])}
                else:
                    artifacts[s.id] = {"type": "script", "deliverable": r.get("deliverable"), "script": r.get("script")}

    remaining = sum(s.cost for s in plan.steps if s.status not in ("done", "skipped")) if plan else 0.0
    gap: Optional[float] = None
    if status != "completed" and est and est.min_feasible_cost > req.budget + 1e-9:
        gap = round(est.min_feasible_cost - req.budget, 4)

    unfinished_reason: Optional[str] = None
    if status != "completed":
        if result.finish_args and result.finish_args.get("unfinished_reason"):
            unfinished_reason = result.finish_args["unfinished_reason"]
        else:
            unfinished_reason = {
                "step_limit_reached": f"达到动作上限 {max_steps}，任务未完成",
                "llm_rounds_limit": "模型交互轮次达到上限，任务未完成",
                "model_no_tool_call": "模型连续未发出工具调用，任务未完成",
                "llm_error": "模型接口调用失败（见 trace 中的 llm_error 事件），任务未完成",
            }.get(result.reason, result.reason)

    adjustments = list(tools.adjustments)  # 含历次启动（runs.adjustments_json），保存时快照进 saved_plans

    timing = _timing(tools._estimate_now() if (plan and status != "completed") else None, req.deadline)
    user_message = _user_message(status, result.reason, req, plan, est, saved, spent, sim_now, gap, adjustments, unfinished_reason, max_steps, timing)
    handoff_note = _handoff_note(status, result.reason, run_id, req, run["attempt_no"], plan, spent, unfinished_reason, bool(result.finish_args), max_steps)

    report: dict[str, Any] = {
        "run_id": run_id,
        "request_id": req.request_id,
        "fingerprint": fp,
        "attempt_no": run["attempt_no"],
        "served_from": served_from,
        "mode": mode,
        **model_info,
        "status": status,
        "finish_reason": result.reason,
        "steps_used": result.steps_used,
        "max_steps": max_steps,
        "llm_rounds": result.llm_rounds,
        "business_exec_count": tools.business_exec_count,
        "result": {
            "plan": plan_view,
            "artifacts": artifacts,
            "spent_cost": spent,
            "remaining_estimate": remaining,
            "projected_total": spent + remaining,
            "budget": req.budget,
            "sim_finish": sim_now.isoformat(),
            "deadline": req.deadline.isoformat(),
            "within_deadline": sim_now <= req.deadline,
            "saved_plan": {"revision": saved["revision"], "saved_at": saved["saved_at"]} if saved else None,
            "unfinished_reason": unfinished_reason,
            "gap": gap,
            "time_gap_hours": timing["gap_hours"] if timing else None,
        },
        "adjustments": adjustments,
        "user_message": user_message,
        "handoff_note": handoff_note,
        "model_summary": (result.finish_args or {}).get("summary"),
        "llm_usage": result.usage,
        "trace_review": trace.review(),
    }
    if mode == "offline_mock":
        report["note"] = "离线 Mock 仅验证流程与状态机，不代表真实模型的决策效果"
    return report


def _user_message(
    status: str,
    reason: str,
    req: Request,
    plan: Any,
    est: Any,
    saved: Any,
    spent: float,
    sim_now: datetime,
    gap: Optional[float],
    adjustments: list[dict[str, Any]],
    unfinished_reason: Optional[str],
    max_steps: int,
    timing: Optional[dict[str, Any]] = None,
) -> str:
    if status == "completed":
        msg = f"方案已保存，费用 {_g(spent)} / 预算 {_g(req.budget)}，预计 {_fmt_dt(sim_now)} 完成。"
        constraint_adj = [a for a in adjustments if a["trigger"] in ("over_budget", "over_deadline")]
        if constraint_adj:
            a = constraint_adj[0]
            why = "预算不足" if a["trigger"] == "over_budget" else "时间不足"
            before, after = a["before"]["projected_total"], a["after"]["projected_total"]
            detail = "、".join(a.get("changes_plain") or a["changes"]) if a["changes"] else "调整了计划"
            if a["trigger"] == "over_budget":
                msg += f" 注意：原方案预计 {_g(before)}，超出预算 {_g(before - req.budget)}，已调整为 {detail}，预计 {_g(after)}——这是{why}下的调整结果。"
            else:
                msg += f" 注意：原方案预计 {_fmt_dt(a['before']['projected_finish'])} 完成，晚于截止时间 {_fmt_dt(req.deadline)}，已调整为 {detail}——这是{why}下的调整结果。"
        other = [a for a in adjustments if a["trigger"] == "tool_failure"]
        if other:
            msg += f" 执行中有步骤失败，已改计划继续：{'、'.join(other[0].get('changes_plain') or other[0]['changes'])}。"
        tradeoff = [a for a in adjustments if a["trigger"] == "preference_tradeoff"]
        if tradeoff and not constraint_adj:
            t = tradeoff[0]
            msg += f" 注意：质量偏好为 high，但{'、'.join(t.get('changes_plain') or t['changes'])}。"
        return msg

    done_steps = [s for s in (plan.steps if plan else []) if s.status == "done"]
    kept = f"已完成的 {len(done_steps)} 个步骤（{'、'.join(_step_name(s) for s in done_steps)}）及其结果已保留" if done_steps else "尚无已完成步骤"
    if gap is not None and est is not None:
        return (
            f"未能生成方案：最低交付（一次素材搜索 + 一份简版主脚本）至少需要 {_g(est.min_feasible_cost)}，"
            f"预算为 {_g(req.budget)}，缺口 {_g(gap)}。可将预算提高到 {_g(est.min_feasible_cost)} 后重新提交。"
        )
    if timing and timing["gap_hours"] is not None and status == "incomplete" and reason == "finish_accepted":
        # 只有“最省时的做法也赶不上”才告诉运营来不及；当前计划超时但还有更省时的做法时，不下这个结论
        return (
            f"未能生成方案：按最省时的做法（必要的素材搜索 + 简版主脚本）还需要 {_g(timing['min_hours'])} 小时，"
            f"最早 {_fmt_dt(timing['min_finish'])} 完成，晚于截止时间 {_fmt_dt(req.deadline)}（差 {_g(timing['gap_hours'])} 小时）。"
            f"可把截止时间放宽到 {_fmt_dt(timing['min_finish'])} 之后重新提交。{kept}。"
        )
    if reason == "step_limit_reached":
        return f"已用完 {max_steps} 次动作额度，任务未完成；{kept}，可用 --resume 继续。{_HANDOFF_HINT}"
    if reason == "llm_error":
        return f"模型接口调用失败，任务未完成；{kept}，检查网络或密钥后可用 --resume 继续。{_HANDOFF_HINT}"
    if status == "failed":
        failed = [s for s in (plan.steps if plan else []) if s.status == "failed"]
        what = "；".join(f"{_step_name(s)}{_failure_count(s)}" for s in failed) or "执行失败"
        return (
            f"未能完成方案：{what}。{kept}，已花费 {_g(spent)} / 预算 {_g(req.budget)}。"
            f"你可以稍后用 --resume 重试（只会重做失败的步骤，不重复计费）；若仍失败，请把下面的“转交说明”发给相关服务的负责人。"
        )
    late = ""
    if timing and timing["plan_over"] and timing["gap_hours"] is None:
        late = f"按当前计划预计 {_fmt_dt(timing['plan_finish'])} 完成，晚于截止时间 {_fmt_dt(req.deadline)}"
        late += f"；换成更省时的做法最早可在 {_fmt_dt(timing['min_finish'])} 完成。" if timing["min_finish"] else "。"
    return f"任务未完成。{late}{kept}，可用 --resume 继续。已花费 {_g(spent)} / 预算 {_g(req.budget)}。"


def _timing(current: Any, deadline: datetime) -> Optional[dict[str, Any]]:
    """按当前状态判断时间：当前计划是否超时（plan_over），以及最省时的做法是否也来不及（gap_hours 非空才是真的来不及）。"""
    if current is None:
        return None
    min_finish = current.sim_now + timedelta(hours=current.min_feasible_hours) if current.min_feasible_hours > 0 else None
    gap_hours = None
    if min_finish is not None and min_finish > deadline:
        gap_hours = round((min_finish - deadline).total_seconds() / 3600, 4)
    return {
        "plan_over": not current.within_deadline,
        "plan_finish": current.projected_finish,
        "min_hours": current.min_feasible_hours,
        "min_finish": min_finish,
        "gap_hours": gap_hours,
    }


def _step_name(s: Any) -> str:
    """给运营看的步骤名：不出现内部编号。"""
    from .plan import _label

    if s.action == "generate_script":
        return f"{_label(s)}脚本生成"
    return f"素材搜索「{s.query}」" if s.query else "素材搜索"


def _failure_count(s: Any) -> str:
    """累计失败次数（含换档位之前的尝试），多档位时分开列出。"""
    from .plan import TIER_NAMES

    total = sum(s.failures.values()) or s.attempts
    if len(s.failures) > 1:
        parts = "、".join(f"{TIER_NAMES.get(k, k)} {v} 次" for k, v in s.failures.items())
        return f"累计失败 {total} 次（{parts}）"
    return f"累计失败 {total} 次"


_HANDOFF_HINT = "若多次续跑仍未完成，请把下面的“转交说明”发给负责人。"
_TOOL_OF = {"generate_script": "generate_script（脚本生成）", "search_materials": "search_materials（素材搜索）"}


def _handoff_note(
    status: str,
    reason: str,
    run_id: str,
    req: Request,
    attempt_no: int,
    plan: Any,
    spent: float,
    unfinished_reason: Optional[str],
    reason_from_model: bool,
    max_steps: int,
) -> Optional[str]:
    """转交说明：只在出问题时生成，给负责排查的人看，可直接转发；允许出现内部名称。

    内容由代码从数据库拼出；只有“模型说明”一行是模型原话，并明确标注。
    预算 / 时间不够属于运营自己能解决的情况，不生成。
    """
    if not (status == "failed" or reason in ("step_limit_reached", "llm_error", "llm_rounds_limit", "model_no_tool_call")):
        return None
    steps = plan.steps if plan else []
    lines = [f"【转交说明】任务 {run_id}（请求 {req.request_id or '无 request_id'}，第 {attempt_no} 次启动）"]
    problem = {
        "step_limit_reached": f"用完 {max_steps} 次动作额度仍未完成",
        "llm_error": "模型接口调用失败（详见 trace.jsonl 中的 llm_error 事件）",
        "llm_rounds_limit": "模型交互轮次达到上限",
        "model_no_tool_call": "模型连续未发出工具调用",
    }.get(reason, "模型判定无法继续并主动结束")
    lines.append(f"问题：{problem}。")
    for s in steps:
        if s.failures:
            total = sum(s.failures.values())
            detail = "、".join(f"{k} {v} 次" for k, v in s.failures.items())
            need = "必需交付物" if (s.deliverable and s.deliverable.required) else "可选"
            state = "未完成" if s.status != "done" else "最终成功"
            lines.append(f"失败记录：工具 {_TOOL_OF.get(s.action, s.action)} 在步骤 {s.id}（{_step_name(s)}，{need}）累计失败 {total} 次（{detail}），{state}。")
    done = [s for s in steps if s.status == "done"]
    lines.append(f"已完成：{'、'.join(f'{s.id} {_step_name(s)}' for s in done) if done else '无'}；已花费 {_g(spent)} / 预算 {_g(req.budget)}。")
    if reason_from_model and unfinished_reason:
        lines.append(f"模型说明（模型原话，未经核对）：{unfinished_reason}")
    lines.append("恢复方式：问题排除后，用同一输入加 --resume 续跑；只重做未完成的步骤，不重复计费。")
    return "\n".join(lines)
