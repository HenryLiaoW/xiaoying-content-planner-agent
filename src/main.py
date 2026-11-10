"""CLI 入口：输入校验 → 身份分流 → 决策循环 → 终报。

退出码：0 = 正常（含 needs_input / incomplete / failed / request_conflict 等业务状态）；2 = 格式错误（invalid_input）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

from .llm import MockLLM, OpenAICompatClient
from .loop import DEFAULT_MAX_STEPS, run_loop
from .plan import CostTable
from .report import build_report
from .schema import InputError, NeedsInput, Request, check_input, fingerprint, identity_of, normalized, parse_dt
from .store import Store
from .tools import Injector, Tools
from .trace import Trace

FIELD_LABELS = {"topic": "主题", "budget": "预算", "deadline": "截止时间"}


def _emit(obj: dict[str, Any], out_dir: Optional[Path] = None, name: str = "output.json") -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / name).write_text(text + "\n", encoding="utf-8")
    print(text)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.main", description="内容任务规划 Agent（题二）")
    sub = p.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="执行一个请求（默认子命令）")
    _add_run_args(run)
    _add_run_args(p)  # 允许省略 run 子命令

    dump = sub.add_parser("dump", help="导出某个任务在库中的全部记录")
    dump.add_argument("--db", required=True)
    dump.add_argument("--run", required=True, help="run_id")
    return p


def _add_run_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--input", help="请求 JSON 文件路径")
    p.add_argument("--db", default="out/agent.db", help="SQLite 路径（默认 out/agent.db）")
    p.add_argument("--out", help="输出目录（默认 out/<run_id>）")
    p.add_argument("--now", help="覆盖输入中的 now（ISO 8601）")
    p.add_argument("--mock", action="store_true", help="离线 Mock：替换模型客户端，其余真跑；仅验证流程")
    p.add_argument("--mock-script", default="normal", help="Mock 脚本名（默认 normal）")
    p.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    p.add_argument("--inject", action="append", default=[], metavar="TOOL:first|always", help="故障注入，可重复")
    p.add_argument("--resume", action="store_true", help="续跑已有的 failed/incomplete 任务")
    p.add_argument("--new-task", action="store_true", help="参数相同也新建任务")
    p.add_argument("--quiet", action="store_true", help="stdout 只打印 user_message")


def run_cli(argv: Optional[list[str]] = None) -> int:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "dump":
        store = Store(args.db)
        _emit(store.dump(args.run))
        return 0
    if not args.input:
        parser.error("--input 是必需的")
    return run_request(args)


def run_request(args: argparse.Namespace) -> int:
    # ---- 1. 读取与校验（纯代码，不调模型）
    try:
        raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _emit({"status": "invalid_input", "error": f"无法读取或解析输入：{e}"})
        return 2
    if args.now and isinstance(raw, dict):
        raw = {**raw, "now": args.now}
    try:
        req = check_input(raw)
    except InputError as e:
        _emit({"status": "invalid_input", "error": str(e), "user_message": f"输入格式有误：{e}"})
        return 2
    except NeedsInput as e:
        labels = "、".join(FIELD_LABELS.get(f, f) for f in e.missing)
        _emit({
            "status": "needs_input",
            "missing_fields": e.missing,
            "clarifying_question": f"请补充{labels}后重新提交。",
            "user_message": f"缺少{labels}，请补充后重新提交。本次未调用模型、未产生费用。",
        })
        return 0
    if args.new_task and req.request_id:
        _emit({"status": "invalid_input", "error": "--new-task 不能与显式 request_id 同用；请换一个 request_id"})
        return 2

    fp = fingerprint(req)
    identity = identity_of(req, fp, new_task=args.new_task)
    mode = "offline_mock" if args.mock else "real"
    store = Store(args.db)

    # ---- 2. 身份分流
    existing = store.get_run_by_identity(identity)
    served_from = "executed"
    if existing is not None:
        if existing["fingerprint"] != fp:
            _emit({
                "status": "request_conflict", "run_id": existing["run_id"], "request_id": req.request_id,
                "message": "同一 request_id 此前提交过不同的参数；未执行、未覆盖。如是新任务请换 request_id。",
                "user_message": "这个请求编号之前用过，但主题/预算/截止时间不一样，为避免覆盖没有执行。请换一个请求编号。",
            })
            return 0
        prior = json.loads(existing["result_json"]) if existing["result_json"] else None
        if existing["status"] == "completed":
            out = {**(prior or {}), "served_from": "existing"}
            out["user_message"] = "（重复提交）" + str(out.get("user_message", ""))
            _emit(out, Path(args.out) if args.out else None)
            return 0
        if not args.resume:
            out = {**(prior or {"run_id": existing["run_id"], "status": existing["status"]}), "served_from": "existing"}
            out["hint"] = f"任务 {existing['run_id']} 状态为 {existing['status']}（attempt {existing['attempt_no']}）；加 --resume 续跑，已完成步骤与费用会保留。"
            _emit(out, Path(args.out) if args.out else None)
            return 0
        run_id = existing["run_id"]
        served_from = "resumed"
        store.update_run(run_id, status="running", attempt_no=existing["attempt_no"] + 1)
    else:
        if args.resume:
            _emit({"status": "not_found", "message": "没有可续跑的任务（该输入此前未提交过）", "user_message": "没有找到可以继续的任务，请去掉 --resume 直接提交。"})
            return 0
        run_id = store.create_run(identity, req.request_id, fp, raw, normalized(req), req.now, mode)

    run = store.get_run(run_id)
    attempt_no = run["attempt_no"]
    out_dir = Path(args.out) if args.out else Path("out") / run_id
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 3. 组装运行时
    try:
        injector = Injector(args.inject)
    except ValueError as e:
        _emit({"status": "invalid_input", "error": str(e)})
        return 2
    table = CostTable()
    tools = Tools(store, run_id, req, table=table, injector=injector, script_backend=os.environ.get("SCRIPT_BACKEND", "template"))
    if args.mock:
        from .mock_scripts import get_script

        llm: Any = MockLLM(get_script(args.mock_script), label=args.mock_script)
        model_info = {"model": f"mock:{args.mock_script}", "temperature": None, "sdk": None}
    else:
        try:
            llm = OpenAICompatClient()
        except RuntimeError as e:
            _emit({"status": "config_error", "error": str(e), "user_message": str(e)})
            return 2
        import openai

        model_info = {"model": llm.model, "temperature": llm.temperature, "sdk": f"openai {openai.__version__}", "base_url": llm.base_url}

    extra_ctx = None
    if served_from == "resumed":
        # 新的重试窗口：未完成步骤的尝试计数归零；已完成步骤与费用保留
        if tools.plan:
            for s in tools.plan.steps:
                if s.status in ("failed", "running"):
                    s.status, s.attempts = "pending", 0
                    store.set_step_status(run_id, s)
            tools.sim_now = max(tools.sim_now, req.now)
            store.update_run(run_id, sim_now=tools.sim_now)
            done_results = {}
            for s in tools.plan.steps:
                if s.status == "done":
                    r = store.step_result(run_id, s.id) or {}
                    if s.action == "search_materials":
                        done_results[s.id] = {"query": r.get("query"), "items": [{"id": m["id"], "title": m["title"]} for m in r.get("items", [])]}
                    else:
                        done_results[s.id] = {"script_title": (r.get("script") or {}).get("title"), "material_refs": (r.get("script") or {}).get("material_refs")}
            extra_ctx = (
                "续跑上下文（这是第 %d 次启动）：以下计划已存在，已完成步骤与费用已保留，失败步骤的重试计数已重置。"
                "当前计划：%s\n已完成步骤的结果：%s\n已花费：%s。请先 estimate_cost 再继续；不要重做已完成的步骤。"
                % (attempt_no, json.dumps(tools.plan_view(), ensure_ascii=False), json.dumps(done_results, ensure_ascii=False), tools.spent())
            )

    trace = Trace(out_dir / "trace.jsonl", run_id, attempt_no)
    trace.event("run_start", mode=mode, served_from=served_from, request=normalized(req), inject=args.inject, max_steps=args.max_steps, model=model_info.get("model"))

    # ---- 4. 决策循环
    result = run_loop(llm, tools, trace, req, max_steps=args.max_steps, extra_user_context=extra_ctx)

    # ---- 5. 终报（全部取自库）
    report = build_report(store, run_id, req, fp, tools, result, trace, mode, model_info, args.max_steps, served_from)
    store.update_run(run_id, status=result.status, steps_used=result.steps_used, result_json=report)
    trace.event("run_end", status=result.status, reason=result.reason, steps_used=result.steps_used, spent_cost=report["result"]["spent_cost"])
    report["trace_review"] = trace.review()  # 含 run_end 后的终态
    (out_dir / "trace_review.json").write_text(json.dumps(report["trace_review"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.quiet:
        (out_dir / "output.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        print(report["user_message"])
    else:
        _emit(report, out_dir)
    return 0


def main() -> None:
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
