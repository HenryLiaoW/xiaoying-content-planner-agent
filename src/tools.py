"""七个模型动作：执行前校验 → 执行 → 记账。校验失败是"观察"，不是异常。

边界：这里不替模型做任何业务选择（不自动换档、不自动重试、不自动删步骤）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from .plan import DATA_DIR, MAX_ATTEMPTS_PER_STEP, CostTable, PlanError, apply_revision, build_plan, estimate, save_blockers
from .schema import (
    ACTION_MODELS,
    Estimate,
    FinishArgs,
    GenerateScriptArgs,
    LiteScript,
    Plan,
    ProposePlanArgs,
    Request,
    RevisePlanArgs,
    Scene,
    SearchMaterialsArgs,
    Step,
)
from .store import Store

Observation = dict[str, Any]


def _err(code: str, message: str, retryable: bool = False, **extra: Any) -> Observation:
    return {"ok": False, "error": code, "retryable": retryable, "message": message, **extra}


# ------------------------------------------------------------ 注入 ----

class Injector:
    """`--inject TOOL:first|always`；按该工具的实际尝试次数计数，只改变工具行为，不指挥模型。"""

    def __init__(self, specs: Optional[list[str]] = None):
        self.rules: dict[str, str] = {}
        for spec in specs or []:
            tool, _, mode = spec.partition(":")
            if mode not in ("first", "always"):
                raise ValueError(f"--inject 格式应为 TOOL:first|always，收到 {spec!r}")
            self.rules[tool] = mode
        self.attempts: dict[str, int] = {}

    def should_fail(self, tool: str) -> bool:
        n = self.attempts.get(tool, 0) + 1
        self.attempts[tool] = n
        mode = self.rules.get(tool)
        return mode == "always" or (mode == "first" and n == 1)


# ------------------------------------------------------------ 素材 ----

class Materials:
    def __init__(self, path: Optional[Path] = None):
        p = path or DATA_DIR / "materials.json"
        self.raw = json.loads(Path(p).read_text(encoding="utf-8"))
        self.items: list[dict[str, Any]] = self.raw["items"]
        self.by_id = {m["id"]: m for m in self.items}

    def search(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        q = query.strip()
        tokens = [t for t in q.replace("，", " ").replace(",", " ").split() if t]
        scored: list[tuple[int, dict[str, Any]]] = []
        for m in self.items:
            hay = " ".join([m["title"], *m["tags"], m["summary"]]).lower()
            score = sum(1 for t in tokens if t.lower() in hay)
            score += sum(2 for tag in m["tags"] if tag.lower() in q.lower())
            if score > 0:
                scored.append((score, m))
        scored.sort(key=lambda x: (-x[0], x[1]["id"]))
        return [
            {"id": m["id"], "type": m["type"], "title": m["title"], "summary": m["summary"], "tags": m["tags"]}
            for _, m in scored[:limit]
        ]


# ------------------------------------------------------- 脚本后端 ----

def template_script(topic: str, brief: str, tier: str, materials: list[dict[str, Any]]) -> dict[str, Any]:
    """确定性模板：基于素材 summary 填充。Mock 级生成，不评估内容质量。"""
    refs = [m["id"] for m in materials]
    copy_items = [m for m in materials if m["type"] == "copy"]
    visual_items = [m for m in materials if m["type"] in ("template", "clip")] or materials
    bgm = next((m for m in materials if m["type"] == "bgm"), None)
    title = f"{topic}｜{brief}"
    if tier == "lite":
        names = "、".join(m["title"] for m in materials[:3]) or "无素材"
        outline = f"用{names}串成{brief}，突出{topic}。"
        if len(outline) > 80:
            outline = outline[:77] + "…"
        return LiteScript(title=title, outline=outline, material_refs=refs).model_dump()

    hook = copy_items[0]["summary"] if copy_items else f"开头 3 秒点明「{topic}」的核心卖点。"
    scenes: list[Scene] = []
    for i, m in enumerate(visual_items[:3], start=1):
        narration = copy_items[(i - 1) % len(copy_items)]["summary"] if copy_items else f"围绕{brief}讲第 {i} 个要点。"
        scenes.append(Scene(no=i, visual=f"[{m['id']}] {m['summary']}", narration=narration))
    while len(scenes) < 3:
        n = len(scenes) + 1
        scenes.append(Scene(no=n, visual=f"产品界面特写，展示{topic}操作", narration=f"第 {n} 个要点：{brief}。"))
    cta = "现在打开小影，套用模板一键成片。" + (f"（配乐 {bgm['id']}）" if bgm else "")
    from .schema import ProScript

    return ProScript(title=title, hook=hook, scenes=scenes, cta=cta, material_refs=refs).model_dump()


# ------------------------------------------------------------ 工具 ----

class Tools:
    def __init__(
        self,
        store: Store,
        run_id: str,
        req: Request,
        table: Optional[CostTable] = None,
        materials: Optional[Materials] = None,
        injector: Optional[Injector] = None,
        script_backend: str = "template",
    ):
        self.store = store
        self.run_id = run_id
        self.req = req
        self.table = table or CostTable()
        self.materials = materials or Materials()
        self.injector = injector or Injector()
        self.script_backend = script_backend

        run = store.get_run(run_id)
        self.plan: Optional[Plan] = store.load_plan(run_id)
        self.sim_now: datetime = datetime.fromisoformat(run["sim_now"])
        self.last_estimate: Optional[Estimate] = store.latest_estimate(run_id)
        self.adjustments: list[dict[str, Any]] = []
        self.last_obs_kind: str = "none"  # 用于推导 revise 的触发原因
        self.business_exec_count = 0

    # ----------------------------------------------------- 入口 ----

    def execute(self, name: str, raw_args: dict[str, Any]) -> Observation:
        model = ACTION_MODELS.get(name)
        if model is None:
            return _err("unknown_action", f"没有名为 {name} 的动作")
        try:
            args = model.model_validate(raw_args)
        except ValidationError as e:
            errs = "; ".join(f"{'.'.join(str(p) for p in x['loc'])}: {x['msg']}" for x in e.errors()[:5])
            return _err("invalid_args", f"参数不合法：{errs}")
        handler = getattr(self, f"_{name}")
        obs = handler(args)
        self._remember(name, obs)
        return obs

    def _remember(self, name: str, obs: Observation) -> None:
        if name == "estimate_cost" and obs.get("ok"):
            if not obs["within_budget"]:
                self.last_obs_kind = "over_budget"
            elif not obs["within_deadline"]:
                self.last_obs_kind = "over_deadline"
            else:
                self.last_obs_kind = "estimate_ok"
        elif name in ("search_materials", "generate_script"):
            if not obs.get("ok"):
                self.last_obs_kind = "tool_failure" if obs.get("error") == "tool_error" else "precheck_rejected"
            elif name == "search_materials" and obs.get("count") == 0:
                self.last_obs_kind = "no_materials"
            else:
                self.last_obs_kind = "step_ok"
        elif name in ("propose_plan", "revise_plan", "save_plan", "finish"):
            self.last_obs_kind = f"{name}_{'ok' if obs.get('ok') else 'rejected'}"

    # --------------------------------------------------- 工具函数 ----

    def spent(self) -> float:
        return self.store.ledger_sum(self.run_id)

    def plan_view(self) -> dict[str, Any]:
        assert self.plan is not None
        return {
            "revision": self.plan.revision,
            "steps": [
                {
                    "id": s.id, "action": s.action, "tier": s.tier,
                    **({"query": s.query} if s.action == "search_materials" else {"brief": s.brief}),
                    "depends_on": s.depends_on,
                    "deliverable": s.deliverable.model_dump() if s.deliverable else None,
                    "status": s.status, "attempts": s.attempts, "cost": s.cost, "hours": s.hours,
                }
                for s in self.plan.steps
            ],
        }

    def _estimate_now(self) -> Estimate:
        assert self.plan is not None
        return estimate(self.plan, self.req.budget, self.req.deadline, self.sim_now, self.spent(), self.table)

    # ------------------------------------------------ 计划动作 ----

    def _propose_plan(self, args: ProposePlanArgs) -> Observation:
        if self.plan is not None:
            return _err("plan_exists", "计划已存在；要修改请用 revise_plan")
        try:
            plan = build_plan(self.run_id, args.steps, self.table)
        except PlanError as e:
            return _err(e.code, e.detail)
        self.plan = plan
        self.store.save_plan(plan, args.reason)
        return {"ok": True, "message": "计划已记录为 revision 1；执行前请先 estimate_cost", "plan": self.plan_view()}

    def _revise_plan(self, args: RevisePlanArgs) -> Observation:
        if self.plan is None:
            return _err("no_plan", "尚无计划，请先 propose_plan")
        if not (args.set_tier or args.set_query or args.set_depends or args.remove or args.add):
            return _err("empty_revision", "revise_plan 没有包含任何修改")
        try:
            new_plan, changes = apply_revision(self.plan, args, self.table)
        except PlanError as e:
            return _err(e.code, e.detail)
        before = self.last_estimate if (self.last_estimate and self.last_estimate.revision == self.plan.revision) else self._estimate_now()
        trigger = {
            "over_budget": "over_budget", "over_deadline": "over_deadline",
            "tool_failure": "tool_failure", "no_materials": "no_materials",
        }.get(self.last_obs_kind, "model_choice")
        self.plan = new_plan
        self.store.save_plan(new_plan, args.reason)
        after = self._estimate_now()
        self.adjustments.append(
            {
                "trigger": trigger,
                "from_revision": before.revision,
                "to_revision": new_plan.revision,
                "before": {"projected_total": before.projected, "projected_finish": before.projected_finish.isoformat()},
                "after": {"projected_total": after.projected, "projected_finish": after.projected_finish.isoformat()},
                "changes": changes,
                "model_reason": args.reason,
            }
        )
        return {
            "ok": True,
            "message": f"计划已更新为 revision {new_plan.revision}；执行前必须重新 estimate_cost",
            "changed": changes,
            "plan": self.plan_view(),
        }

    def _estimate_cost(self, args: Any) -> Observation:
        if self.plan is None:
            return _err("no_plan", "尚无计划，请先 propose_plan")
        est = self._estimate_now()
        self.last_estimate = est
        self.store.save_estimate(self.run_id, est)
        hint = []
        if not est.within_budget:
            hint.append(f"预计总费用 {est.projected:g} 超出预算 {est.budget:g}，超出 {est.over_budget_by:g}")
            if est.min_feasible_cost > est.budget:
                hint.append(f"最低可行费用 {est.min_feasible_cost:g} 仍高于预算，缺口 {est.min_feasible_cost - est.budget:g}；没有可行方案")
        if not est.within_deadline:
            hint.append(f"预计完成 {est.projected_finish.isoformat()} 晚于截止 {est.deadline.isoformat()}，超出 {est.over_time_hours:g} 小时")
        return {
            "ok": True,
            "revision": est.revision,
            "spent_cost": est.spent,
            "remaining_estimate": est.remaining,
            "projected_total": est.projected,
            "budget": est.budget,
            "within_budget": est.within_budget,
            "over_budget_by": est.over_budget_by,
            "sim_now": est.sim_now.isoformat(),
            "projected_finish": est.projected_finish.isoformat(),
            "deadline": est.deadline.isoformat(),
            "within_deadline": est.within_deadline,
            "over_time_hours": est.over_time_hours,
            "min_feasible_cost": est.min_feasible_cost,
            "min_feasible_hours": est.min_feasible_hours,
            "message": "；".join(hint) if hint else "在预算与截止时间内，可以执行",
        }

    # ------------------------------------------------ 业务动作 ----

    def _precheck(self, step_id: str, action: str) -> tuple[Optional[Step], Optional[Observation]]:
        if self.plan is None:
            return None, _err("no_plan", "尚无计划，请先 propose_plan")
        step = self.plan.get(step_id)
        if step is None:
            return None, _err("step_not_in_plan", f"步骤 {step_id} 不在当前计划（revision {self.plan.revision}）中")
        if step.action != action:
            return None, _err("action_mismatch", f"步骤 {step_id} 的动作是 {step.action}，不是 {action}")
        if step.status == "done":
            return step, None  # 调用方返回缓存结果
        not_done = [d for d in step.depends_on if (self.plan.get(d) or Step(id=d, action="search_materials")).status != "done"]
        if not_done:
            return None, _err("deps_not_done", f"依赖未完成：{not_done}", deps_not_done=not_done)
        if step.attempts >= MAX_ATTEMPTS_PER_STEP:
            return None, _err(
                "retry_limit_reached",
                f"步骤 {step_id} 已尝试 {step.attempts} 次，达到上限 {MAX_ATTEMPTS_PER_STEP}；可以换档位 / 改计划，或停止",
                attempts=step.attempts,
            )
        est = self.last_estimate
        if est is None or est.revision != self.plan.revision:
            return None, _err("estimate_required", f"当前计划 revision {self.plan.revision} 还没有估算，请先 estimate_cost")
        if not est.within_budget:
            return None, _err(
                "over_budget",
                f"按当前计划预计总费用 {est.projected:g} 超出预算 {est.budget:g}（超出 {est.over_budget_by:g}），拒绝执行",
                over_budget_by=est.over_budget_by, min_feasible_cost=est.min_feasible_cost,
            )
        if not est.within_deadline:
            return None, _err(
                "over_deadline",
                f"按当前计划预计完成 {est.projected_finish.isoformat()} 晚于截止，拒绝执行",
                over_time_hours=est.over_time_hours,
            )
        return step, None

    def _cached(self, step: Step) -> Observation:
        result = self.store.step_result(self.run_id, step.id) or {}
        return {"ok": True, "already_done": True, "step_id": step.id, "message": "该步骤已完成，返回已有结果，不重复计费", **result}

    def _run_step(self, step: Step, tool: str, produce) -> Observation:
        """统一的执行 + 记账：注入 / 失败不计费 / 成功记账并推进时钟。"""
        assert self.plan is not None
        self.business_exec_count += 1
        step.attempts += 1
        if self.injector.should_fail(tool):
            self.store.record_step_failure(self.run_id, step)
            return _err(
                "tool_error",
                f"{tool} 服务返回错误（注入故障），步骤 {step.id} 第 {step.attempts} 次尝试失败；未计费",
                retryable=step.attempts < MAX_ATTEMPTS_PER_STEP,
                step_id=step.id, attempts=step.attempts, attempts_left=MAX_ATTEMPTS_PER_STEP - step.attempts,
            )
        result = produce()
        at = self.sim_now
        self.sim_now = self.sim_now + timedelta(hours=step.hours)
        spent_after = self.spent() + step.cost
        self.store.record_step_success(self.run_id, step, result, at, self.sim_now, spent_after)
        return {
            "ok": True, "step_id": step.id, "status": "done", "charged": step.cost, "hours": step.hours,
            "spent_cost": spent_after, "sim_now": self.sim_now.isoformat(), **result,
        }

    def _search_materials(self, args: SearchMaterialsArgs) -> Observation:
        step, err = self._precheck(args.step_id, "search_materials")
        if err:
            return err
        assert step is not None
        if step.status == "done":
            return self._cached(step)
        query = args.query.strip() or (step.query or "")

        def produce() -> dict[str, Any]:
            items = self.materials.search(query)
            return {"query": query, "count": len(items), "items": items}

        obs = self._run_step(step, "search_materials", produce)
        if obs.get("ok") and obs.get("count") == 0:
            obs["message"] = "没有命中任何素材；可以换检索词重试（会再次计费），或停止"
        return obs

    def _generate_script(self, args: GenerateScriptArgs) -> Observation:
        step, err = self._precheck(args.step_id, "generate_script")
        if err:
            return err
        assert step is not None and self.plan is not None
        if step.status == "done":
            return self._cached(step)
        allowed: dict[str, dict[str, Any]] = {}
        for d in step.depends_on:
            dep = self.plan.get(d)
            if dep and dep.action == "search_materials":
                for m in (self.store.step_result(self.run_id, d) or {}).get("items", []):
                    allowed[m["id"]] = m
        unknown = [m for m in args.material_ids if m not in allowed]
        if unknown:
            return _err("unknown_material", f"素材 {unknown} 不在该步骤依赖的搜索结果中；可用：{sorted(allowed)}", allowed=sorted(allowed))
        if not args.material_ids:
            return _err("invalid_args", "material_ids 不能为空；请从依赖的搜索结果中选择素材")
        chosen = [allowed[m] for m in args.material_ids]
        tier = step.tier or "lite"
        brief = args.brief.strip() or (step.brief or "")

        def produce() -> dict[str, Any]:
            if self.script_backend != "template":
                raise PlanError("backend_unavailable", f"脚本后端 {self.script_backend} 未实现，当前仅支持 template")
            script = template_script(self.req.topic, brief, tier, chosen)
            return {"tier": tier, "deliverable": step.deliverable.model_dump() if step.deliverable else None, "script": script}

        try:
            return self._run_step(step, "generate_script", produce)
        except PlanError as e:
            return _err(e.code, e.detail)

    # ------------------------------------------------ 保存 / 结束 ----

    def _blockers(self) -> list[str]:
        assert self.plan is not None
        est = self.last_estimate
        return save_blockers(self.plan, est, self.spent(), self.sim_now, self.req.deadline, self.req.budget)

    def _save_plan(self, args: Any) -> Observation:
        if self.plan is None:
            return _err("no_plan", "尚无计划，请先 propose_plan")
        problems = self._blockers()
        if problems:
            return _err("not_saveable", "尚不满足保存条件：" + "；".join(problems), problems=problems)
        status = self.store.save_saved_plan(self.run_id, self.plan.revision, self.sim_now, self.adjustments)
        return {
            "ok": True, "saved": status, "revision": self.plan.revision,
            "spent_cost": self.spent(), "sim_finish": self.sim_now.isoformat(),
            "message": "方案已保存" if status == "created" else "该版本方案此前已保存，未重复写入",
        }

    def _finish(self, args: FinishArgs) -> Observation:
        if args.status == "completed":
            reasons: list[str] = []
            if self.plan is None:
                reasons.append("尚无计划")
            else:
                saved = self.store.get_saved_plan(self.run_id)
                if saved is None or saved["revision"] != self.plan.revision:
                    reasons.append("当前计划版本尚未 save_plan")
                reasons.extend(self._blockers())
            if reasons:
                return _err("finish_rejected", "不能以 completed 结束：" + "；".join(reasons), reasons=reasons)
            return {"ok": True, "accepted": True, "status": "completed"}
        if not (args.unfinished_reason and args.unfinished_reason.strip()):
            return _err("invalid_args", f"status={args.status} 必须提供 unfinished_reason")
        return {"ok": True, "accepted": True, "status": args.status, "unfinished_reason": args.unfinished_reason}
