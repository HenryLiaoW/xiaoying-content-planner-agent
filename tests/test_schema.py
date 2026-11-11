import pytest

from src.schema import InputError, NeedsInput, check_input, fingerprint, normalized

from conftest import BASE_INPUT


def _with(**over):
    d = dict(BASE_INPUT)
    for k, v in over.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    return d


@pytest.mark.parametrize("bad", [
    _with(budget="10"), _with(budget=True), _with(budget=-1), _with(budget=float("inf")),
    _with(deadline="not-a-date"), _with(deadline="2026-11-01T00:00:00+08:00"),  # 早于 now
    _with(topic="   "), _with(quality_preference="ultra"), _with(extra="x"), [], "str",
])
def test_invalid_input(bad):
    with pytest.raises(InputError):
        check_input(bad)


@pytest.mark.parametrize("missing", [["budget"], ["deadline"], ["topic"], ["budget", "deadline"]])
def test_needs_input_lists_missing(missing):
    with pytest.raises(NeedsInput) as e:
        check_input(_with(**{m: None for m in missing}))
    assert e.value.missing == missing


def test_type_error_wins_over_missing():
    with pytest.raises(InputError):
        check_input(_with(budget="10", deadline=None))


def test_now_defaults_to_current_time_when_absent():
    from datetime import datetime, timedelta, timezone

    before = datetime.now(timezone.utc)
    req = check_input(_with(now=None, deadline="2099-01-01T00:00:00+08:00"))
    after = datetime.now(timezone.utc)
    assert before - timedelta(seconds=1) <= req.now <= after + timedelta(seconds=1)  # 取的是系统当前时间
    assert req.now.utcoffset() is not None  # 带时区


def test_fingerprint_ignores_representation_only():
    base = fingerprint(check_input(BASE_INPUT))
    assert fingerprint(check_input(_with(budget=10.0))) == base
    assert fingerprint(check_input(_with(topic="  一键 Vlog 模板双11推广 "))) == base
    assert fingerprint(check_input(_with(deadline="2026-11-12T10:00:00Z"))) == base
    assert fingerprint(check_input(_with(now="2026-11-10T12:00:00+08:00"))) == base  # now 不进指纹
    assert fingerprint(check_input(_with(request_id="other"))) == base


def test_fingerprint_changes_on_semantic_difference():
    base = fingerprint(check_input(BASE_INPUT))
    assert fingerprint(check_input(_with(budget=12))) != base
    assert fingerprint(check_input(_with(topic="一键 vlog 模板双11推广"))) != base  # 不 casefold
    assert fingerprint(check_input(_with(quality_preference="low"))) != base


def test_normalized_shape():
    n = normalized(check_input(BASE_INPUT))
    assert n == {"topic": "一键 Vlog 模板双11推广", "budget": 10, "deadline": "2026-11-12T10:00:00+00:00", "quality_preference": "high"}


def test_check_input_does_not_modify_caller_input():
    """校验与规范化不改动调用方传入的原始输入。"""
    import copy

    raw = _with(topic="  一键 Vlog 模板双11推广  ", budget=10)
    before = copy.deepcopy(raw)
    check_input(raw)
    assert raw == before
