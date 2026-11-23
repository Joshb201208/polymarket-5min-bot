"""Open the journal, state store and ledger in one call (startup wiring).

Usage (scheduler startup)::

    boot = open_ledger(config.DATA_DIR, broker=self.broker, alert=self.alerts.alert)
    self.ledger = boot.ledger
    boot.ledger.mark_submitting_ambiguous()
    await boot.ledger.refresh("startup", rebuild=boot.needs_rebuild)

Defaults work with no owner action: a missing ``state.db`` next to a legacy
``portfolio.json`` is migrated automatically (``migrate_v0``); an unreadable
``state.db`` is quarantined and the ledger starts blocked until the first clean
rebuild from the broker clears the block (B1).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .journal import Journal
from .ledger import Ledger
from .migrate_v0 import MigrationReport, has_legacy_state, migrate_v0
from .state_store import StateStore, StoreHealth


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class Boot:
    ledger: Ledger
    health: StoreHealth
    migration: MigrationReport | None

    @property
    def needs_rebuild(self) -> bool:
        """True when the first refresh must rebuild from the broker (new or quarantined store)."""
        return bool(self.health.created or self.health.quarantined or self.ledger.trading_block())


def open_ledger(data_dir: Path, *, broker: Any = None, alert: Callable[..., Any] | None = None,
                options_importer: Any = None, clock: Callable | None = None) -> Boot:
    data_dir = Path(data_dir)
    journal = Journal(Path(os.getenv("JOURNAL_DIR") or data_dir / "journal"),
                      max_mb=_env_int("JOURNAL_MAX_MB", 500), clock=clock)
    store = StateStore(Path(os.getenv("STATE_DB_PATH") or data_dir / "state.db"), journal,
                       backup_keep=_env_int("STATE_BACKUP_KEEP", 14), clock=clock)
    health = store.open()
    migration = None
    if health.created and not health.quarantined and has_legacy_state(data_dir):
        migration = migrate_v0(data_dir, store, options_importer=options_importer)
        health.migrated_v0 = migration.ran
        health.notes.append(migration.summary())
    ledger = Ledger(store, journal, broker=broker, max_age_s=_env_int("RECONCILE_MAX_AGE_SEC", 120),
                    clock=clock, alert=alert)
    return Boot(ledger=ledger, health=health, migration=migration)
