"""Research-owned work and metering tables on the shared SQLite state store."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from stock_agent.llm_client import ModelRoute
from stock_agent.state_store import StateStore

NY = ZoneInfo("America/New_York")


def week_key(now: datetime) -> str:
    day = now.astimezone(NY).date()
    return (day - timedelta(days=day.weekday())).isoformat()


def _migrate(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS research_usage (
        request_id TEXT PRIMARY KEY, week TEXT NOT NULL,
        reserved_microusd INTEGER NOT NULL, settled_microusd INTEGER,
        exit_work INTEGER NOT NULL, created_at TEXT NOT NULL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS research_usage_week ON research_usage(week)")
    conn.execute("""CREATE TABLE IF NOT EXISTS research_work (
        work_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, claimed_at TEXT NOT NULL,
        status TEXT NOT NULL, thesis_id TEXT, details_json TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS research_exit_slots (
        session TEXT NOT NULL, position_id TEXT NOT NULL,
        PRIMARY KEY (session, position_id)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS research_cooldowns (
        provider TEXT NOT NULL, model TEXT NOT NULL, until_at TEXT NOT NULL,
        PRIMARY KEY (provider, model)
    )""")


class ResearchStore:
    def __init__(self, store: StateStore, config):
        self.store = store
        self.config = config
        store.register_migration(1, "research", _migrate)

    def available_budget(self, now: datetime) -> dict:
        rows = self.store.query(
            """SELECT COALESCE(sum(COALESCE(settled_microusd, reserved_microusd)), 0),
            COALESCE(sum(CASE WHEN exit_work=0 THEN COALESCE(settled_microusd, reserved_microusd) END), 0)
            FROM research_usage WHERE week=?""",
            (week_key(now),),
        )
        return {"week": week_key(now), "billed_or_reserved_microusd": rows[0][0], "entry_microusd": rows[0][1]}

    def reserve(self, request_id: str, now: datetime, ceiling_microusd: int, *, exit_work: bool) -> bool:
        if ceiling_microusd <= 0:
            raise ValueError("invalid research reservation")
        budget = int(Decimal(str(self.config.RESEARCH_WEEKLY_BUDGET_USD)) * 1_000_000)
        max_entries = budget * (100 - self.config.RESEARCH_EXIT_RESERVE_PCT) // 100
        week = week_key(now)
        with self.store.transaction() as conn:
            total, non_exit = conn.execute(
                """SELECT COALESCE(sum(COALESCE(settled_microusd, reserved_microusd)), 0),
                COALESCE(sum(CASE WHEN exit_work=0 THEN COALESCE(settled_microusd, reserved_microusd) END), 0)
                FROM research_usage WHERE week=?""",
                (week,),
            ).fetchone()
            if total + ceiling_microusd > budget or not exit_work and non_exit + ceiling_microusd > max_entries:
                return False
            conn.execute(
                "INSERT INTO research_usage VALUES(?,?,?,NULL,?,?)",
                (request_id, week, ceiling_microusd, int(exit_work), now.isoformat()),
            )
        return True

    def settle(self, request_id: str, actual_microusd: int | None) -> None:
        if actual_microusd is None:
            return  # Crash, timeout or no meter: preserve upper-bound charge.
        if actual_microusd < 0:
            raise ValueError("negative research cost")
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE research_usage SET settled_microusd=? WHERE request_id=? AND settled_microusd IS NULL",
                (actual_microusd, request_id),
            )

    def get_cooldown(self, route: ModelRoute) -> datetime | None:
        rows = self.store.query(
            "SELECT max(until_at) FROM research_cooldowns WHERE provider=? AND model IN (?, '')",
            (route.provider, route.model),
        )
        return datetime.fromisoformat(rows[0][0]) if rows[0][0] else None

    def set_cooldown(self, route: ModelRoute, until: datetime, *, provider_wide: bool) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO research_cooldowns VALUES(?,?,?)
                ON CONFLICT(provider,model) DO UPDATE SET until_at=max(until_at,excluded.until_at)""",
                (route.provider, "" if provider_wide else route.model, until.isoformat()),
            )

    def claim_work(self, work_key: str, symbol: str, now: datetime) -> bool:
        # Exactly once per stable input, including crashes during a paid call.
        # The status remains 'started' after an ambiguous crash; don't redraw.
        with self.store.transaction() as conn:
            return bool(conn.execute(
                """INSERT OR IGNORE INTO research_work(work_key,symbol,claimed_at,status)
                VALUES(?,?,?,'started')""",
                (work_key, symbol, now.isoformat()),
            ).rowcount)

    def work_state(self, work_key: str):
        rows = self.store.query("SELECT * FROM research_work WHERE work_key=?", (work_key,))
        return rows[0] if rows else None

    def finish_work(self, work_key: str, status: str, *, thesis_id: str | None = None, detail: dict | None = None) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE research_work SET status=?, thesis_id=?, details_json=? WHERE work_key=? AND status='started'",
                (status, thesis_id, json.dumps(detail or {}, default=str), work_key),
            )

    def unqueued_proposals(self) -> list[dict]:
        """Recover completed research when a restart happened before job enqueue."""
        rows = self.store.query(
            """SELECT w.details_json FROM research_work w
            WHERE w.status='proposed' AND NOT EXISTS (
                SELECT 1 FROM order_intents i WHERE i.dedupe_key=w.work_key
            ) ORDER BY w.claimed_at LIMIT 100""",
        )
        return [json.loads(row[0])["intent"] for row in rows]

    def reserve_exit(self, session: str, position_id: str) -> bool:
        with self.store.transaction() as conn:
            if conn.execute(
                "SELECT 1 FROM research_exit_slots WHERE session=? AND position_id=?", (session, position_id),
            ).fetchone():
                return False
            count = conn.execute("SELECT count(*) FROM research_exit_slots WHERE session=?", (session,)).fetchone()[0]
            if count >= self.config.RESEARCH_MAX_MODEL_EXITS_PER_SESSION:
                return False
            conn.execute("INSERT INTO research_exit_slots VALUES (?,?)", (session, position_id))
            return True

    def record_proposal(self, work_key: str, thesis_id: str, intent) -> None:
        self.finish_work(work_key, "proposed", thesis_id=thesis_id, detail={"intent": asdict(intent)})
