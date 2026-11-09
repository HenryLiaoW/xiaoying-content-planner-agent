"""场景定义：输入、CLI 参数、默认 mock 脚本、期望（供测试与 run_examples 使用）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

BASE_TOPIC = "一键 Vlog 模板双11推广"
NOW = "2026-11-10T09:00:00+08:00"
DEADLINE = "2026-11-12T18:00:00+08:00"


def _input(request_id: str, **over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "request_id": request_id,
        "topic": BASE_TOPIC,
        "budget": 20,
        "deadline": DEADLINE,
        "now": NOW,
        "quality_preference": "balanced",
    }
    d.update(over)
    for k in [k for k, v in d.items() if v is None]:
        del d[k]
    return d


@dataclass
class Scenario:
    name: str
    input: dict[str, Any]
    covers: str
    mock_script: Optional[str] = None  # None → 不进入循环（入口层就返回）
    cli: list[str] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)
    real_path: bool = False  # 真实模型优先保证的路径


SCENARIOS: dict[str, Scenario] = {
    "normal": Scenario(
        "normal", _input("req-normal-001"), "正常完成", "normal",
        expect={"status": "completed", "spent_cost": 8.0, "saved": True}, real_path=True,
    ),
    "tool_failure_once": Scenario(
        "tool_failure_once", _input("req-tool-failure-once-001"), "工具失败一次 → 模型重试，保留成功结果",
        "tool_failure_once", cli=["--inject", "generate_script:first"],
        expect={"status": "completed", "spent_cost": 8.0, "saved": True, "attempts": {"s2": 2}}, real_path=True,
    ),
    "tool_failure_persistent": Scenario(
        "tool_failure_persistent", _input("req-tool-failure-persistent-001"), "工具持续失败 → 重试上限 → failed，不记成功",
        "tool_failure_persistent", cli=["--inject", "generate_script:always"],
        expect={"status": "failed", "spent_cost": 1.0, "saved": False, "attempts": {"s2": 2}},
    ),
    "over_budget": Scenario(
        "over_budget", _input("req-over-budget-001", budget=10, quality_preference="high"),
        "预算超限：12 → 重规划 → 8 → 保存", "over_budget",
        expect={"status": "completed", "spent_cost": 8.0, "saved": True, "adjust_trigger": "over_budget"},
    ),
    "infeasible": Scenario(
        "infeasible", _input("req-infeasible-001", budget=2), "无可行方案：返回未完成与缺口", "infeasible",
        expect={"status": "incomplete", "spent_cost": 0.0, "saved": False, "gap": 1.0},
    ),
    "missing_budget": Scenario(
        "missing_budget", _input("req-missing-budget-001", budget=None), "缺预算 → needs_input，不调模型",
        expect={"status": "needs_input", "missing_fields": ["budget"]},
    ),
    "missing_deadline": Scenario(
        "missing_deadline", _input("req-missing-deadline-001", deadline=None), "缺截止时间 → needs_input",
        expect={"status": "needs_input", "missing_fields": ["deadline"]},
    ),
    "invalid_input": Scenario(
        "invalid_input", _input("req-invalid-001", budget="10"), "格式错误 → invalid_input，退出码 2",
        expect={"status": "invalid_input", "exit_code": 2},
    ),
    "no_materials": Scenario(
        "no_materials", _input("req-no-materials-001", topic="水下摄影课程推广"), "首次检索无命中 → 模型补搜 → 完成",
        "no_materials", expect={"status": "completed", "spent_cost": 4.0, "saved": True},
    ),
    "deadline_tight": Scenario(
        "deadline_tight", _input("req-deadline-tight-001", deadline="2026-11-10T10:30:00+08:00"),
        "截止时间只剩 1.5h → pro 超期 → 模型改 lite", "deadline_tight",
        expect={"status": "completed", "spent_cost": 3.0, "saved": True, "adjust_trigger": "over_deadline"},
    ),
    "step_limit": Scenario(
        "step_limit", _input("req-step-limit-001"), "动作上限 → incomplete，可续跑", "normal", cli=["--max-steps", "4"],
        expect={"status": "incomplete", "finish_reason": "step_limit_reached", "saved": False},
    ),
}

REAL_PATHS = [s.name for s in SCENARIOS.values() if s.real_path]
