"""输入校验、规范化与指纹；计划 / 步骤 / 动作参数 / 脚本产物 / 估算的数据模型。

边界：这里只做"事实"层面的校验（类型、必填、取值范围），不做任何业务选择。
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

TZ_SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")

REQUIRED_FIELDS = ("topic", "budget", "deadline")
OPTIONAL_FIELDS = ("request_id", "now", "quality_preference")
ALLOWED_FIELDS = REQUIRED_FIELDS + OPTIONAL_FIELDS
QUALITY_LEVELS = ("high", "balanced", "low")

Quality = Literal["high", "balanced", "low"]
BusinessAction = Literal["search_materials", "generate_script"]
Tier = Literal["pro", "lite"]
StepStatus = Literal["pending", "running", "done", "failed", "skipped"]
RunStatus = Literal["running", "completed", "incomplete", "failed"]


# ---------------------------------------------------------------- 输入 ----

class InputError(Exception):
    """格式 / 类型 / 取值错误 → invalid_input（退出码 2）。"""


class NeedsInput(Exception):
    """必填字段缺失 → needs_input（退出码 0，不调模型、不建任务）。"""

    def __init__(self, missing: list[str]):
        super().__init__(f"缺少必填字段：{', '.join(missing)}")
        self.missing = missing


class Request(BaseModel):
    """校验通过后的请求。`now` 一定有值（输入缺省时取当前时间）。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str
    budget: float
    deadline: datetime
    now: datetime
    request_id: Optional[str] = None
    quality_preference: Quality = "balanced"


