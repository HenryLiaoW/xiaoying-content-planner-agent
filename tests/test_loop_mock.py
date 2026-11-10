"""每个场景一用例：MockLLM 按脚本发动作并核对收到的观察；断言状态、费用、步骤、事件。"""
import json
from pathlib import Path

import pytest

from src.scenarios import SCENARIOS

from conftest import run_cli_json


def run_scenario(name: str, tmp_path: Path):
    sc = SCENARIOS[name]
    inp = tmp_path / f"{name}.json"
    inp.write_text(json.dumps(sc.input, ensure_ascii=False), encoding="utf-8")
    args = ["--input", str(inp), "--db", str(tmp_path / "a.db"), "--out", str(tmp_path / name), *sc.cli]
    if sc.mock_script:
        args += ["--mock", "--mock-script", sc.mock_script]
    return run_cli_json(args, tmp_path), tmp_path / name


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_scenario(name, tmp_path):
    (out, code), out_dir = run_scenario(name, tmp_path)
    sc = SCENARIOS[name]
    exp = sc.expect
    assert out["status"] == exp["status"], out.get("user_message")
    assert code == exp.get("exit_code", 0)
    if "missing_fields" in exp:
        assert out["missing_fields"] == exp["missing_fields"]
        assert not (out_dir / "trace.jsonl").exists()  # 入口层返回，未进入循环
        return
    if out["status"] in ("invalid_input",):
        return
    res = out["result"]
    if "spent_cost" in exp:
        assert res["spent_cost"] == exp["spent_cost"]
    assert bool(res["saved_plan"]) == exp["saved"]
    if exp["saved"]:
        assert res["remaining_estimate"] == 0 and res["projected_total"] == res["spent_cost"]
        assert res["spent_cost"] <= res["budget"] and res["within_deadline"]
    if "gap" in exp:
        assert res["gap"] == exp["gap"]
    if "finish_reason" in exp:
        assert out["finish_reason"] == exp["finish_reason"]
    for sid, n in exp.get("attempts", {}).items():
        step = next(s for s in res["plan"]["steps"] if s["id"] == sid)
        assert step["attempts"] == n
    if "adjust_trigger" in exp:
        assert out["adjustments"][0]["trigger"] == exp["adjust_trigger"]
        assert "调整" in out["user_message"]
    # 五要素审阅版
    review = out["trace_review"]
    assert len(review) == out["steps_used"]
    for row in review:
        assert set(row) >= {"step", "tool", "args", "result_summary", "next_action", "decision_basis"}
        assert row["decision_basis"]
    assert review[-1]["next_action"] in ("(end)", "(step limit reached)")
    # 事件日志只追加
    lines = (out_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    seqs = [json.loads(l)["seq"] for l in lines]
    assert seqs == sorted(seqs) and lines[0].count('"run_start"') == 1 and '"run_end"' in lines[-1]
    assert out["note"].startswith("离线 Mock")


def test_failed_step_never_recorded_as_success(tmp_path):
    (out, _), _ = run_scenario("tool_failure_persistent", tmp_path)
    s2 = next(s for s in out["result"]["plan"]["steps"] if s["id"] == "s2")
    assert s2["status"] == "failed" and "s2" not in out["result"]["artifacts"]
    assert "s1" in out["result"]["artifacts"] and out["result"]["spent_cost"] == 1.0


def test_ledger_equals_sum_of_done_steps(tmp_path):
    (out, _), _ = run_scenario("over_budget", tmp_path)
    done = [s for s in out["result"]["plan"]["steps"] if s["status"] == "done"]
    assert sum(s["cost"] for s in done) == out["result"]["spent_cost"] == 8.0
    assert [s["id"] for s in out["result"]["plan"]["steps"]] == ["s1", "s3", "s4"]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_user_message_has_no_internal_ids(name, tmp_path):
    """给运营的话里不出现步骤编号、工具名等内部名称。"""
    import re

    (out, _), _ = run_scenario(name, tmp_path)
    msg = out.get("user_message") or ""
    assert not re.search(r"\bs\d+\b|generate_script|search_materials|\bmain\b", msg), msg


def _sub(tmp_path, name):
    d = tmp_path / name
    d.mkdir()
    return d


def test_handoff_note_only_when_something_went_wrong(tmp_path):
    """转交说明：失败 / 动作用完时生成，含任务号、工具名、失败次数；正常完成和预算不足时没有。"""
    (out, _), _ = run_scenario("tool_failure_persistent", _sub(tmp_path, "a"))
    note = out["handoff_note"]
    assert out["run_id"] in note and "generate_script" in note and "累计失败 2 次" in note and "--resume" in note
    assert "转交说明" in out["user_message"]
    (out, _), _ = run_scenario("step_limit", _sub(tmp_path, "b"))
    assert out["handoff_note"] and "动作额度" in out["handoff_note"]
    for name in ("normal", "over_budget", "infeasible"):
        (out, _), _ = run_scenario(name, _sub(tmp_path, name))
        assert out["handoff_note"] is None and "转交说明" not in out["user_message"]
