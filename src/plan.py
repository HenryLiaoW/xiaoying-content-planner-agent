"""计划校验、修订与估算。全部是确定性计算：模型只提交结构，数字由这里算。

口径（docs/02-solution.md §费用）：
  spent     = 已成功执行且计费的步骤费用之和（账本）
  remaining = 当前计划中未完成（pending/failed）步骤的预计费用
  projected = spent + remaining
  min_feasible = 满足必需交付的最低总费用（已花费 + 还缺的最便宜配置）
"""
from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from .schema import Estimate, Plan, RevisePlanArgs, Step, StepSpec

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
MAX_ATTEMPTS_PER_STEP = 2


class PlanError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class CostTable:
    def __init__(self, path: Optional[Path] = None):
        p = path or DATA_DIR / "cost_table.json"
        self.raw: dict[str, Any] = json.loads(Path(p).read_text(encoding="utf-8"))
        self.version: str = self.raw["version"]
        self.actions: dict[str, Any] = self.raw["actions"]

    def cost_hours(self, action: str, tier: Optional[str]) -> tuple[float, float]:
        entry = self.actions[action]
        if action == "generate_script":
            if tier not in entry:
                raise PlanError("invalid_tier", f"generate_script 档位必须是 {sorted(entry)}，收到 {tier!r}")
            entry = entry[tier]
        return float(entry["cost"]), float(entry["hours"])

    def cheapest_tier(self) -> str:
        tiers = self.actions["generate_script"]
        return min(tiers, key=lambda t: tiers[t]["cost"])


# ------------------------------------------------------------- 校验 ----

def _check_structure(steps: list[StepSpec | Step], table: CostTable) -> None:
    """结构校验：id 唯一、动作与字段匹配、依赖存在且无环、必需交付物恰好一个且依赖搜索。"""
    ids = [s.id for s in steps]
    if len(ids) != len(set(ids)):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise PlanError("duplicate_step_id", f"步骤编号重复：{dup}")
    idset = set(ids)

    for s in steps:
        if s.action == "search_materials":
            if s.tier is not None:
                raise PlanError("invalid_step", f"{s.id}: search_materials 不应有 tier")
            if not (s.query and s.query.strip()):
                raise PlanError("invalid_step", f"{s.id}: search_materials 需要非空 query")
            if s.deliverable is not None:
                raise PlanError("invalid_step", f"{s.id}: search_materials 不应有 deliverable")
        else:  # generate_script
            table.cost_hours("generate_script", s.tier)  # 档位合法性
            if not (s.brief and s.brief.strip()):
                raise PlanError("invalid_step", f"{s.id}: generate_script 需要非空 brief")
            if s.deliverable is None:
                raise PlanError("invalid_step", f"{s.id}: generate_script 需要 deliverable")
        for d in s.depends_on:
            if d not in idset:
                raise PlanError("unknown_dependency", f"{s.id} 依赖不存在的步骤 {d}")
            if d == s.id:
                raise PlanError("dependency_cycle", f"{s.id} 依赖自己")

    # 无环
    graph = {s.id: list(s.depends_on) for s in steps}
    state: dict[str, int] = {}

    def visit(n: str) -> None:
        if state.get(n) == 1:
            raise PlanError("dependency_cycle", f"依赖成环，涉及 {n}")
        if state.get(n) == 2:
            return
        state[n] = 1
        for m in graph[n]:
            visit(m)
        state[n] = 2

    for n in graph:
        visit(n)

    # 必需交付物
    mains = [s for s in steps if s.deliverable and s.deliverable.key == "main"]
    if len(mains) != 1:
        raise PlanError("required_deliverable_missing", f"必须恰好一个 key=main 的脚本步骤，当前 {len(mains)} 个")
    main = mains[0]
    if not main.deliverable.required:  # type: ignore[union-attr]
        raise PlanError("required_deliverable_missing", f"{main.id}: key=main 必须 required=true")
    others_required = [s.id for s in steps if s.deliverable and s.deliverable.key != "main" and s.deliverable.required]
    if others_required:
        raise PlanError("invalid_step", f"只有 main 可以 required=true，违规：{others_required}")
    by_id = {s.id: s for s in steps}
    if not any(by_id[d].action == "search_materials" for d in main.depends_on):
        raise PlanError("main_needs_search", f"{main.id}: 主脚本必须直接依赖至少一个搜索步骤")
    for s in steps:
        if s.action == "generate_script" and not any(by_id[d].action == "search_materials" for d in s.depends_on):
            raise PlanError("script_needs_search", f"{s.id}: 脚本步骤必须直接依赖至少一个搜索步骤")