def parse_dt(value: str, field: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise InputError(f"{field} 不是合法的 ISO 8601 时间：{value!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_SHANGHAI)
    return dt


def check_input(raw: Any, default_now: Optional[datetime] = None) -> Request:
    """三段校验：格式/类型 → 必填缺失 → 取值。

    抛 InputError（invalid_input）或 NeedsInput（needs_input）。
    顺序：先查类型与未知字段（有错即 invalid），再查缺失（needs_input），最后查取值。
    """
    if not isinstance(raw, dict):
        raise InputError("输入必须是 JSON 对象")
    unknown = sorted(set(raw) - set(ALLOWED_FIELDS))
    if unknown:
        raise InputError(f"未知字段：{unknown}")

    if "topic" in raw and raw["topic"] is not None:
        if not isinstance(raw["topic"], str) or not raw["topic"].strip():
            raise InputError("topic 必须是非空字符串")
    if "budget" in raw and raw["budget"] is not None:
        if isinstance(raw["budget"], bool) or not isinstance(raw["budget"], (int, float)):
            raise InputError("budget 必须是数字（不接受字符串）")
    for f in ("deadline", "now", "request_id"):
        if f in raw and raw[f] is not None and not isinstance(raw[f], str):
            raise InputError(f"{f} 必须是字符串")
    if "quality_preference" in raw and raw["quality_preference"] is not None:
        if raw["quality_preference"] not in QUALITY_LEVELS:
            raise InputError(f"quality_preference 只能是 {list(QUALITY_LEVELS)}")

    missing = [f for f in REQUIRED_FIELDS if raw.get(f) is None]
    if missing:
        raise NeedsInput(missing)

    budget = float(raw["budget"])
    if not math.isfinite(budget) or budget < 0:
        raise InputError("budget 必须是 ≥ 0 的有限数")
    deadline = parse_dt(raw["deadline"], "deadline")
    if raw.get("now") is not None:
        now = parse_dt(raw["now"], "now")
    else:
        now = default_now or datetime.now(TZ_SHANGHAI)
    if deadline <= now:
        raise InputError("deadline 必须晚于 now")

    return Request(
        topic=raw["topic"],
        budget=budget,
        deadline=deadline,
        now=now,
        request_id=raw.get("request_id"),
        quality_preference=raw.get("quality_preference") or "balanced",
    )


def normalized(req: Request) -> dict[str, Any]:
    """只消除表示法差异：首尾空白、10 == 10.0、时区统一为 UTC。不 casefold、不折叠内部空白。"""
    budget: float | int = req.budget
    if budget == int(budget):
        budget = int(budget)
    return {
        "topic": req.topic.strip(),
        "budget": budget,
        "deadline": req.deadline.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "quality_preference": req.quality_preference,
    }


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(req: Request) -> str:
    """规范化字段的 SHA-256；`now` 与 `request_id` 不参与。"""
    return hashlib.sha256(canonical_json(normalized(req)).encode("utf-8")).hexdigest()


def identity_of(req: Request, fp: str, new_task: bool = False) -> str:
    """身份：有 request_id 用它；否则 fp:<指纹>；--new-task 且无 request_id 时派生新身份。"""
    if req.request_id:
        return req.request_id
    return f"fp:{fp}" + (f"#{uuid.uuid4().hex[:8]}" if new_task else "")


# ---------------------------------------------------------------- 计划 ----

class Deliverable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(description="交付键，如 main / teaser")
    required: bool = Field(default=False, description="必需交付物；恰好一个 key=main 且 required=true")


class StepSpec(BaseModel):
    """模型提交的步骤定义（propose_plan / revise_plan.add 用）。"""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,15}$", description="步骤编号，如 s1")
    action: BusinessAction
    tier: Optional[Tier] = Field(default=None, description="generate_script 必填：pro | lite；search_materials 留空")
    query: Optional[str] = Field(default=None, description="search_materials 的检索词")
    brief: Optional[str] = Field(default=None, description="generate_script 的脚本简述，如“主视频”")
    depends_on: list[str] = Field(default_factory=list, description="依赖的步骤编号；脚本步骤至少依赖一个搜索步骤")
    deliverable: Optional[Deliverable] = Field(default=None, description="generate_script 必填")


class Step(StepSpec):
    """运行态步骤：在 StepSpec 之上加状态、尝试次数、结果引用与费用。"""

    status: StepStatus = "pending"
    attempts: int = 0
    failures: dict[str, int] = Field(default_factory=dict, description="累计失败次数，按档位（或 search）分开记；换档位、续跑都不清零")
    result_ref: Optional[str] = None
    cost: float = 0.0
    hours: float = 0.0


class Plan(BaseModel):
    run_id: str
    revision: int
    steps: list[Step]

    def get(self, step_id: str) -> Optional[Step]:
        for s in self.steps:
            if s.id == step_id:
                return s
        return None

    def ids(self) -> set[str]:
        return {s.id for s in self.steps}


# ---------------------------------------------------------- 动作参数 ----

class ProposePlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[StepSpec] = Field(min_length=1)
    reason: str = Field(description="一句话决策依据")


class TierChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    tier: Tier


class QueryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    query: str


class DependsChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    depends_on: list[str]


class RevisePlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    set_tier: list[TierChange] = Field(default_factory=list, description="改脚本档位")
    set_query: list[QueryChange] = Field(default_factory=list, description="改搜索词")
    set_depends: list[DependsChange] = Field(default_factory=list, description="改依赖（例如删掉一个搜索步骤后把脚本改为依赖另一个搜索）")
    remove: list[str] = Field(default_factory=list, description="删除的步骤编号（只能删未完成且非必需的）")
    add: list[StepSpec] = Field(default_factory=list, description="新增步骤")
    reason: str


class EstimateCostArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class SearchMaterialsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    query: str
    reason: str


class GenerateScriptArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str
    material_ids: list[str] = Field(description="来自该步骤所依赖搜索结果的素材 id")
    brief: str
    reason: str


class SavePlanArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str


class FinishArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["completed", "incomplete", "failed"]
    summary: str = Field(description="给运营的一句话总结（不作为数字来源）")
    unfinished_reason: Optional[str] = Field(default=None, description="status 非 completed 时必填")
    reason: str


ACTION_MODELS: dict[str, type[BaseModel]] = {
    "propose_plan": ProposePlanArgs,
    "revise_plan": RevisePlanArgs,
    "estimate_cost": EstimateCostArgs,
    "search_materials": SearchMaterialsArgs,
    "generate_script": GenerateScriptArgs,
    "save_plan": SavePlanArgs,
    "finish": FinishArgs,
}

ACTION_DESCRIPTIONS: dict[str, str] = {
    "propose_plan": "提交初始计划（只能调用一次）。步骤含搜索与脚本；恰好一个 required=true 且 key=main 的主脚本，且它依赖至少一个搜索步骤。",
    "revise_plan": "修改当前计划：改档位 / 改搜索词 / 删步骤 / 加步骤。不能触碰已完成步骤；改完后必须重新 estimate_cost。",
    "estimate_cost": "对当前计划估算：已花费、剩余预计、预计总额、是否在预算与截止时间内、最低可行费用。执行计费动作前必须有当前版本的估算。",
    "search_materials": "执行一个搜索步骤，返回命中的素材列表（含 id 与 summary）。费用 1，工时 0.5h。",
    "generate_script": "执行一个脚本步骤。档位取自计划；material_ids 必须来自其依赖搜索步骤的返回。pro：费用 5 / 2h；lite：费用 2 / 1h。",
    "save_plan": "保存方案。要求：全部步骤完成、必需交付物齐、估算新鲜且在预算与截止时间内。",
    "finish": "结束任务。completed 要求方案已保存；否则给 unfinished_reason。",
}


# ---------------------------------------------------------- 脚本产物 ----

class Scene(BaseModel):
    no: int
    visual: str
    narration: str


class ProScript(BaseModel):
    tier: Literal["pro"] = "pro"
    title: str
    hook: str
    scenes: list[Scene] = Field(min_length=3)
    cta: str
    material_refs: list[str]


class LiteScript(BaseModel):
    tier: Literal["lite"] = "lite"
    title: str
    outline: str = Field(max_length=80)
    material_refs: list[str]


# ---------------------------------------------------------------- 估算 ----

class Estimate(BaseModel):
    revision: int
    spent: float
    remaining: float
    projected: float
    budget: float
    within_budget: bool
    over_budget_by: float
    sim_now: datetime
    projected_finish: datetime
    deadline: datetime
    within_deadline: bool
    over_time_hours: float
    min_feasible_cost: float
    min_feasible_hours: float
    cost_table_version: str


# ------------------------------------------------------- 工具 JSON Schema --

def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 pydantic 生成的 $ref/$defs 展开为内联 schema，兼容只接受扁平 schema 的接口。"""
    defs = schema.get("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                name = node["$ref"].rsplit("/", 1)[-1]
                return walk(defs[name])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    out = walk(schema)
    out.pop("title", None)
    return out


def tool_schemas() -> list[dict[str, Any]]:
    """OpenAI 工具调用格式的七个动作定义。"""
    tools = []
    for name, model in ACTION_MODELS.items():
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": ACTION_DESCRIPTIONS[name],
                    "parameters": _inline_refs(model.model_json_schema()),
                },
            }
        )
    return tools
