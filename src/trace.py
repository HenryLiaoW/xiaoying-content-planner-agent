"""追加式事件日志（JSONL）与审阅版导出。

事件只追加、不回写。审阅版的"后续动作"由后一条真实事件推导：没有后续写 "(interrupted)"。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SUMMARY_KEYS = (
    "ok", "error", "message", "already_done", "status", "charged", "spent_cost", "sim_now",
    "revision", "projected_total", "remaining_estimate", "within_budget", "within_deadline",
    "over_budget_by", "over_time_hours", "min_feasible_cost", "count", "attempts", "attempts_left",
    "saved", "accepted", "changed", "deps_not_done", "problems", "reasons", "allowed", "unfinished_reason",
)


def summarize(obs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: obs[k] for k in SUMMARY_KEYS if k in obs}
    if "items" in obs:
        out["item_ids"] = [m.get("id") for m in obs["items"]]
    if "script" in obs and isinstance(obs["script"], dict):
        s = obs["script"]
        out["script"] = {"tier": s.get("tier"), "title": s.get("title"), "scenes": len(s.get("scenes", [])) or None, "material_refs": s.get("material_refs")}
    if "plan" in obs and isinstance(obs["plan"], dict):
        out["plan"] = {"revision": obs["plan"].get("revision"), "steps": [f"{s['id']}:{s['action']}{'/' + s['tier'] if s.get('tier') else ''}={s['status']}" for s in obs["plan"].get("steps", [])]}
    return out


class Trace:
    def __init__(self, path: Path | str, run_id: str, attempt_no: int):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.attempt_no = attempt_no
        self.seq = 0
        if self.path.exists():
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        self.seq = max(self.seq, json.loads(line).get("seq", 0))

    def event(self, type_: str, **fields: Any) -> dict[str, Any]:
        self.seq += 1
        ev = {
            "seq": self.seq,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "attempt_no": self.attempt_no,
            "type": type_,
            **fields,
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        return ev

    # ---------------------------------------------------------- 导出 ----

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def review(self) -> list[dict[str, Any]]:
        """题目要求的五要素：步骤、工具参数、返回摘要、后续动作、决策依据。"""
        events = self.read()
        actions = [e for e in events if e["type"] == "model_action"]
        results = {e["step_no"]: e for e in events if e["type"] == "tool_result"}
        terminal = next((e for e in events if e["type"] in ("limit_reached", "run_end")), None)
        review: list[dict[str, Any]] = []
        for i, a in enumerate(actions):
            res = results.get(a["step_no"])
            if i + 1 < len(actions):
                nxt = actions[i + 1]
                next_action = f"{nxt['tool']}({_brief_args(nxt.get('args', {}))})"
            elif res and a["tool"] == "finish" and res.get("summary", {}).get("accepted"):
                next_action = "(end)"
            elif terminal and terminal["type"] == "limit_reached":
                next_action = "(step limit reached)"
            else:
                next_action = "(interrupted)"
            review.append(
                {
                    "step": a["step_no"],
                    "attempt_no": a.get("attempt_no"),
                    "tool": a["tool"],
                    "args": {k: v for k, v in a.get("args", {}).items() if k != "reason"},
                    "result_summary": res.get("summary") if res else None,
                    "next_action": next_action,
                    "decision_basis": a.get("reason"),
                }
            )
        return review


def _brief_args(args: dict[str, Any]) -> str:
    parts = []
    for k, v in args.items():
        if k == "reason":
            continue
        s = json.dumps(v, ensure_ascii=False)
        parts.append(f"{k}={s if len(s) <= 40 else s[:37] + '…'}")
    return ", ".join(parts)