def _fill_costs(steps: list[Step], table: CostTable) -> None:
    for s in steps:
        if s.status != "done":
            s.cost, s.hours = table.cost_hours(s.action, s.tier)


def build_plan(run_id: str, specs: list[StepSpec], table: CostTable) -> Plan:
    _check_structure(specs, table)
    steps = [Step(**sp.model_dump()) for sp in specs]
    _fill_costs(steps, table)
    return Plan(run_id=run_id, revision=1, steps=steps)


def apply_revision(plan: Plan, args: RevisePlanArgs, table: CostTable) -> tuple[Plan, list[str]]:
    """原子修订：任一项不合法则整体拒绝，返回 (新计划, 变更描述列表)。

    规则：不得触碰 done 步骤；删除只限非 main；改档位/改词的失败步骤重置为 pending 且重试计数归零。
    """
    new = deepcopy(plan)
    changes: list[str] = []
    by_id = {s.id: s for s in new.steps}

    def touchable(sid: str) -> Step:
        s = by_id.get(sid)
        if s is None:
            raise PlanError("step_not_in_plan", f"步骤 {sid} 不在计划中")
        if s.status == "done":
            raise PlanError("step_already_done", f"步骤 {sid} 已完成，不能修改或删除")
        return s

    for ch in args.set_tier:
        s = touchable(ch.step_id)
        if s.action != "generate_script":
            raise PlanError("invalid_step", f"{s.id} 不是脚本步骤，不能改档位")
        old_cost, _ = table.cost_hours(s.action, s.tier)
        new_cost, _ = table.cost_hours(s.action, ch.tier)
        if s.tier != ch.tier:
            label = s.deliverable.key if s.deliverable else s.id
            changes.append(f"{s.id} {label} 脚本 {s.tier}→{ch.tier}（{new_cost - old_cost:+g}）")
            s.tier = ch.tier
            if s.status == "failed":
                s.status, s.attempts = "pending", 0

    for ch in args.set_query:
        s = touchable(ch.step_id)
        if s.action != "search_materials":
            raise PlanError("invalid_step", f"{s.id} 不是搜索步骤，不能改检索词")
        if s.query != ch.query:
            changes.append(f"{s.id} 搜索词「{s.query}」→「{ch.query}」")
            s.query = ch.query
            if s.status == "failed":
                s.status, s.attempts = "pending", 0

    for ch in args.set_depends:
        s = touchable(ch.step_id)
        if s.depends_on != ch.depends_on:
            changes.append(f"{s.id} 依赖 {s.depends_on}→{ch.depends_on}")
            s.depends_on = list(ch.depends_on)

    for sid in args.remove:
        s = touchable(sid)
        if s.deliverable and s.deliverable.key == "main":
            raise PlanError("required_deliverable_missing", f"{sid} 是必需的主脚本，不能删除")
        cost, _ = table.cost_hours(s.action, s.tier)
        kind = "搜索" if s.action == "search_materials" else f"{s.deliverable.key if s.deliverable else ''} 脚本"
        changes.append(f"删除 {sid} {kind}（{-cost:+g}）")
        new.steps = [x for x in new.steps if x.id != sid]
        for x in new.steps:
            if sid in x.depends_on:
                x.depends_on = [d for d in x.depends_on if d != sid]

    for sp in args.add:
        if sp.id in {x.id for x in new.steps}:
            raise PlanError("duplicate_step_id", f"新增步骤编号 {sp.id} 已存在")
        st = Step(**sp.model_dump())
        cost, _ = table.cost_hours(st.action, st.tier)
        changes.append(f"新增 {st.id} {st.action}{'/' + st.tier if st.tier else ''}（{cost:+g}）")
        new.steps.append(st)

    _check_structure(new.steps, table)
    _fill_costs(new.steps, table)
    new.revision = plan.revision + 1
    return new, changes


