"""进程级：重复执行、续跑提示、退出码。"""
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NO_KEY = {**os.environ, "LLM_API_KEY": ""}  # 显式置空：不读本机 .env 里的真实密钥，测试不会发起任何模型调用


def cli(args, cwd=ROOT, env=None):
    p = subprocess.run([sys.executable, "-m", "src.main", *args], cwd=cwd, capture_output=True, text=True, env=env)
    try:
        return json.loads(p.stdout), p.returncode
    except json.JSONDecodeError:
        raise AssertionError(f"非 JSON 输出：{p.stdout[:300]} / {p.stderr[-500:]}")


def write(tmp_path, name, obj):
    f = tmp_path / name
    f.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    return str(f)


BASE = {"request_id": "req-cli-001", "topic": "一键 Vlog 模板双11推广", "budget": 20,
        "deadline": "2026-11-12T18:00:00+08:00", "now": "2026-11-10T09:00:00+08:00"}


def test_duplicate_submission_is_idempotent(tmp_path):
    inp = write(tmp_path, "in.json", BASE)
    db = str(tmp_path / "a.db")
    first, c1 = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o1"), "--mock"])
    second, c2 = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o2"), "--mock"])
    assert c1 == c2 == 0 and first["status"] == "completed"
    assert second["served_from"] == "existing" and second["run_id"] == first["run_id"]
    assert second["result"]["spent_cost"] == first["result"]["spent_cost"] == 8.0
    assert not (tmp_path / "o2" / "trace.jsonl").exists()  # 第二次零新事件
    dump, _ = cli(["dump", "--db", db, "--run", first["run_id"]])
    assert len(dump["runs"]) == 1 and len(dump["saved_plans"]) == 1 and len(dump["ledger"]) == 3


def test_conflict_same_request_id_different_budget(tmp_path):
    db = str(tmp_path / "a.db")
    cli(["--input", write(tmp_path, "a.json", BASE), "--db", db, "--out", str(tmp_path / "o1"), "--mock"])
    out, code = cli(["--input", write(tmp_path, "b.json", {**BASE, "budget": 12}), "--db", db, "--out", str(tmp_path / "o2"), "--mock"])
    assert code == 0 and out["status"] == "request_conflict"
    assert not (tmp_path / "o2").exists()


def test_new_task_with_different_request_id_runs_again(tmp_path):
    db = str(tmp_path / "a.db")
    a, _ = cli(["--input", write(tmp_path, "a.json", BASE), "--db", db, "--out", str(tmp_path / "o1"), "--mock"])
    b, _ = cli(["--input", write(tmp_path, "b.json", {**BASE, "request_id": "req-cli-002"}), "--db", db, "--out", str(tmp_path / "o2"), "--mock"])
    assert a["run_id"] != b["run_id"] and b["served_from"] == "executed" and b["status"] == "completed"


def test_failed_task_needs_explicit_resume(tmp_path):
    inp = write(tmp_path, "in.json", BASE)
    db = str(tmp_path / "a.db")
    first, _ = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o1"), "--mock", "--mock-script", "tool_failure_persistent", "--inject", "generate_script:always"])
    assert first["status"] == "failed"
    again, _ = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o2"), "--mock"])
    assert again["served_from"] == "existing" and "--resume" in again["hint"]


def test_exit_codes(tmp_path):
    out, code = cli(["--input", write(tmp_path, "bad.json", {**BASE, "budget": "20"}), "--db", str(tmp_path / "a.db")])
    assert code == 2 and out["status"] == "invalid_input"
    out, code = cli(["--input", write(tmp_path, "miss.json", {k: v for k, v in BASE.items() if k != "budget"}), "--db", str(tmp_path / "a.db")])
    assert code == 0 and out["status"] == "needs_input" and out["missing_fields"] == ["budget"]
    assert not (tmp_path / "a.db").exists()  # 入口层直接返回：不建库、不建任务


