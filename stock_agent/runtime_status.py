"""Local process status for systemd supervision and in-place deploy checks."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from stock_agent.market_calendar import MarketState, MarketStatus

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]


def notify_systemd(message: str) -> None:
    destination = os.environ.get("NOTIFY_SOCKET")
    if not destination:
        return
    if destination.startswith("@"):
        destination = "\0" + destination[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
            client.settimeout(0.2)
            client.sendto(message.encode(), destination)
    except OSError:
        LOGGER.warning("Could not notify the service supervisor")


def release_sha(root: Path = ROOT) -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"], cwd=root, capture_output=True, text=True,
            check=True, timeout=2,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class RuntimeStatus:
    def __init__(
        self,
        path: Path = ROOT / "data/runtime-health.json",
        *,
        sha: str | None = None,
        trial_metrics_provider: Callable[[], Mapping[str, Any] | None] | None = None,
    ):
        self.path = path
        self.sha = release_sha() if sha is None else sha
        self.trial_metrics_provider = trial_metrics_provider

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> RuntimeStatus:
        # Cron and the systemd service agree on this path without moving the ledger.
        path = os.environ.get("STOCK_AGENT_RUNTIME_STATUS")
        return cls(Path(path) if path else data_dir / "runtime-health.json")

    def _collect_trial_metrics(self) -> dict[str, Any] | None:
        provider = self.trial_metrics_provider
        if provider is None:
            return None
        try:
            payload = provider()
        except Exception:
            LOGGER.debug("Runtime trial metrics provider failed", exc_info=True)
            return None
        return dict(payload) if isinstance(payload, Mapping) else None

    def deployment_event(self) -> dict | None:
        try:
            state = json.loads(self.path.with_name("deploy-status.json").read_text())
            event = state.get("last_event") if isinstance(state, dict) else None
            if (isinstance(event, dict) and event.get("active_sha") == self.sha
                    and event.get("event") in {"deployed", "rollback"}
                    and isinstance(event.get("at"), str)):
                return event
        except (OSError, ValueError):
            pass
        return None

    def publish(
        self, market: MarketState, *, ready: bool, reconciled: bool, entries_enabled: bool,
    ) -> None:
        record = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
            "release_sha": self.sha,
            "ready": ready,
            "reconciled": reconciled,
            "entries_enabled": entries_enabled,
            "market_source": market.source.value,
            "market_open": None if market.status is MarketStatus.UNKNOWN else market.status is MarketStatus.OPEN,
            "market_observed_at": market.observed_at.isoformat() if market.observed_at else None,
            "market_next_open": market.next_open.isoformat() if market.next_open else None,
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w") as file:
                trial_metrics = self._collect_trial_metrics()
                if trial_metrics is not None:
                    record["trial_metrics"] = trial_metrics
                json.dump(record, file)
            tmp.replace(self.path)
        except OSError:
            LOGGER.warning("Could not publish local trading service health")
