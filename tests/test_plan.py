from datetime import datetime

import pytest

from src.plan import PlanError, apply_revision, build_plan, estimate, save_blockers
from src.schema import Deliverable, RevisePlanArgs, StepSpec

from conftest import PLAN_12, REVISE_TO_8

NOW = datetime.fromisoformat("2026-11-10T09:00:00+08:00")
DDL = datetime.fromisoformat("2026-11-12T18:00:00+08:00")


def specs(raw):
    return [StepSpec(**s) for s in raw]


def test_reference_example_12_then_8(table):
    p1 = build_plan("r", specs(PLAN_12), table)
    e1 = estimate(p1, 10, DDL, NOW, 0.0, table)
    assert (e1.projected, e1.within_budget, e1.over_budget_by, e1.min_feasible_cost) == (12.0, False, 2.0, 3.0)
    p2, changes, plain = apply_revision(p1, RevisePlanArgs(**REVISE_TO_8), table)
    e2 = estimate(p2, 10, DDL, NOW, 0.0, table)
    assert p2.revision == 2 and e2.projected == 8.0 and e2.within_budget
    assert any("pro→lite" in c for c in changes) and any("删除 s2" in c for c in changes)
    assert plain == ["预热脚本由完整版改为简版", "减少一次素材搜索"]


def test_min_feasible_uses_cheapest_tier_plus_one_search(table):
    p = build_plan("r", specs(PLAN_12), table)
    e = estimate(p, 2, DDL, NOW, 0.0, table)
    assert e.min_feasible_cost == 3.0 and e.min_feasible_cost - e.budget == 1.0


def test_spent_is_preserved_when_optional_step_removed(table):
    p = build_plan("r", specs(PLAN_12), table)
    p.steps[1].status = "done"  # s2 已完成并计费 1
    e = estimate(p, 10, DDL, NOW, 1.0, table)
    assert e.spent == 1.0 and e.remaining == 11.0 and e.projected == 12.0
    with pytest.raises(PlanError) as ex:
        apply_revision(p, RevisePlanArgs(remove=["s2"], reason="x"), table)
    assert ex.value.code == "step_already_done"


@pytest.mark.parametrize("bad, code", [
    ([{"id": "s1", "action": "generate_script", "tier": "pro", "brief": "b", "deliverable": {"key": "main", "required": True}}], "main_needs_search"),
    ([{"id": "s1", "action": "search_materials", "query": "q"}], "required_deliverable_missing"),
    ([{"id": "s1", "action": "search_materials", "query": "q"}, {"id": "s1", "action": "generate_script", "tier": "pro", "brief": "b", "depends_on": ["s1"], "deliverable": {"key": "main", "required": True}}], "duplicate_step_id"),
    ([{"id": "s1", "action": "search_materials", "query": "q", "depends_on": ["s2"]}, {"id": "s2", "action": "generate_script", "tier": "pro", "brief": "b", "depends_on": ["s1"], "deliverable": {"key": "main", "required": True}}], "dependency_cycle"),
    ([{"id": "s1", "action": "search_materials", "query": "q"}, {"id": "s2", "action": "generate_script", "tier": "pro", "brief": "b", "depends_on": ["s9"], "deliverable": {"key": "main", "required": True}}], "unknown_dependency"),
    ([{"id": "s1", "action": "search_materials", "query": "q"}, {"id": "s2", "action": "generate_script", "tier": "pro", "brief": "b", "depends_on": ["s1"], "deliverable": {"key": "main", "required": False}}], "required_deliverable_missing"),
])
def test_structure_rejections(table, bad, code):
    with pytest.raises(PlanError) as ex:
        build_plan("r", specs(bad), table)
    assert ex.value.code == code


def test_revision_cannot_remove_main_or_unknown(table):
    p = build_plan("r", specs(PLAN_12), table)
    for args, code in [({"remove": ["s3"]}, "required_deliverable_missing"), ({"remove": ["s9"]}, "step_not_in_plan"),
                       ({"set_tier": [{"step_id": "s1", "tier": "lite"}]}, "invalid_step")]:
        with pytest.raises(PlanError) as ex:
            apply_revision(p, RevisePlanArgs(**args, reason="x"), table)
        assert ex.value.code == code


def test_revision_resets_failed_step_when_tier_changes(table):
    p = build_plan("r", specs(PLAN_12), table)
    s4 = p.get("s4"); s4.status, s4.attempts = "failed", 2
    p2, _, _ = apply_revision(p, RevisePlanArgs(set_tier=[{"step_id": "s4", "tier": "lite"}], reason="x"), table)
    assert (p2.get("s4").status, p2.get("s4").attempts, p2.get("s4").cost) == ("pending", 0, 2.0)


def test_sim_clock_and_deadline(table):
    p = build_plan("r", specs(PLAN_12), table)
    e = estimate(p, 20, NOW.replace(hour=10, minute=30), NOW, 0.0, table)
    assert e.projected_finish == NOW.replace(hour=14) and not e.within_deadline and e.over_time_hours == 3.5


def test_save_blockers(table):
    p = build_plan("r", specs(PLAN_12), table)
    probs = save_blockers(p, None, 0.0, NOW, DDL, 10)
    assert any("未完成步骤" in x for x in probs) and any("没有估算" in x for x in probs)
    for s in p.steps:
        s.status = "done"
    e = estimate(p, 20, DDL, NOW, 12.0, table)
    assert save_blockers(p, e, 12.0, NOW, DDL, 20) == []
    assert any("超过预算" in x for x in save_blockers(p, e, 12.0, NOW, DDL, 10))
