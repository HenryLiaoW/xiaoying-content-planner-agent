"""七个动作的执行前校验、缓存复用、素材引用限制、注入计数。"""
from conftest import PLAN_12, REVISE_TO_8, make_tools


def propose(t, plan=PLAN_12):
    return t.execute("propose_plan", {"steps": plan, "reason": "r"})


def test_precheck_order_and_codes(tools):
    assert tools.execute("search_materials", {"step_id": "s1", "query": "q", "reason": "r"})["error"] == "no_plan"
    assert propose(tools)["ok"]
    assert tools.execute("propose_plan", {"steps": PLAN_12, "reason": "r"})["error"] == "plan_exists"
    assert tools.execute("search_materials", {"step_id": "s9", "query": "q", "reason": "r"})["error"] == "step_not_in_plan"
    assert tools.execute("generate_script", {"step_id": "s1", "material_ids": [], "brief": "b", "reason": "r"})["error"] == "action_mismatch"
    assert tools.execute("search_materials", {"step_id": "s1", "query": "q", "reason": "r"})["error"] == "estimate_required"
    est = tools.execute("estimate_cost", {"reason": "r"})
    assert est["projected_total"] == 12.0 and not est["within_budget"]
    obs = tools.execute("search_materials", {"step_id": "s1", "query": "q", "reason": "r"})
    assert obs["error"] == "over_budget" and obs["over_budget_by"] == 2.0 and not obs["retryable"]
    assert tools.spent() == 0.0  # 拒绝不计费


def test_deps_cached_and_material_restriction(tools):
    propose(tools)
    assert tools.execute("revise_plan", REVISE_TO_8)["ok"]
    assert tools.execute("search_materials", {"step_id": "s1", "query": "q", "reason": "r"})["error"] == "estimate_required"  # 改计划后需重估
    assert tools.execute("estimate_cost", {"reason": "r"})["within_budget"]
    assert tools.execute("generate_script", {"step_id": "s3", "material_ids": ["m01"], "brief": "b", "reason": "r"})["error"] == "deps_not_done"
    s = tools.execute("search_materials", {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "r"})
    assert s["ok"] and s["count"] > 0 and s["charged"] == 1 and tools.spent() == 1.0
    again = tools.execute("search_materials", {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "r"})
    assert again["already_done"] and tools.spent() == 1.0 and tools.business_exec_count == 1
    bad = tools.execute("generate_script", {"step_id": "s3", "material_ids": ["m99"], "brief": "b", "reason": "r"})
    assert bad["error"] == "unknown_material" and "m01" in bad["allowed"]
    ok = tools.execute("generate_script", {"step_id": "s3", "material_ids": [s["items"][0]["id"]], "brief": "主视频", "reason": "r"})
    assert ok["ok"] and ok["script"]["tier"] == "pro" and len(ok["script"]["scenes"]) >= 3 and tools.spent() == 6.0


def test_injection_first_then_retry_limit(store, req):
    t = make_tools(store, req, ["generate_script:always"])
    propose(t); t.execute("revise_plan", REVISE_TO_8); t.execute("estimate_cost", {"reason": "r"})
    s = t.execute("search_materials", {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "r"})
    ids = [s["items"][0]["id"]]
    f1 = t.execute("generate_script", {"step_id": "s3", "material_ids": ids, "brief": "b", "reason": "r"})
    assert f1["error"] == "tool_error" and f1["retryable"] and f1["attempts"] == 1
    f2 = t.execute("generate_script", {"step_id": "s3", "material_ids": ids, "brief": "b", "reason": "r"})
    assert f2["error"] == "tool_error" and not f2["retryable"] and f2["attempts"] == 2
    f3 = t.execute("generate_script", {"step_id": "s3", "material_ids": ids, "brief": "b", "reason": "r"})
    assert f3["error"] == "retry_limit_reached"
    assert t.spent() == 1.0  # 失败不计费，搜索结果保留
    assert store.load_plan(t.run_id).get("s3").status == "failed"
    assert store.load_plan(t.run_id).get("s1").status == "done"


def test_injection_first_only_fails_once(store, req):
    t = make_tools(store, req, ["generate_script:first"])
    propose(t); t.execute("revise_plan", REVISE_TO_8); t.execute("estimate_cost", {"reason": "r"})
    s = t.execute("search_materials", {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "r"})
    ids = [s["items"][0]["id"]]
    assert t.execute("generate_script", {"step_id": "s3", "material_ids": ids, "brief": "b", "reason": "r"})["error"] == "tool_error"
    ok = t.execute("generate_script", {"step_id": "s3", "material_ids": ids, "brief": "b", "reason": "r"})
    assert ok["ok"] and t.spent() == 6.0 and store.load_plan(t.run_id).get("s3").attempts == 2


def test_tier_comes_from_plan_not_args(tools):
    propose(tools); tools.execute("revise_plan", REVISE_TO_8); tools.execute("estimate_cost", {"reason": "r"})
    s = tools.execute("search_materials", {"step_id": "s1", "query": "Vlog 模板 双11", "reason": "r"})
    tools.execute("generate_script", {"step_id": "s3", "material_ids": [s["items"][0]["id"]], "brief": "b", "reason": "r"})
    ok = tools.execute("generate_script", {"step_id": "s4", "material_ids": [s["items"][0]["id"]], "brief": "b", "reason": "r"})
    assert ok["script"]["tier"] == "lite" and ok["charged"] == 2


def test_invalid_args_is_observation(tools):
    obs = tools.execute("propose_plan", {"steps": "not-a-list"})
    assert obs["error"] == "invalid_args" and "reason" in obs["message"] or obs["error"] == "invalid_args"
    assert tools.execute("nonexistent", {})["error"] == "unknown_action"


def test_adjustment_records_trigger_and_changes(tools):
    propose(tools); tools.execute("estimate_cost", {"reason": "r"})
    tools.execute("revise_plan", REVISE_TO_8)
    a = tools.adjustments[0]
    assert a["trigger"] == "over_budget" and a["before"]["projected_total"] == 12.0 and a["after"]["projected_total"] == 8.0
    assert a["model_reason"] == "超预算 2" and a["changes_plain"]
