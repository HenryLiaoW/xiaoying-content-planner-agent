import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.plan import CostTable  # noqa: E402
from src.schema import Deliverable, Plan, Request, Step, check_input, fingerprint, identity_of, normalized  # noqa: E402
from src.store import Store  # noqa: E402
from src.tools import Injector, Tools  # noqa: E402

BASE_INPUT = {
    "request_id": "req-test-001",
    "topic": "一键 Vlog 模板双11推广",
    "budget": 10,
    "deadline": "2026-11-12T18:00:00+08:00",
    "now": "2026-11-10T09:00:00+08:00",
    "quality_preference": "high",
}

PLAN_12 = [  # 参考实例首版：2 搜索 + 2 pro = 12
    {"id": "s1", "action": "search_materials", "query": "Vlog 模板 双11"},
    {"id": "s2", "action": "search_materials", "query": "双11 促销 BGM"},
    {"id": "s3", "action": "generate_script", "tier": "pro", "brief": "主视频", "depends_on": ["s1", "s2"], "deliverable": {"key": "main", "required": True}},
    {"id": "s4", "action": "generate_script", "tier": "pro", "brief": "预热", "depends_on": ["s2"], "deliverable": {"key": "teaser", "required": False}},
]
REVISE_TO_8 = {"set_tier": [{"step_id": "s4", "tier": "lite"}], "set_depends": [{"step_id": "s4", "depends_on": ["s1"]}], "remove": ["s2"], "reason": "超预算 2"}


@pytest.fixture
def table() -> CostTable:
    return CostTable()


@pytest.fixture
def req() -> Request:
    return check_input(BASE_INPUT)


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(str(tmp_path / "t.db"))


def make_tools(store: Store, req: Request, inject: list[str] | None = None) -> Tools:
    fp = fingerprint(req)
    run_id = store.create_run(identity_of(req, fp), req.request_id, fp, dict(BASE_INPUT), normalized(req), req.now, "test")
    return Tools(store, run_id, req, injector=Injector(inject or []))


@pytest.fixture
def tools(store, req) -> Tools:
    return make_tools(store, req)


def run_cli_json(args: list[str], tmp_path: Path) -> tuple[dict, int]:
    """进程内调用 CLI，返回 (stdout JSON, exit code)。"""
    import contextlib
    import io

    from src.main import run_cli

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = run_cli(args)
    return json.loads(buf.getvalue()), code
