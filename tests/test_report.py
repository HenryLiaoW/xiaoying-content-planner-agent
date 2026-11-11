"""给运营的话：超期相关的说法只依据算得出来的事实，不把“当前计划超时”说成“怎么做都来不及”。"""
from datetime import datetime, timedelta

from src.plan import build_plan, estimate
from src.report import _timing, _user_message
from src.schema import StepSpec, check_input

NOW = datetime.fromisoformat("2026-11-10T09:00:00+08:00")


def _message(table, tier: str, hours_left: float, status: str, main_failed: bool = False) -> str:
    """一次搜索（0.5h）+ 主脚本（pro 2h / lite 1h），离截止还有 hours_left 小时；返回未完成时给运营的话。"""
    deadline = NOW + timedelta(hours=hours_left)
    req = check_input({"topic": "一键 Vlog 模板双11推广", "budget": 20, "deadline": deadline.isoformat(), "now": NOW.isoformat()})
    plan = build_plan("r", [
        StepSpec(id="s1", action="search_materials", query="Vlog 模板"),
        StepSpec(id="s2", action="generate_script", tier=tier, brief="主视频", depends_on=["s1"], deliverable={"key": "main", "required": True}),
    ], table)
    if main_failed:
        main = plan.get("s2")
        main.status, main.attempts, main.failures = "failed", 2, {tier: 2}
    est = estimate(plan, 20, deadline, NOW, 0.0, table)
    return _user_message(status, "finish_accepted", req, plan, est, None, 0.0, NOW, None, [], "模型原话", 15, _timing(est, deadline))


def test_says_too_late_only_when_even_the_fastest_option_misses_the_deadline(table):
    # 只剩 1 小时：最省时的做法（搜索 0.5h + 简版 1h）也要 1.5 小时
    msg = _message(table, "lite", 1.0, "incomplete")
    assert "还需要 1.5 小时" in msg and "差 0.5 小时" in msg and "放宽到 11-10 10:30 之后" in msg


def test_plan_over_deadline_but_faster_option_fits_is_not_called_too_late(table):
    # 还剩 1.5 小时：当前计划（完整版，共 2.5h）超时，但换简版（1.5h）刚好来得及
    msg = _message(table, "pro", 1.5, "incomplete")
    assert "未能生成方案" not in msg and "放宽" not in msg
    assert "按当前计划预计 11-10 11:30 完成，晚于截止时间 11-10 10:30" in msg and "最早可在 11-10 10:30 完成" in msg


def test_failed_run_reports_the_failure_not_the_deadline(table):
    # 同样是“当前计划超时”，但任务是因为主脚本连续失败而结束：应当说失败，不说来不及
    msg = _message(table, "pro", 1.5, "failed", main_failed=True)
    assert "主视频脚本生成累计失败 2 次" in msg and "放宽" not in msg and "来不及" not in msg


def test_no_deadline_remark_when_plan_fits(table):
    msg = _message(table, "lite", 8.0, "incomplete")
    assert "晚于截止时间" not in msg and "可用 --resume 继续" in msg
