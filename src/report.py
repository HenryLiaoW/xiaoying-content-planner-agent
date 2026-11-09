"""从库组装终报：金额、状态、保存结果全部取自 SQLite；模型只提供 status 意图与 summary。

同时生成"调整说明"（adjustments）与面向运营的一句话结论（user_message）。模型不写任何数字。
"""
from __future__ import annotations

from datetime import datetime
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
            }.get(result.reason, result.reason)

    adjustments = list(tools.adjustments)
    if saved and saved["adjustments_json"]:
        import json

        adjustments = json.loads(saved["adjustments_json"]) or adjustments

    user_message = _user_message(status, result.reason, req, plan, est, saved, spent, sim_now, gap, adjustments, unfinished_reason, max_steps)

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
        },
        "adjustments": adjustments,
        "user_message": user_message,
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
                msg += f" 注意：原方案预计 {_fmt_dt(a['before']['projected_finish'])} 完成，晚于截止时间，已调整为 {detail}——这是{why}下的调整结果。"
        other = [a for a in adjustments if a["trigger"] == "tool_failure"]
        if other:
            msg += f" 执行中有步骤失败，已改计划继续：{'、'.join(other[0].get('changes_plain') or other[0]['changes'])}。"
        return msg

    done_steps = [s for s in (plan.steps if plan else []) if s.status == "done"]
    kept = f"已完成的 {len(done_steps)} 个步骤（{', '.join(s.id for s in done_steps)}）及其结果已保留" if done_steps else "尚无已完成步骤"
    if gap is not None and est is not None:
        return (
            f"未能生成方案：最低交付（一次素材搜索 + 一份简版主脚本）至少需要 {_g(est.min_feasible_cost)}，"
            f"预算为 {_g(req.budget)}，缺口 {_g(gap)}。可将预算提高到 {_g(est.min_feasible_cost)} 后重新提交。"
        )
    if est is not None and not est.within_deadline and status != "completed" and reason == "finish_accepted" and est.min_feasible_hours > 0:
        return (
            f"未能生成方案：最低交付至少需要 {_g(est.min_feasible_hours)} 小时，"
            f"截止时间 {_fmt_dt(req.deadline)} 来不及。可放宽截止时间后重新提交。{kept}。"
        )
    if reason == "step_limit_reached":
        return f"已用完 {max_steps} 次动作额度，任务未完成；{kept}，可用 --resume 继续。"
    if status == "failed":
        failed = [s.id for s in (plan.steps if plan else []) if s.status == "failed"]
        where = f"步骤 {', '.join(failed)} " if failed else ""
        return f"任务失败：{where}{unfinished_reason or ''}。{kept}，可用 --resume 重试。"
    return f"任务未完成：{unfinished_reason or reason}。{kept}，可用 --resume 继续。已花费 {_g(spent)} / 预算 {_g(req.budget)}。"