# ------------------------------------------------------------- 估算 ----

def open_steps(plan: Plan) -> list[Step]:
    return [s for s in plan.steps if s.status in ("pending", "failed", "running")]


def estimate(plan: Plan, budget: float, deadline: datetime, sim_now: datetime, spent: float, table: CostTable) -> Estimate:
    remaining = sum(s.cost for s in open_steps(plan))
    remaining_hours = sum(s.hours for s in open_steps(plan))
    projected = spent + remaining
    projected_finish = sim_now + timedelta(hours=remaining_hours)

    # 最低可行：已花费 + （主脚本未完成 → 最便宜档位）+（尚无完成的搜索 → 一次搜索）
    main = next(s for s in plan.steps if s.deliverable and s.deliverable.key == "main")
    min_cost, min_hours = spent, 0.0
    if main.status != "done":
        c, h = table.cost_hours("generate_script", table.cheapest_tier())
        min_cost, min_hours = min_cost + c, min_hours + h
    if not any(s.action == "search_materials" and s.status == "done" for s in plan.steps):
        c, h = table.cost_hours("search_materials", None)
        min_cost, min_hours = min_cost + c, min_hours + h

    over_budget_by = max(0.0, projected - budget)
    over_time = max(0.0, (projected_finish - deadline).total_seconds() / 3600)
    return Estimate(
        revision=plan.revision,
        spent=spent,
        remaining=remaining,
        projected=projected,
        budget=budget,
        within_budget=projected <= budget + 1e-9,
        over_budget_by=round(over_budget_by, 4),
        sim_now=sim_now,
        projected_finish=projected_finish,
        deadline=deadline,
        within_deadline=projected_finish <= deadline,
        over_time_hours=round(over_time, 4),
        min_feasible_cost=min_cost,
        min_feasible_hours=min_hours,
        cost_table_version=table.version,
    )


def save_blockers(plan: Plan, est: Optional[Estimate], ledger_sum: float, sim_now: datetime, deadline: datetime, budget: float) -> list[str]:
    """保存前必须全部为空的问题列表。"""
    problems: list[str] = []
    not_done = [s.id for s in plan.steps if s.status not in ("done", "skipped")]
    if not_done:
        problems.append(f"未完成步骤：{not_done}")
    main = next((s for s in plan.steps if s.deliverable and s.deliverable.key == "main"), None)
    if main is None or main.status != "done":
        problems.append("必需交付物 main 尚无产物")
    if est is None or est.revision != plan.revision:
        problems.append("当前计划版本没有估算（请先 estimate_cost）")
    else:
        if abs(est.spent - ledger_sum) > 1e-9 or est.remaining > 1e-9:
            problems.append(
                f"估算已过期（估算时已花费 {est.spent:g} / 剩余 {est.remaining:g}，账本已花费 {ledger_sum:g}），保存前请重新 estimate_cost"
            )
    if ledger_sum > budget + 1e-9:
        problems.append(f"已花费 {ledger_sum} 超过预算 {budget}")
    if sim_now > deadline:
        problems.append(f"模拟完成时间 {sim_now.isoformat()} 晚于截止 {deadline.isoformat()}")
    return problems
