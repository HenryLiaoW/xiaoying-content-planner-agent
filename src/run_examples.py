"""批量运行场景：`python -m src.run_examples --mock`（离线，写到 examples/mock/<场景>/）
或 `python -m src.run_examples`（真实模型，需要 .env；写到 out/real_examples/<场景>/，不覆盖 examples/ 里的存档）。

每个场景目录：input.json、output.json、trace.jsonl、trace_review.json、command.txt。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from .main import run_cli
from .scenarios import SCENARIOS

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mock", action="store_true", help="离线 Mock（写到 examples/mock/）")
    p.add_argument("--only", nargs="*", help="只跑这些场景")
    p.add_argument("--keep-db", action="store_true", help="不清空场景数据库")
    args = p.parse_args(argv)

    base = ROOT / "examples" / "mock" if args.mock else ROOT / "out" / "real_examples"
    names = args.only or list(SCENARIOS)
    summary = []
    for name in names:
        sc = SCENARIOS[name]
        out_dir = base / name
        out_dir.mkdir(parents=True, exist_ok=True)
        db = out_dir / "agent.db"
        if not args.keep_db and db.exists():
            db.unlink()
        for f in ("output.json", "trace.jsonl", "trace_review.json"):
            if (out_dir / f).exists():
                (out_dir / f).unlink()
        (out_dir / "input.json").write_text(json.dumps(sc.input, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        rel_in = (out_dir / "input.json").relative_to(ROOT)
        cli = ["--input", str(rel_in), "--out", str(out_dir.relative_to(ROOT)), "--db", str(db.relative_to(ROOT)), *sc.cli]
        if args.mock:
            cli += ["--mock", "--mock-script", sc.mock_script or "normal"]
        (out_dir / "command.txt").write_text("python -m src.main " + " ".join(cli) + "\n", encoding="utf-8")

        print(f"\n### {name}  ({sc.covers})", file=sys.stderr)
        import io, contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_cli(cli)
        out_text = buf.getvalue()
        if not (out_dir / "output.json").exists():  # 入口层直接返回的场景
            (out_dir / "output.json").write_text(out_text, encoding="utf-8")
        try:
            status = json.loads(out_text).get("status")
        except json.JSONDecodeError:
            status = "?"
        summary.append((name, status, code))
        print(f"→ status={status} exit={code}", file=sys.stderr)
        if db.exists():
            db.unlink()  # 数据库不入库；重复执行场景由 tests/test_cli.py 覆盖
    print("\n| 场景 | 状态 | 退出码 |\n|---|---|---|", file=sys.stderr)
    for n, s, c in summary:
        print(f"| {n} | {s} | {c} |", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