def test_resume_redoes_only_failed_step_and_keeps_ledger(tmp_path):
    inp = write(tmp_path, "in.json", BASE)
    db = str(tmp_path / "a.db")
    first, _ = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o1"), "--mock", "--mock-script", "tool_failure_persistent", "--inject", "generate_script:always"])
    assert first["status"] == "failed" and first["result"]["spent_cost"] == 1.0
    second, code = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o2"), "--mock", "--mock-script", "resume", "--resume"])
    assert code == 0 and second["status"] == "completed" and second["served_from"] == "resumed" and second["attempt_no"] == 2
    assert second["run_id"] == first["run_id"] and second["result"]["spent_cost"] == 6.0 and second["business_exec_count"] == 1
    steps = {s["id"]: s for s in second["result"]["plan"]["steps"]}
    assert steps["s1"]["attempts"] == 1 and steps["s2"]["status"] == "done"
    dump, _ = cli(["dump", "--db", db, "--run", first["run_id"]])
    assert [(l["step_id"], l["amount"]) for l in dump["ledger"]] == [("s1", 1.0), ("s2", 5.0)]  # s1 未重复计费
    assert len(dump["runs"]) == 1 and len(dump["saved_plans"]) == 1


def test_new_task_flag_creates_a_separate_run(tmp_path):
    """无 request_id 时：同样的输入默认算重复提交；加 --new-task 才新建任务。带 request_id 时该开关被拒。"""
    no_id = {k: v for k, v in BASE.items() if k != "request_id"}
    inp = write(tmp_path, "in.json", no_id)
    db = str(tmp_path / "a.db")
    first, _ = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o1"), "--mock"])
    again, _ = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o2"), "--mock"])
    fresh, _ = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o3"), "--mock", "--new-task"])
    assert again["served_from"] == "existing" and again["run_id"] == first["run_id"]
    assert fresh["served_from"] == "executed" and fresh["run_id"] != first["run_id"] and fresh["status"] == "completed"
    rejected, code = cli(["--input", write(tmp_path, "b.json", BASE), "--db", db, "--out", str(tmp_path / "o4"), "--mock", "--new-task"])
    assert rejected["status"] == "invalid_input" and code == 2


def test_config_error_leaves_no_run_behind(tmp_path):
    """真实模式缺密钥：返回 config_error（退出码 2），且库里不留半截任务；随后同一输入、同一个库用离线模式能正常执行。"""
    inp = write(tmp_path, "in.json", BASE)
    db = tmp_path / "a.db"
    out, code = cli(["--input", inp, "--db", str(db), "--out", str(tmp_path / "o1")], env=NO_KEY)
    assert code == 2 and out["status"] == "config_error"
    assert not (tmp_path / "o1").exists()
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    again, code = cli(["--input", inp, "--db", str(db), "--out", str(tmp_path / "o2"), "--mock"])
    assert code == 0 and again["status"] == "completed" and again["served_from"] == "executed"


def test_real_and_mock_records_are_not_mixed(tmp_path):
    """同一个库里，离线 Mock 跑过的请求再用真实模式提交：既不返回 Mock 的结果，也不执行。"""
    inp = write(tmp_path, "in.json", BASE)
    db = str(tmp_path / "a.db")
    first, _ = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o1"), "--mock"])
    assert first["status"] == "completed" and first["mode"] == "offline_mock"
    second, code = cli(["--input", inp, "--db", db, "--out", str(tmp_path / "o2")], env=NO_KEY)
    assert code == 0 and second["status"] == "request_conflict" and second["conflict"] == "mode"
    assert "result" not in second and not (tmp_path / "o2").exists()


def test_default_database_differs_by_mode(tmp_path):
    """不传 --db 时，离线 Mock 写 out/mock.db，不碰真实模型默认用的 out/agent.db。"""
    from src.main import default_db_path

    assert default_db_path(mock=True) == "out/mock.db" and default_db_path(mock=False) == "out/agent.db"
    inp = write(tmp_path, "in.json", BASE)
    out, code = cli(["--input", inp, "--mock", "--out", str(tmp_path / "o1")], cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert code == 0 and out["status"] == "completed"
    assert (tmp_path / "out" / "mock.db").exists() and not (tmp_path / "out" / "agent.db").exists()


def test_output_records_source_version(tmp_path):
    """终报与事件日志都记下产生它的源码版本（git 短提交号，可带 +dirty；非 git 环境为 unknown）。"""
    out, _ = cli(["--input", write(tmp_path, "in.json", BASE), "--db", str(tmp_path / "a.db"), "--out", str(tmp_path / "o1"), "--mock"])
    assert re.fullmatch(r"[0-9a-f]{7,40}(\+dirty)?|unknown", out["source_version"])
    start = json.loads((tmp_path / "o1" / "trace.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert start["type"] == "run_start" and start["source_version"] == out["source_version"]
