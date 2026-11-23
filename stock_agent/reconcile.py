"""Reconcile the ledger with the broker (the broker is the source of truth).

``reconcile(ledger, reason=..., rebuild=False) -> BookSnapshot``

* Reads account, positions (stock and option rows), open orders and new
  account activities through ``ledger.broker`` (``stock_agent.broker.AlpacaClient``
  or anything with the same async methods). ANY read failure raises
  ``ReconcileError`` and leaves the ledger untouched; a failure is never
  read as "the account is empty".
* Broker quantities, average cost and cash always win. Shorts and fractions
  are kept as they are (signed ``Decimal``).
* Local metadata (position_id, thesis link, entry date, sector, stop) is
  preserved across quantity changes; a symbol that goes flat and reopens
  gets a new position_id.
* Every difference is a typed ``Diff``, journaled, stored in
  ``reconcile_runs`` and alerted when severity >= warning.
"""

from __future__ import annotations

from typing import Any, Protocol

from stock_agent.ledger import BookSnapshot, Ledger


class ReconcileError(Exception):
    """A broker read failed; nothing was changed."""


class BrokerReader(Protocol):
    async def get_account(self) -> Any: ...
    async def get_positions(self) -> list[Any]: ...
    async def list_orders(self, status: str = "open", **kw: Any) -> list[Any]: ...
    async def get_activities(self, activity_types: Any = ("FILL",), *, after: Any = None,
                             page_size: int = 100, max_pages: int = 20) -> list[dict]: ...


# Activity types ingested into broker_activities (OPXRC is the documented
# spelling of exercise; OPEXC is what the live account returns).
ACTIVITY_TYPES = ("FILL", "DIV", "FEE", "OPEXP", "OPASN", "OPEXC", "OPXRC", "OPTRD", "NC", "SC",
                  "SSP", "SSO", "MA", "REORG", "JNLC", "JNLS", "JNL", "CSD", "CSW", "INT")
CASH_FLOW_TYPES = ("JNLC", "CSD", "CSW", "JNL")

# a run that would close at least this share of >= 2 open positions re-reads once
MASS_CLOSE_FRACTION = 0.5


async def reconcile(ledger: Ledger, *, reason: str, rebuild: bool = False) -> BookSnapshot:
    raise NotImplementedError
