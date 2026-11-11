"""事件日志只追加：再次启动写同一个文件时，旧内容原样保留，序号接着往后排。"""
import json

from src.trace import Trace


def test_trace_file_is_append_only_across_restarts(tmp_path):
    path = tmp_path / "trace.jsonl"
    first = Trace(path, "run_x", 1)
    first.event("run_start")
    first.event("tool_result", ok=True)
    before = path.read_text(encoding="utf-8")

    second = Trace(path, "run_x", 2)  # 续跑时对同一个文件新建写入器
    second.event("run_start")
    after = path.read_text(encoding="utf-8")

    assert after.startswith(before) and len(after) > len(before)  # 旧内容一字未动，只在末尾追加
    events = [json.loads(line) for line in after.splitlines()]
    assert [e["seq"] for e in events] == [1, 2, 3] and [e["attempt_no"] for e in events] == [1, 1, 2]
