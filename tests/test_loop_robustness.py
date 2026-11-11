"""模型发来不合法的工具参数时，不能因此弄坏下一次请求。

2026-11-11 在最终候选源码的真实复验里遇到：模型第一步的参数是被截断的 JSON，
程序把它原样放回对话历史，百炼以 400 拒绝了下一次请求，任务以"模型接口调用失败"结束。
"""
import json

from src.llm import LLMReply, ToolCall
from src.loop import run_loop
from src.trace import Trace


class StrictFakeLLM:
    """模拟百炼的行为：对话历史里只要有一条工具调用的参数不是合法 JSON，就拒绝整个请求。"""

    name = "strict-fake"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, messages, tools):
        for m in messages:
            for tc in m.get("tool_calls") or []:
                json.loads(tc["function"]["arguments"])  # 不合法就抛异常，相当于接口返回 400
        self.calls += 1
        if self.calls == 1:
            # 少了结尾的 }：和真实复验里遇到的情况一样
            call = ToolCall("c1", "propose_plan", '{"reason": "r", "steps": [{"id": "s1", "action": "search_materials"}]')
        else:
            args = {"status": "incomplete", "summary": "s", "unfinished_reason": "测试到此为止", "reason": "r"}
            call = ToolCall(f"c{self.calls}", "finish", json.dumps(args, ensure_ascii=False))
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments_json}}],
        }
        return LLMReply(None, [call], message)


def test_malformed_tool_arguments_do_not_break_the_next_request(tools, req, tmp_path):
    llm = StrictFakeLLM()
    result = run_loop(llm, tools, Trace(tmp_path / "trace.jsonl", tools.run_id, 1), req, max_steps=5)

    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e for e in events if e["type"] == "llm_error"] == []  # 下一次请求没有被拒绝
    first_obs = next(e for e in events if e["type"] == "tool_result")
    assert first_obs["summary"]["error"] == "invalid_args"  # 模型被如实告知参数不合法
    assert llm.calls == 2 and result.reason == "finish_accepted" and result.steps_used == 2  # 循环继续，由模型决定下一步
