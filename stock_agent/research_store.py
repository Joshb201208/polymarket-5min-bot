"""Research-owned work and metering tables on the shared SQLite state store."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
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


def _migrate_retry(conn) -> None:
    conn.execute("ALTER TABLE research_work ADD COLUMN retry_after_s INTEGER")
    conn.execute("""CREATE TABLE research_option_ideas (
        story_id TEXT PRIMARY KEY, expires_at_s INTEGER NOT NULL, payload_json TEXT NOT NULL,
        handled INTEGER NOT NULL DEFAULT 0
    )""")


def _migrate_confirmation(conn) -> None:
    conn.execute("ALTER TABLE research_work ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1")
    conn.execute("""CREATE TABLE research_confirmations (
        work_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, position_id TEXT NOT NULL,
        due_at_s INTEGER NOT NULL, expires_at_s INTEGER NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'waiting',
        payload_json TEXT NOT NULL
    )""")


def _migrate_handoff(conn) -> None:
    # Research-only, additive; old versions do not read this table.
    conn.execute("""CREATE TABLE research_pending_buys (
        work_key TEXT PRIMARY KEY, symbol TEXT NOT NULL, session_date TEXT NOT NULL,
        thesis_id TEXT NOT NULL, due_at_s INTEGER NOT NULL, expires_at_s INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'waiting', details_json TEXT NOT NULL, intent_id TEXT
    )""")
    conn.execute("""CREATE INDEX research_pending_due
        ON research_pending_buys(status,due_at_s,expires_at_s)""")
    conn.execute("""CREATE UNIQUE INDEX research_pending_thesis
        ON research_pending_buys(symbol,session_date,thesis_id)""")


def _migrate_indirect_news(conn) -> None:
    conn.execute("""CREATE TABLE research_indirect_news (
        ny_date TEXT NOT NULL, symbol TEXT NOT NULL, story_id TEXT NOT NULL,
        PRIMARY KEY(ny_date, symbol)
    )""")


class ResearchStore:
    def __init__(self, store: StateStore, config):
        self.store = store
        self.config = config
        store.register_migration(1, "research", _migrate)
        store.register_migration(2, "research", _migrate_retry)
        store.register_migration(3, "research", _migrate_confirmation)
        store.register_migration(4, "research", _migrate_handoff)
        store.register_migration(5, "research", _migrate_indirect_news)

    def reserve_indirect_news(self, ny_date: str, symbol: str, story_id: str) -> bool:
        """One indirect trigger per unheld symbol/day; that story may retry."""
        with self.store.transaction() as conn:
            conn.execute("INSERT OR IGNORE INTO research_indirect_news VALUES (?,?,?)",
                         (ny_date, symbol, story_id))
            return conn.execute("SELECT story_id FROM research_indirect_news WHERE ny_date=? AND symbol=?",
                                (ny_date, symbol)).fetchone()[0] == story_id

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
                (route.provider, "" if provider_wide else route.model, until.astimezone(UTC).isoformat()),
            )

    def claim_work(self, work_key: str, symbol: str, now: datetime) -> bool:
        # An observed retryable failure can reopen work, with a hard cap.
        # An ambiguous process crash leaves 'started' and is never redrawn.
        with self.store.transaction() as conn:
            return bool(conn.execute(
                """INSERT INTO research_work(work_key,symbol,claimed_at,status)
                VALUES(?,?,?,'started') ON CONFLICT(work_key) DO UPDATE SET
                    claimed_at=excluded.claimed_at,status='started',retry_after_s=NULL,attempts=attempts+1
                WHERE status='retry' AND attempts<3 AND retry_after_s<=?""",
                (work_key, symbol, now.isoformat(), int(now.timestamp())),
            ).rowcount)

    def ready(self, work_key: str, now: datetime) -> bool:
        row = self.work_state(work_key)
        return row is None or (
            row["status"] == "retry" and row["attempts"] < 3 and row["retry_after_s"] <= int(now.timestamp())
        )

    def work_state(self, work_key: str):
        rows = self.store.query("SELECT * FROM research_work WHERE work_key=?", (work_key,))
        return rows[0] if rows else None

    def retry_story_payloads(self, now: datetime, kind: str) -> list[dict]:
        rows = self.store.query(
            """SELECT details_json FROM research_work
            WHERE status='retry' AND attempts<3 AND retry_after_s<=? ORDER BY claimed_at LIMIT 100""",
            (int(now.timestamp()),),
        )
        details = (json.loads(row[0]) for row in rows)
        return [detail["story"] for detail in details if detail.get("kind") == kind and detail.get("story")]

    def finish_work(self, work_key: str, status: str, *, thesis_id: str | None = None, detail: dict | None = None,
                    retry_at: datetime | None = None) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """UPDATE research_work SET status=?, thesis_id=?, details_json=?, retry_after_s=?
                WHERE work_key=? AND status IN ('started','confirmation_pending')""",
                (status, thesis_id, json.dumps(detail or {}, default=str),
                 int(retry_at.timestamp()) if retry_at is not None else None, work_key),
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

    def has_unqueued_exit(self, symbol: str, now: datetime) -> bool:
        rows = self.store.query(
            """SELECT w.details_json FROM research_work w
            WHERE w.symbol=? AND w.status='proposed' AND NOT EXISTS (
                SELECT 1 FROM order_intents i WHERE i.dedupe_key=w.work_key
            )""",
            (symbol,),
        )
        for row in rows:
            intent = json.loads(row[0])["intent"]
            if intent["side"] == "sell" and datetime.fromisoformat(intent["expires_at"]) > now:
                return True
        return False

    def has_pending_confirmation(self, symbol: str, now: datetime) -> bool:
        return bool(self.store.query(
            """SELECT 1 FROM research_confirmations
            WHERE symbol=? AND status IN ('waiting','checking') AND expires_at_s>? LIMIT 1""",
            (symbol, int(now.timestamp())),
        ))

    def schedule_confirmation(self, work_key: str, symbol: str, position_id: str, now: datetime,
                              expires_at: datetime, payload: dict) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO research_confirmations(work_key,symbol,position_id,due_at_s,expires_at_s,payload_json)
                VALUES(?,?,?,?,?,?)""",
                (work_key, symbol, position_id, int(now.timestamp()), int(expires_at.timestamp()),
                 json.dumps(payload, default=str)),
            )
            self.finish_work(work_key, "confirmation_pending", thesis_id=payload["thesis_id"])

    def due_confirmations(self, now: datetime):
        at = int(now.timestamp())
        return self.store.query(
            """SELECT * FROM research_confirmations
            WHERE status='waiting' AND due_at_s<=? AND expires_at_s>? ORDER BY due_at_s LIMIT 4""",
            (at, at),
        )

    def claim_confirmation(self, key: str, now: datetime):
        at = int(now.timestamp())
        with self.store.transaction() as conn:
            return conn.execute(
                """UPDATE research_confirmations SET status='checking',attempts=attempts+1
                WHERE work_key=? AND status='waiting' AND due_at_s<=? AND expires_at_s>? RETURNING *""",
                (key, at, at),
            ).fetchone()

    def finish_confirmation(self, key: str, status: str, *, retry_at: datetime | None = None) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """UPDATE research_confirmations SET status=?, due_at_s=COALESCE(?, due_at_s)
                WHERE work_key=? AND status IN ('waiting','checking')""",
                (status, int(retry_at.timestamp()) if retry_at is not None else None, key),
            )

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

    def stage_pending_buy(self, work_key: str, thesis_id: str, intent, anchor_kind: str) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO research_pending_buys
                   (work_key,symbol,session_date,thesis_id,due_at_s,expires_at_s,details_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (work_key, intent.symbol, intent.session_date.isoformat(), thesis_id,
                 int(intent.next_attempt_at.timestamp()), int(intent.expires_at.timestamp()),
                 json.dumps({"intent": asdict(intent), "anchor_kind": anchor_kind}, default=str)),
            )
            self.finish_work(work_key, "no_action", thesis_id=thesis_id)

    def pending_buys(self, now: datetime):
        # Include expiry past close so outages/restarts are reported, not silently lost.
        return self.store.query(
            """SELECT * FROM research_pending_buys WHERE status='waiting'
               AND due_at_s<=? ORDER BY due_at_s, work_key LIMIT 8""",
            (int(now.timestamp()),),
        )

    def pending_buy(self, key: str):
        rows = self.store.query("SELECT * FROM research_pending_buys WHERE work_key=?", (key,))
        return rows[0] if rows else None

    def supersede_pending_buys(self, symbol: str, session_date: str) -> int:
        with self.store.transaction() as conn:
            return conn.execute(
                """UPDATE research_pending_buys SET status='handoff_superseded'
                   WHERE symbol=? AND session_date=? AND status='waiting'""",
                (symbol, session_date),
            ).rowcount

    def protects_scheduled_buy(self, symbol: str, session_date: str) -> bool:
        return bool(self.store.query(
            """SELECT 1 FROM research_pending_buys p JOIN current_thesis t
               ON t.symbol=p.symbol AND t.thesis_id=p.thesis_id
               WHERE p.symbol=? AND p.session_date=? AND
               (p.status='waiting' OR (p.status='handoff_promoted' AND EXISTS (
                   SELECT 1 FROM order_intents i WHERE i.intent_id=p.intent_id AND i.status='queued'
               ))) LIMIT 1""",
            (symbol, session_date),
        ))

    def finish_pending_buy(self, key: str, status: str, *, intent_id: str | None = None,
                           retry_at: datetime | None = None, hold_reason: str | None = None) -> bool:
        with self.store.transaction() as conn:
            return bool(conn.execute(
                """UPDATE research_pending_buys SET status=?,intent_id=?,due_at_s=COALESCE(?,due_at_s),
                   details_json=CASE WHEN ? IS NULL THEN details_json ELSE json_set(details_json,'$.last_hold',?) END
                   WHERE work_key=? AND status='waiting'""",
                (status, intent_id, int(retry_at.timestamp()) if retry_at is not None else None,
                 hold_reason, hold_reason, key),
            ).rowcount)

    def record_option_idea(self, story_id: str, idea, expires_at: datetime) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO research_option_ideas(story_id,expires_at_s,payload_json) VALUES (?,?,?)""",
                (story_id, int(expires_at.timestamp()), json.dumps(asdict(idea), default=str)),
            )

    def pending_option_ideas(self, now: datetime) -> list[dict]:
        return [json.loads(row[0]) for row in self.store.query(
            """SELECT payload_json FROM research_option_ideas
            WHERE handled=0 AND expires_at_s>? ORDER BY rowid LIMIT 20""",
            (int(now.timestamp()),),
        )]

    def acknowledge_options(self, story_ids: tuple[str, ...]) -> None:
        with self.store.transaction() as conn:
            conn.executemany("UPDATE research_option_ideas SET handled=1 WHERE story_id=?",
                             ((story_id,) for story_id in story_ids))
