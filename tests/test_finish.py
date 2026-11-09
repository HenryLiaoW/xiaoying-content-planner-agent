"""finish 是会失败的工具；save_plan 的条件。"""
from conftest import PLAN_12, REVISE_TO_8


def test_completed_without_plan_rejected(tools):
    obs = tools.execute("finish", {"status": "completed", "summary": "s", "reason": "r"})
    assert obs["error"] == "finish_rejected" and "尚无计划" in obs["reasons"][0]


def test_completed_without_save_rejected_then_accepted(tools):
    tools.execute("propose_plan", {"steps": PLAN_12, "reason": "r"}); tools.execute("estimate_cost", {"reason": "r"})
    tools.execute("revise_plan", REVISE_TO_8); tools.execute("estimate_cost", {"reason": "r"})
    s = tools.execute("search_materials", {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "r"})
    ids = [s["items"][0]["id"]]
    tools.execute("generate_script", {"step_id": "s3", "material_ids": ids, "brief": "b", "reason": "r"})
    tools.execute("generate_script", {"step_id": "s4", "material_ids": ids, "brief": "b", "reason": "r"})
    rejected = tools.execute("finish", {"status": "completed", "summary": "s", "reason": "r"})
    assert rejected["error"] == "finish_rejected" and any("save_plan" in x for x in rejected["reasons"])
    stale = tools.execute("save_plan", {"reason": "r"})
    assert stale["error"] == "not_saveable" and "estimate_cost" in stale["message"]
    tools.execute("estimate_cost", {"reason": "r"})
    assert tools.execute("save_plan", {"reason": "r"})["saved"] == "created"
    assert tools.execute("save_plan", {"reason": "r"})["saved"] == "duplicate"
    assert tools.execute("finish", {"status": "completed", "summary": "s", "reason": "r"})["accepted"]


def test_incomplete_requires_reason(tools):
    assert tools.execute("finish", {"status": "incomplete", "summary": "s", "reason": "r"})["error"] == "invalid_args"
    ok = tools.execute("finish", {"status": "incomplete", "summary": "s", "unfinished_reason": "预算不够", "reason": "r"})
    assert ok["accepted"] and ok["status"] == "incomplete"


def test_save_rejected_when_over_budget_after_execution(store, req):
    """构造：预算 10，计划 8 可行；执行后人为改预算不可能——改为验证主脚本缺失时不能保存。"""
    from conftest import make_tools

    t = make_tools(store, req)
    t.execute("propose_plan", {"steps": PLAN_12, "reason": "r"}); t.execute("estimate_cost", {"reason": "r"})
    t.execute("revise_plan", REVISE_TO_8); t.execute("estimate_cost", {"reason": "r"})
    s = t.execute("search_materials", {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "r"})
    t.execute("generate_script", {"step_id": "s4", "material_ids": [s["items"][0]["id"]], "brief": "b", "reason": "r"})  # 只做了可选的
    t.execute("estimate_cost", {"reason": "r"})
    obs = t.execute("save_plan", {"reason": "r"})
    assert obs["error"] == "not_saveable" and any("main" in p for p in obs["problems"])
