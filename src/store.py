"""SQLite 持久化：runs / plans / steps / estimates / ledger / saved_plans。

原则：状态变更、结果、账本在同一事务内提交；金额与状态只以库为准。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator, Optional

from .schema import Estimate, Plan, Step

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  identity TEXT NOT NULL UNIQUE,
  request_id TEXT,
  fingerprint TEXT NOT NULL,
  request_raw TEXT NOT NULL,
  request_norm TEXT NOT NULL,
  mode TEXT NOT NULL,
  status TEXT NOT NULL,
  attempt_no INTEGER NOT NULL DEFAULT 1,
  sim_now TEXT NOT NULL,
  spent_cost REAL NOT NULL DEFAULT 0,
  steps_used INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  result_json TEXT
);
CREATE TABLE IF NOT EXISTS plans (
  run_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  plan_json TEXT NOT NULL,
  reason TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, revision)
);
CREATE TABLE IF NOT EXISTS steps (
  run_id TEXT NOT NULL,
  step_id TEXT NOT NULL,
  action TEXT NOT NULL,
  tier TEXT,
  spec_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  result_json TEXT,
  cost REAL NOT NULL DEFAULT 0,
  hours REAL NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (run_id, step_id)
);
CREATE TABLE IF NOT EXISTS estimates (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  revision INTEGER NOT NULL,
  estimate_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS ledger (
  run_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  step_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  amount REAL NOT NULL,
  hours REAL NOT NULL,
  at TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS saved_plans (
  run_id TEXT NOT NULL,
  revision INTEGER NOT NULL,
  saved_at TEXT NOT NULL,
  adjustments_json TEXT,
  PRIMARY KEY (run_id, revision)
);
"""


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _now_iso() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            yield cur
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------ runs --

    def create_run(
        self,
        identity: str,
        request_id: Optional[str],
        fp: str,
        request_raw: dict[str, Any],
        request_norm: dict[str, Any],
        sim_now: datetime,
        mode: str,
    ) -> str:
        run_id = f"run_{uuid.uuid4().hex[:12]}"
        ts = _now_iso()
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO runs (run_id, identity, request_id, fingerprint, request_raw, request_norm, mode, "
                "status, attempt_no, sim_now, spent_cost, steps_used, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,'running',1,?,0,0,?,?)",
                (run_id, identity, request_id, fp, json.dumps(request_raw, ensure_ascii=False),
                 json.dumps(request_norm, ensure_ascii=False), mode, _iso(sim_now), ts, ts),
            )
        return run_id

    def get_run_by_identity(self, identity: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE identity = ?", (identity,)).fetchone()

    def get_run(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()

    def update_run(self, run_id: str, **fields: Any) -> None:
        if not fields:
            return
        if "sim_now" in fields and isinstance(fields["sim_now"], datetime):
            fields["sim_now"] = _iso(fields["sim_now"])
        if "result_json" in fields and not isinstance(fields["result_json"], (str, type(None))):
            fields["result_json"] = json.dumps(fields["result_json"], ensure_ascii=False)
        fields["updated_at"] = _now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.tx() as cur:
            cur.execute(f"UPDATE runs SET {cols} WHERE run_id = ?", (*fields.values(), run_id))

    # ----------------------------------------------------------- plans --

    def save_plan(self, plan: Plan, reason: str = "") -> None:
        """写入一个计划版本，并同步 steps 表（新增/更新现有步骤，删除本版本不再包含的未完成步骤）。"""
        ts = _now_iso()
        with self.tx() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO plans (run_id, revision, plan_json, reason, created_at) VALUES (?,?,?,?,?)",
                (plan.run_id, plan.revision, plan.model_dump_json(), reason, ts),
            )
            keep = tuple(plan.ids())
            placeholders = ",".join("?" * len(keep)) or "''"
            cur.execute(
                f"DELETE FROM steps WHERE run_id = ? AND step_id NOT IN ({placeholders}) AND status != 'done'",
                (plan.run_id, *keep),
            )
            for s in plan.steps:
                self._upsert_step(cur, plan.run_id, s, ts)

    def _upsert_step(self, cur: sqlite3.Cursor, run_id: str, s: Step, ts: str) -> None:
        spec = s.model_dump(include={"id", "action", "tier", "query", "brief", "depends_on", "deliverable"})
        cur.execute(
            "INSERT INTO steps (run_id, step_id, action, tier, spec_json, status, attempts, result_json, cost, hours, updated_at) "
            "VALUES (?,?,?,?,?,?,?,COALESCE((SELECT result_json FROM steps WHERE run_id=? AND step_id=?), NULL),?,?,?) "
            "ON CONFLICT(run_id, step_id) DO UPDATE SET action=excluded.action, tier=excluded.tier, spec_json=excluded.spec_json, "
            "status=excluded.status, attempts=excluded.attempts, cost=excluded.cost, hours=excluded.hours, updated_at=excluded.updated_at",
            (run_id, s.id, s.action, s.tier, json.dumps(spec, ensure_ascii=False), s.status, s.attempts,
             run_id, s.id, s.cost, s.hours, ts),
        )

    def load_plan(self, run_id: str) -> Optional[Plan]:
        """最新版本的计划结构 + steps 表中的当前状态。"""
        row = self.conn.execute(
            "SELECT plan_json FROM plans WHERE run_id = ? ORDER BY revision DESC LIMIT 1", (run_id,)
        ).fetchone()
        if not row:
            return None
        plan = Plan.model_validate_json(row["plan_json"])
        state = {
            r["step_id"]: r
            for r in self.conn.execute("SELECT * FROM steps WHERE run_id = ?", (run_id,)).fetchall()
        }
        for s in plan.steps:
            r = state.get(s.id)
            if r:
                s.status = r["status"]
                s.attempts = r["attempts"]
                s.cost = r["cost"]
                s.hours = r["hours"]
                s.result_ref = f"res_{run_id}_{s.id}" if r["result_json"] else None
        return plan

    def list_plans(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT revision, plan_json, reason, created_at FROM plans WHERE run_id = ? ORDER BY revision", (run_id,)
        ).fetchall()

    # ----------------------------------------------------------- steps --

    def step_result(self, run_id: str, step_id: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT result_json FROM steps WHERE run_id = ? AND step_id = ?", (run_id, step_id)
        ).fetchone()
        if row and row["result_json"]:
            return json.loads(row["result_json"])
        return None

    def record_step_success(
        self, run_id: str, step: Step, result: dict[str, Any], at: datetime, new_sim_now: datetime, spent_after: float
    ) -> None:
        """步骤成功：状态 done、写结果、记账、推进模拟时钟——同一事务。"""
        ts = _now_iso()
        with self.tx() as cur:
            step.status = "done"
            step.result_ref = f"res_{run_id}_{step.id}"
            self._upsert_step(cur, run_id, step, ts)
            cur.execute(
                "UPDATE steps SET result_json = ? WHERE run_id = ? AND step_id = ?",
                (json.dumps(result, ensure_ascii=False), run_id, step.id),
            )
            cur.execute(
                "INSERT INTO ledger (run_id, seq, step_id, kind, amount, hours, at) "
                "VALUES (?, (SELECT COALESCE(MAX(seq),0)+1 FROM ledger WHERE run_id = ?), ?, 'charge', ?, ?, ?)",
                (run_id, run_id, step.id, step.cost, step.hours, _iso(at)),
            )
            cur.execute(
                "UPDATE runs SET spent_cost = ?, sim_now = ?, updated_at = ? WHERE run_id = ?",
                (spent_after, _iso(new_sim_now), ts, run_id),
            )

    def record_step_failure(self, run_id: str, step: Step) -> None:
        """步骤失败：状态 failed、attempts+1；不记账、不推进时钟。"""
        ts = _now_iso()
        with self.tx() as cur:
            step.status = "failed"
            self._upsert_step(cur, run_id, step, ts)

    def set_step_status(self, run_id: str, step: Step) -> None:
        ts = _now_iso()
        with self.tx() as cur:
            self._upsert_step(cur, run_id, step, ts)

    # ------------------------------------------------------- estimates --

    def save_estimate(self, run_id: str, est: Estimate) -> None:
        with self.tx() as cur:
            cur.execute(
                "INSERT INTO estimates (run_id, seq, revision, estimate_json, created_at) "
                "VALUES (?, (SELECT COALESCE(MAX(seq),0)+1 FROM estimates WHERE run_id = ?), ?, ?, ?)",
                (run_id, run_id, est.revision, est.model_dump_json(), _now_iso()),
            )

    def latest_estimate(self, run_id: str) -> Optional[Estimate]:
        row = self.conn.execute(
            "SELECT estimate_json FROM estimates WHERE run_id = ? ORDER BY seq DESC LIMIT 1", (run_id,)
        ).fetchone()
        return Estimate.model_validate_json(row["estimate_json"]) if row else None

    def list_estimates(self, run_id: str) -> list[Estimate]:
        rows = self.conn.execute(
            "SELECT estimate_json FROM estimates WHERE run_id = ? ORDER BY seq", (run_id,)
        ).fetchall()
        return [Estimate.model_validate_json(r["estimate_json"]) for r in rows]

    # ---------------------------------------------------------- ledger --

    def ledger(self, run_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM ledger WHERE run_id = ? ORDER BY seq", (run_id,)).fetchall()

    def ledger_sum(self, run_id: str) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN kind='charge' THEN amount ELSE -amount END),0) AS s FROM ledger WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return float(row["s"])

    # ----------------------------------------------------- saved_plans --

    def save_saved_plan(self, run_id: str, revision: int, saved_at: datetime, adjustments: list[dict[str, Any]]) -> str:
        """返回 created | duplicate。"""
        try:
            with self.tx() as cur:
                cur.execute(
                    "INSERT INTO saved_plans (run_id, revision, saved_at, adjustments_json) VALUES (?,?,?,?)",
                    (run_id, revision, _iso(saved_at), json.dumps(adjustments, ensure_ascii=False)),
                )
            return "created"
        except sqlite3.IntegrityError:
            return "duplicate"

    def get_saved_plan(self, run_id: str) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM saved_plans WHERE run_id = ? ORDER BY revision DESC LIMIT 1", (run_id,)
        ).fetchone()

    # ------------------------------------------------------------ dump --

    def dump(self, run_id: str) -> dict[str, Any]:
        def rows(sql: str) -> list[dict[str, Any]]:
            return [dict(r) for r in self.conn.execute(sql, (run_id,)).fetchall()]

        return {
            "runs": rows("SELECT * FROM runs WHERE run_id = ?"),
            "plans": rows("SELECT run_id, revision, reason, created_at FROM plans WHERE run_id = ? ORDER BY revision"),
            "steps": rows("SELECT run_id, step_id, action, tier, status, attempts, cost, hours, updated_at FROM steps WHERE run_id = ? ORDER BY step_id"),
            "estimates": rows("SELECT run_id, seq, revision, created_at FROM estimates WHERE run_id = ? ORDER BY seq"),
            "ledger": rows("SELECT * FROM ledger WHERE run_id = ? ORDER BY seq"),
            "saved_plans": rows("SELECT * FROM saved_plans WHERE run_id = ?"),
        }

    def count(self, table: str) -> int:
        return int(self.conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])
