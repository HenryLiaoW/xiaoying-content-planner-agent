"""离线 Mock 脚本：每个场景一份"模型会怎么做"的剧本，带 expect_prev 核对收到的观察。

只验证流程与状态机；不代表真实模型的决策效果。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from .llm import MockStep

Obs = Optional[dict[str, Any]]
Hist = list[dict[str, Any]]


def _ids_from_search(history: Hist, n: int = 3) -> list[str]:
    """从历史观察里找最近一次有命中的搜索结果，取前 n 个素材 id。"""
    for obs in reversed(history):
        if obs.get("ok") and "items" in obs and obs.get("count", 0) > 0:
            return [m["id"] for m in obs["items"][:n]]
    return []


def _gen(step_id: str, brief: str, reason: str, expect: Optional[dict[str, Any]] = None, n: int = 3) -> MockStep:
    def step(prev: Obs, history: Hist) -> tuple[str, dict[str, Any]]:
        if expect is not None and prev is not None:
            for k, v in expect.items():
                if prev.get(k) != v:
                    from .llm import MockMismatch

                    raise MockMismatch(f"generate_script({step_id}) 期望上一条观察 {k}={v!r}，实际 {prev.get(k)!r}（{prev.get('error')} {str(prev.get('message',''))[:80]}）")
        return "generate_script", {"step_id": step_id, "material_ids": _ids_from_search(history, n), "brief": brief, "reason": reason}

    return step


def _plan_basic(main_tier: str = "pro", teaser: bool = True) -> list[dict[str, Any]]:
    steps = [
        {"id": "s1", "action": "search_materials", "query": "Vlog 模板 双11"},
        {"id": "s2", "action": "generate_script", "tier": main_tier, "brief": "主视频脚本", "depends_on": ["s1"],
         "deliverable": {"key": "main", "required": True}},
    ]
    if teaser:
        steps.append({"id": "s3", "action": "generate_script", "tier": "lite", "brief": "预热短片", "depends_on": ["s1"],
                      "deliverable": {"key": "teaser", "required": False}})
    return steps


def _tail(expect_after_last_exec: Optional[dict[str, Any]] = None) -> list[MockStep]:
    return [
        {"tool": "estimate_cost", "args": {"reason": "保存前复核剩余费用为 0"}, "expect_prev": expect_after_last_exec or {"ok": True}},
        {"tool": "save_plan", "args": {"reason": "全部步骤完成且在预算内"}, "expect_prev": {"ok": True, "remaining_estimate": 0.0}},
        {"tool": "finish", "args": {"status": "completed", "summary": "方案已保存", "reason": "保存成功"}, "expect_prev": {"ok": True, "saved": "created"}},
    ]


def normal() -> list[MockStep]:
    return [
        {"tool": "propose_plan", "args": {"steps": _plan_basic("pro", teaser=True), "reason": "一次搜索 + 主视频 pro + 预热 lite，预算充足"}},
        {"tool": "estimate_cost", "args": {"reason": "执行前估算"}, "expect_prev": {"ok": True}},
        {"tool": "search_materials", "args": {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "先拿素材"}, "expect_prev": {"ok": True, "within_budget": True}},
        _gen("s2", "主视频脚本", "依赖已完成，生成主脚本", expect={"ok": True}),
        _gen("s3", "预热短片", "生成预热脚本", expect={"ok": True}),
        *_tail(),
    ]


def tool_failure_once() -> list[MockStep]:
    return [
        {"tool": "propose_plan", "args": {"steps": _plan_basic("pro", teaser=True), "reason": "标准计划"}},
        {"tool": "estimate_cost", "args": {"reason": "执行前估算"}, "expect_prev": {"ok": True}},
        {"tool": "search_materials", "args": {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "先拿素材"}, "expect_prev": {"ok": True, "within_budget": True}},
        _gen("s2", "主视频脚本", "生成主脚本", expect={"ok": True}),
        _gen("s2", "主视频脚本", "脚本服务报错且可重试，重试一次", expect={"ok": False, "error": "tool_error", "retryable": True}),
        _gen("s3", "预热短片", "主脚本成功，继续预热", expect={"ok": True, "status": "done"}),
        *_tail(),
    ]


def tool_failure_persistent() -> list[MockStep]:
    return [
        {"tool": "propose_plan", "args": {"steps": _plan_basic("pro", teaser=False), "reason": "只做主脚本"}},
        {"tool": "estimate_cost", "args": {"reason": "执行前估算"}, "expect_prev": {"ok": True}},
        {"tool": "search_materials", "args": {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "先拿素材"}, "expect_prev": {"ok": True, "within_budget": True}},
        _gen("s2", "主视频脚本", "生成主脚本", expect={"ok": True}),
        _gen("s2", "主视频脚本", "重试一次", expect={"ok": False, "error": "tool_error", "retryable": True}),
        _gen("s2", "主视频脚本", "再试", expect={"ok": False, "error": "tool_error", "retryable": False}),
        {"tool": "finish", "args": {"status": "failed", "summary": "脚本服务持续不可用，任务失败", "unfinished_reason": "主脚本 s2 连续 2 次失败并达到重试上限；搜索结果已保留", "reason": "重试上限已到，停止"},
         "expect_prev": {"ok": False, "error": "retry_limit_reached"}},
    ]


def over_budget() -> list[MockStep]:
    """复刻参考实例：首版 12（2 搜索 + 2 pro）→ 超预算 → 改为 8（1 搜索 + pro + lite）。"""
    first = [
        {"id": "s1", "action": "search_materials", "query": "Vlog 模板 双11"},
        {"id": "s2", "action": "search_materials", "query": "双11 促销 BGM"},
        {"id": "s3", "action": "generate_script", "tier": "pro", "brief": "主视频脚本", "depends_on": ["s1", "s2"], "deliverable": {"key": "main", "required": True}},
        {"id": "s4", "action": "generate_script", "tier": "pro", "brief": "预热短片", "depends_on": ["s2"], "deliverable": {"key": "teaser", "required": False}},
    ]
    return [
        {"tool": "propose_plan", "args": {"steps": first, "reason": "质量偏好 high：两次搜索 + 两份 pro 脚本"}},
        {"tool": "estimate_cost", "args": {"reason": "执行前估算"}, "expect_prev": {"ok": True}},
        {"tool": "revise_plan", "args": {"set_tier": [{"step_id": "s4", "tier": "lite"}], "set_depends": [{"step_id": "s4", "depends_on": ["s1"]}], "remove": ["s2"],
                                         "reason": "预计 12 超预算 2：删掉第二次搜索，预热脚本降为 lite"},
         "expect_prev": {"ok": True, "within_budget": False, "over_budget_by": 2.0}},
        {"tool": "estimate_cost", "args": {"reason": "重规划后重新估算"}, "expect_prev": {"ok": True, "revision": 2}},
        {"tool": "search_materials", "args": {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "估算 8 在预算内，开始执行"}, "expect_prev": {"ok": True, "within_budget": True, "projected_total": 8.0}},
        _gen("s3", "主视频脚本", "生成主脚本", expect={"ok": True}),
        _gen("s4", "预热短片", "生成预热脚本", expect={"ok": True}),
        *_tail(),
    ]


def infeasible() -> list[MockStep]:
    return [
        {"tool": "propose_plan", "args": {"steps": _plan_basic("lite", teaser=False), "reason": "预算很少，直接用最省配置"}},
        {"tool": "estimate_cost", "args": {"reason": "执行前估算"}, "expect_prev": {"ok": True}},
        {"tool": "finish", "args": {"status": "incomplete", "summary": "预算不足以完成最低交付", "unfinished_reason": "最低可行费用 3 高于预算 2，缺口 1，没有可行方案", "reason": "min_feasible_cost 已超预算，继续调整无意义"},
         "expect_prev": {"ok": True, "within_budget": False, "min_feasible_cost": 3.0}},
    ]


def no_materials() -> list[MockStep]:
    return [
        {"tool": "propose_plan", "args": {"steps": [
            {"id": "s1", "action": "search_materials", "query": "水下 摄影"},
            {"id": "s2", "action": "generate_script", "tier": "lite", "brief": "主视频脚本", "depends_on": ["s1"], "deliverable": {"key": "main", "required": True}},
        ], "reason": "按主题检索"}},
        {"tool": "estimate_cost", "args": {"reason": "执行前估算"}, "expect_prev": {"ok": True}},
        {"tool": "search_materials", "args": {"step_id": "s1", "query": "水下 摄影", "reason": "先拿素材"}, "expect_prev": {"ok": True, "within_budget": True}},
        {"tool": "revise_plan", "args": {"add": [{"id": "s1b", "action": "search_materials", "query": "教程 新手 模板"}], "set_depends": [{"step_id": "s2", "depends_on": ["s1", "s1b"]}],
                                         "reason": "首次检索无命中，换更通用的检索词补一次搜索"},
         "expect_prev": {"ok": True, "count": 0}},
        {"tool": "estimate_cost", "args": {"reason": "改计划后重新估算"}, "expect_prev": {"ok": True, "revision": 2}},
        {"tool": "search_materials", "args": {"step_id": "s1b", "query": "教程 新手 模板", "reason": "补搜"}, "expect_prev": {"ok": True, "within_budget": True}},
        _gen("s2", "主视频脚本", "有素材了，生成主脚本", expect={"ok": True}),
        *_tail(),
    ]


def deadline_tight() -> list[MockStep]:
    return [
        {"tool": "propose_plan", "args": {"steps": _plan_basic("pro", teaser=False), "reason": "主脚本 pro"}},
        {"tool": "estimate_cost", "args": {"reason": "执行前估算"}, "expect_prev": {"ok": True}},
        {"tool": "revise_plan", "args": {"set_tier": [{"step_id": "s2", "tier": "lite"}], "reason": "pro 需 2.5h 超过截止，改 lite（1.5h）"},
         "expect_prev": {"ok": True, "within_deadline": False}},
        {"tool": "estimate_cost", "args": {"reason": "改档后重新估算"}, "expect_prev": {"ok": True, "revision": 2}},
        {"tool": "search_materials", "args": {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "在时限内，开始执行"}, "expect_prev": {"ok": True, "within_deadline": True}},
        _gen("s2", "主视频脚本", "生成主脚本", expect={"ok": True}),
        *_tail(),
    ]


SCRIPTS: dict[str, Callable[[], list[MockStep]]] = {
    "normal": normal,
    "tool_failure_once": tool_failure_once,
    "tool_failure_persistent": tool_failure_persistent,
    "over_budget": over_budget,
    "infeasible": infeasible,
    "no_materials": no_materials,
    "deadline_tight": deadline_tight,
}


def get_script(name: str) -> list[MockStep]:
    if name not in SCRIPTS:
        raise KeyError(f"未知的 mock 脚本 {name!r}；可选：{sorted(SCRIPTS)}")
    return SCRIPTS[name]()
