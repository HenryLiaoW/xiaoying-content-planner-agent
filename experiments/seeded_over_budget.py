"""独立验证实验：从一份【人工预置】的超预算初稿出发，看真实模型如何估算、调整、重估、保存。

为什么需要它：提示词里写明了价格表，真实模型会一开始就把计划排进预算，
所以常规运行里看不到“先超预算、再重规划”。这里不隐藏价格、不改提示词、不改 src/ 下任何代码，
只是把“第一版计划由谁提出”换成人工：初稿通过现有的 propose_plan 工具入库（同样经过全部校验），
之后的每一步（估算、怎么调、调到多少、是否保存）都由真实模型在现有决策循环里自己决定。

这不是生产流程的一部分，结果里用 initialization 字段和 Trace 的 seed_plan 事件标明初稿来源。

用法（需要 .env 里的真实密钥）：
    uv run python -m experiments.seeded_over_budget
    uv run python -m experiments.seeded_over_budget --out out/seeded_over_budget --db out/seeded.db
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.llm import OpenAICompatClient
from src.loop import DEFAULT_MAX_STEPS, run_loop
from src.main import source_version
from src.plan import CostTable
from src.report import build_report
from src.schema import check_input, fingerprint, identity_of, normalized
from src.store import Store
from src.tools import Tools
from src.trace import Trace

ROOT = Path(__file__).resolve().parent.parent

REQUEST = {
    "request_id": "req-seeded-over-budget-001",
    "topic": "一键 Vlog 模板双11推广",
    "budget": 10,
    "deadline": "2026-11-12T18:00:00+08:00",
    "now": "2026-11-10T09:00:00+08:00",
    "quality_preference": "high",
}

# 人工写的初稿：两次搜素材（各 1）+ 完整版主视频脚本（5）+ 完整版预热脚本（5）= 12，超过预算 10。
SEED_PLAN = {
    "reason": "【人工预置初稿，非模型生成】验证实验用：两次搜素材，主视频与预热脚本都用完整版",
    "steps": [
        {"id": "s1", "action": "search_materials", "query": "一键Vlog模板 教程"},
        {"id": "s2", "action": "search_materials", "query": "双11 Vlog推广 素材"},
        {"id": "s3", "action": "generate_script", "tier": "pro", "depends_on": ["s1"],
         "deliverable": {"key": "main", "required": True}, "brief": "主推广视频脚本"},
        {"id": "s4", "action": "generate_script", "tier": "pro", "depends_on": ["s2"],
         "deliverable": {"key": "teaser", "required": False}, "brief": "预热短视频脚本"},
    ],
}

# 给模型的说明：只交代事实（初稿已存在、不是它提的、还没估算），不提示该怎么调、调到多少。
SEED_CONTEXT = (
    "说明：本任务已有一份由人工提供的初稿计划（不是你提出的），已记录为 revision 1，尚未估算、尚未执行。"
    "当前计划：%s\n请从 estimate_cost 开始，之后根据观察结果自行决定下一步。"
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--out", default="out/seeded_over_budget")
    p.add_argument("--db", default="out/seeded_over_budget.db")
    p.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    args = p.parse_args(argv)

    out_dir = Path(args.out)
    db_path = Path(args.db)
    if db_path.exists():
        print(f"数据库 {db_path} 已存在；为避免和上一次实验混在一起，请换 --db 或先移走它。", file=sys.stderr)
        return 2
    load_dotenv()  # 和正式入口一样从 .env 读密钥
    try:
        llm = OpenAICompatClient()
    except RuntimeError as e:  # 没配密钥：不建库、不留半截任务
        print(f"配置错误：{e}", file=sys.stderr)
        return 2

    req = check_input(REQUEST)
    fp = fingerprint(req)
    out_dir.mkdir(parents=True, exist_ok=True)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    (out_dir / "input.json").write_text(json.dumps(REQUEST, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "seed_plan.json").write_text(json.dumps(SEED_PLAN, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    store = Store(str(db_path))
    run_id = store.create_run(identity_of(req, fp), req.request_id, fp, REQUEST, normalized(req), req.now, "real")
    tools = Tools(store, run_id, req, table=CostTable())

    version = source_version()
    import openai

    model_info = {"model": llm.model, "temperature": llm.temperature, "sdk": f"openai {openai.__version__}",
                  "base_url": llm.base_url, "source_version": version}
    trace = Trace(out_dir / "trace.jsonl", run_id, 1)
    trace.event("run_start", mode="real", served_from="executed", request=normalized(req), inject=None,
                max_steps=args.max_steps, model=llm.model, source_version=version,
                experiment="seeded_over_budget")

    # 初稿走现有的 propose_plan 工具入库：和模型提交的计划经过同一套校验
    seed_obs = tools.execute("propose_plan", SEED_PLAN)
    trace.event("seed_plan", source="human_seed", via="propose_plan", ok=bool(seed_obs.get("ok")),
                plan=SEED_PLAN, note="初稿由人工提供；此后所有动作由真实模型决定")
    if not seed_obs.get("ok"):
        print(f"初稿未通过校验：{seed_obs}", file=sys.stderr)
        return 2

    ctx = SEED_CONTEXT % json.dumps(tools.plan_view(), ensure_ascii=False)
    result = run_loop(llm, tools, trace, req, max_steps=args.max_steps, extra_user_context=ctx)

    report = build_report(store, run_id, req, fp, tools, result, trace, "real", model_info, args.max_steps, "executed")
    report["initialization"] = {
        "source": "human_seed",
        "note": "第一版计划由人工预置（见 seed_plan.json），不是模型提出的；估算、调整、重估、保存均由真实模型完成",
        "context_given_to_model": ctx,
    }
    store.update_run(run_id, status=result.status, steps_used=result.steps_used, result_json=report)
    trace.event("run_end", status=result.status, reason=result.reason, steps_used=result.steps_used,
                spent_cost=report["result"]["spent_cost"])
    report["trace_review"] = trace.review()
    (out_dir / "trace_review.json").write_text(json.dumps(report["trace_review"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "output.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")

    print(f"源码版本：{version}　状态：{report['status']}　动作数：{result.steps_used}")
    for e in report["trace_review"]:
        print(f"  第 {e['step']} 步 {e['tool']}：{e.get('decision_basis') or ''}"[:160])
    print("给运营的话：", report.get("user_message"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
