"""模型客户端：OpenAI 兼容接口（百炼 qwen-plus）与同接口的 MockLLM。

MockLLM 只替换"模型"这一层；工具、存储、循环、校验全部真跑。
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_json: str

    def arguments(self) -> dict[str, Any] | None:
        try:
            v = json.loads(self.arguments_json or "{}")
        except json.JSONDecodeError:
            return None
        return v if isinstance(v, dict) else None


@dataclass
class LLMReply:
    content: Optional[str]
    tool_calls: list[ToolCall]
    assistant_message: dict[str, Any]
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int = 0


class LLMClient(Protocol):
    name: str

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply: ...


# ------------------------------------------------------- 真实客户端 ----

class OpenAICompatClient:
    """百炼 / 任何 OpenAI 兼容端点。temperature 0，不开 thinking，SDK timeout 60s、max_retries 1。"""

    name = "openai-compat"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        timeout: float = 60.0,
        max_retries: int = 1,
    ):
        from openai import OpenAI  # 延迟导入：mock 模式不需要

        self.base_url = base_url or os.environ.get("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        api_key = api_key or os.environ.get("LLM_API_KEY")
        if not api_key:
            raise RuntimeError("缺少 LLM_API_KEY（请在 .env 中设置），或改用 --mock 离线运行")
        self.model = model or os.environ.get("LLM_MODEL", "qwen-plus")
        self.temperature = temperature
        self.client = OpenAI(base_url=self.base_url, api_key=api_key, timeout=timeout, max_retries=max_retries)
        self.total_usage: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.calls = 0

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        t0 = time.time()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=self.temperature,
            extra_body={"enable_thinking": False},
        )
        latency = int((time.time() - t0) * 1000)
        self.calls += 1
        msg = resp.choices[0].message
        tool_calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments_json=tc.function.arguments or "{}")
            for tc in (msg.tool_calls or [])
        ]
        assistant_message: dict[str, Any] = {"role": "assistant", "content": msg.content}
        if tool_calls:
            assistant_message["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments_json}}
                for tc in tool_calls
            ]
        usage: dict[str, int] = {}
        if resp.usage:
            usage = {
                "prompt_tokens": resp.usage.prompt_tokens or 0,
                "completion_tokens": resp.usage.completion_tokens or 0,
                "total_tokens": resp.usage.total_tokens or 0,
            }
            for k, v in usage.items():
                self.total_usage[k] += v
        return LLMReply(content=msg.content, tool_calls=tool_calls, assistant_message=assistant_message, usage=usage, latency_ms=latency)


# ------------------------------------------------------------- Mock ----

class MockMismatch(AssertionError):
    """Mock 脚本期望的上一条观察与实际不符——说明流程行为变了，测试应当失败。"""


# 脚本步骤：静态 (tool, args[, expect_prev]) 或 callable(prev_obs, history) -> (tool, args)
MockStep = dict[str, Any] | Callable[[Optional[dict[str, Any]], list[dict[str, Any]]], tuple[str, dict[str, Any]]]


class MockLLM:
    """按脚本依次发出工具调用。每一步可声明 expect_prev（上一条观察必须包含的键值），用于核对流程。

    离线 Mock 只验证流程与状态机，不代表真实模型的决策效果。
    """

    name = "mock"

    def __init__(self, script: list[MockStep], label: str = "mock"):
        self.script = list(script)
        self.label = label
        self.pos = 0
        self.calls = 0
        self.total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.history: list[dict[str, Any]] = []  # 收到的观察

    @staticmethod
    def _last_observation(messages: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        for m in reversed(messages):
            if m.get("role") == "tool":
                try:
                    return json.loads(m["content"])
                except (json.JSONDecodeError, TypeError):
                    return {"_raw": m.get("content")}
            if m.get("role") == "assistant":
                break
        return None

    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMReply:
        self.calls += 1
        prev = self._last_observation(messages)
        if prev is not None:
            self.history.append(prev)
        if self.pos >= len(self.script):
            return LLMReply(content="(mock 脚本已结束)", tool_calls=[], assistant_message={"role": "assistant", "content": "(mock 脚本已结束)"})
        step = self.script[self.pos]
        self.pos += 1
        if callable(step):
            tool, args = step(prev, self.history)
        else:
            expect = step.get("expect_prev")
            if expect is not None:
                if prev is None:
                    raise MockMismatch(f"[{self.label}] 第 {self.pos} 步期望上一条观察 {expect}，但没有观察")
                for k, v in expect.items():
                    if prev.get(k) != v:
                        raise MockMismatch(f"[{self.label}] 第 {self.pos} 步期望上一条观察 {k}={v!r}，实际 {prev.get(k)!r}（{prev.get('error')} {prev.get('message','')[:80]}）")
            tool, args = step["tool"], step["args"]
        call_id = f"call_mock_{self.pos}"
        args_json = json.dumps(args, ensure_ascii=False)
        assistant_message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": call_id, "type": "function", "function": {"name": tool, "arguments": args_json}}],
        }
        return LLMReply(content=None, tool_calls=[ToolCall(call_id, tool, args_json)], assistant_message=assistant_message)
