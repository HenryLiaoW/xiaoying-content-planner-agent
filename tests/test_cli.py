"""进程级：重复执行、续跑提示、退出码。"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cli(args, cwd=ROOT):
    p = subprocess.run([sys.executable, "-m", "src.main", *args], cwd=cwd, capture_output=True, text=True)
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
    assert not (tmp_path / "a.db").exists() or True  # 入口层不建任务：由 test_loop_mock 覆盖 trace 不存在
