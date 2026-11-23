"""Options settings, read in exactly one place.

`OPTIONS_MODE` decides what the options module may do:

- ``defined_risk`` (code default): open and manage defined-risk debit structures.
- ``close_only``: never open; manage, exit and repair what exists.
- ``off``: no options orders at all, except the repair of short stock or of an
  orphan short leg, which is a mechanical fix and always runs.

The legacy ``OPTIONS_ENABLED=false`` maps to ``off`` when ``OPTIONS_MODE`` is
not set. Every other number here is a code default the owner never has to set.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


class OptionsMode(StrEnum):
    OFF = "off"
    CLOSE_ONLY = "close_only"
    DEFINED_RISK = "defined_risk"

    @property
    def may_open(self) -> bool:
        return self is OptionsMode.DEFINED_RISK

    @property
    def may_manage(self) -> bool:
        return self is not OptionsMode.OFF


@dataclass(frozen=True)
class OptionsSettings:
    mode: OptionsMode = OptionsMode.DEFINED_RISK
    # budgets (fractions of equity), lead decision A4
    max_loss_per_structure_pct: float = 0.03
    max_loss_total_pct: float = 0.10
    max_open_structures: int = 6
    # entry gate
    min_effective_conviction: int = 8
    single_option_min_conviction: int = 9
    # contract selection
    min_dte: int = 21
    max_dte: int = 45
    target_dte: int = 35
    min_width_pct: float = 0.05
    max_width_pct: float = 0.12
    min_reward_risk: float = 1.0
    max_quote_age_s: int = 120
    max_leg_spread_pct: float = 0.15
    min_open_interest: int = 100
    limit_slippage_frac: float = 0.25  # of the distance from mid to the natural price
    # exits
    profit_target_frac: float = 0.50  # of max profit (spreads) / of debit (single options)
    loss_limit_frac: float = 0.50  # of debit
    exit_trading_days_before_expiry: int = 2
    open_grace_minutes: int = 5
    # cool-down (trading days)
    cooldown_after_loss: int = 10
    cooldown_after_two_losses: int = 30
    # news-driven entries
    max_news_opens_per_session: int = 2
    # order handling
    fill_wait_seconds: int = 60
    fill_poll_seconds: float = 2.0


_LEGACY_FALSE = {"false", "0", "no", "off"}


def _get(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def resolve_mode(config: Any = None, environ: Mapping[str, str] | None = None) -> OptionsMode:
    """The single reader of OPTIONS_MODE / OPTIONS_ENABLED.

    Precedence: config.OPTIONS_MODE, then the environment's OPTIONS_MODE, then the
    legacy OPTIONS_ENABLED flag (false -> off), then the code default.
    An unknown value is loud and falls back to ``close_only``: never to opening.
    """
    env = os.environ if environ is None else environ
    raw = _get(config, "OPTIONS_MODE") or env.get("OPTIONS_MODE")
    if raw:
        value = str(raw).strip().lower()
        try:
            return OptionsMode(value)
        except ValueError:
            logger.error("OPTIONS_MODE=%r is not one of off|close_only|defined_risk; using close_only", raw)
            return OptionsMode.CLOSE_ONLY
    legacy_env = env.get("OPTIONS_ENABLED")
    if legacy_env is not None:
        return OptionsMode.OFF if legacy_env.strip().lower() in _LEGACY_FALSE else OptionsMode.DEFINED_RISK
    return OptionsMode.DEFINED_RISK


# Budgets have one numerical source: the risk settings on Config, when present.
_SHARED_BUDGETS = {
    "max_loss_per_structure_pct": "RISK_OPTION_MAX_LOSS_PCT",
    "max_loss_total_pct": "RISK_OPTION_TOTAL_LOSS_PCT",
    "max_open_structures": "RISK_OPTION_MAX_STRUCTURES",
}


def load_options_settings(config: Any = None, environ: Mapping[str, str] | None = None) -> OptionsSettings:
    base = OptionsSettings()
    shared: dict[str, Any] = {}
    for field_name, cfg_name in _SHARED_BUDGETS.items():
        v = _get(config, cfg_name)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            shared[field_name] = type(getattr(base, field_name))(v)
    return replace(base, mode=resolve_mode(config, environ), **shared)
