from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from enum import StrEnum
from typing import Protocol

from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot, OpenOrder
from stock_agent.research_gate import OwnerEntryProof

DRAWDOWN_STATE_KEY = "drawdown"
_ZERO = Decimal(0)
# Known leveraged ETFs/ETPs; never infer leverage from ticker suffixes.
_LEVERAGED_2X = frozenset("""
    AGQ BOIL CONL DDM DIG DUG DXD EUO GLL KOLD LTL MSTU MSTX NVDL NVDU
    NVDX QLD QID ROM RXL SDS SSO SRS TSLI TSLL TSLT TSLQ
    TWM UCO UGL USD URE UWM UVIX YCS ZSL
""".split())
_LEVERAGED_3X = frozenset("""
    BULZ BNKU CURE DPST DRN DRV DUSL EDZ EDC FAS FAZ FNGU FNGD GUSH HIBL HIBS
    LABU LABD MIDU NAIL RETL SOXL SOXS SPXL SPXS SQQQ TECL TECS TMF TMV TNA TQQQ
    TZA UDOW UMDD UPRO URTY USLV UTSL WEBL WEBS YANG YINN
""".split())


class TradeKind(StrEnum):
    BUY = "buy"
    ADD = "add"
    SELL = "sell"
    BUY_TO_COVER = "buy_to_cover"
    OPTION_OPEN = "option_open"
    OPTION_CLOSE = "option_close"
    OPTION_DELIVERY = "option_delivery"
    CASH_SECURED_PUT = "cash_secured_put"
    COVERED_CALL = "covered_call"


class CanonicalSector(StrEnum):
    ENERGY = "Energy"
    MATERIALS = "Materials"
    INDUSTRIALS = "Industrials"
    CONSUMER_DISCRETIONARY = "Consumer Discretionary"
    CONSUMER_STAPLES = "Consumer Staples"
    HEALTH_CARE = "Health Care"
    FINANCIALS = "Financials"
    INFORMATION_TECHNOLOGY = "Information Technology"
    COMMUNICATION_SERVICES = "Communication Services"
    UTILITIES = "Utilities"
    REAL_ESTATE = "Real Estate"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class Classification:
    issuer: str
    sector: CanonicalSector
    clusters: tuple[str, ...] = ()


@dataclass(frozen=True)
class TradeIntent:
    kind: TradeKind
    symbol: str
    qty: Decimal = Decimal(0)
    price: Decimal = Decimal(0)
    mark_price: Decimal | None = None
    sector: str | None = None
    fee: Decimal = Decimal(0)
    raw_conviction: int | None = None
    effective_conviction: int | None = None
    last_filled_conviction: int | None = None
    minimum_conviction: int = 7
    stop_distance: Decimal | None = None
    analysis_price: Decimal | None = None
    signal_at: datetime | None = None
    quote_at: datetime | None = None
    option_max_loss: Decimal = Decimal(0)
    option_cash_debit: Decimal = Decimal(0)
    assignment_shares: Decimal = Decimal(0)
    assignment_strike: Decimal = Decimal(0)
    structure_id: str | None = None
    reason: str | None = None
    thesis_change: str | None = None
    initial_reaffirmation: bool = False


@dataclass(frozen=True)
class OptionCommitment:
    structure_id: str
    max_loss: Decimal
    cash_reserved: Decimal = Decimal(0)
    order_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DrawdownState:
    # The high water is measured on a flow-adjusted index in first-close units.
    high_water: Decimal = Decimal(0)
    drawdown: Decimal = Decimal(0)
    paused: bool = False
    recovery_closes: int = 0
    last_session: str | None = None
    last_equity: Decimal | None = None
    rolling_closes: tuple[tuple[str | None, Decimal], ...] = ()
    rolling_drawdown: Decimal | None = None
    pause_start_session: str | None = None
    pause_sessions_governed: int = 0

    @property
    def decision_drawdown(self) -> Decimal:
        return self.drawdown if self.rolling_drawdown is None else self.rolling_drawdown

    @property
    def rolling_high_water(self) -> Decimal:
        return max((close for _, close in self.rolling_closes), default=self.high_water)

    @property
    def throttled(self) -> bool:
        return self.is_throttled()

    @property
    def alert(self) -> bool:
        return self.should_alert()

    def is_throttled(self, config=Config) -> bool:
        # Preserve the shipped exact Decimal boundary at 15%; the research
        # float reference uses a 1e-10 guard against binary rounding.
        return self.paused or self.decision_drawdown >= _decimal(config.DRAWDOWN_THROTTLE_PCT)

    def should_alert(self, config=Config) -> bool:
        return self.decision_drawdown >= _decimal(config.DRAWDOWN_ALERT_PCT)

    def pause_sessions_remaining(self, config=Config) -> int:
        if not self.paused:
            return 0
        return max(0, config.DRAWDOWN_PAUSE_MAX_SESSIONS - self.pause_sessions_governed)

    def to_record(self) -> dict[str, object]:
        """Serialize as the v3 record; all decimals are exact strings.

        A state built directly with only the legacy fields is written as its
        Addendum 2 migration (synthetic peak + last close; a latch starts an episode).
        """
        if self.last_session is not None and not self.rolling_closes:
            return self._seeded().to_record()
        return {
            "version": 3,
            "high_water": str(self.high_water),
            "drawdown": str(self.drawdown),
            "paused": self.paused,
            "recovery_closes": self.recovery_closes,
            "last_session": self.last_session,
            "last_equity": None if self.last_equity is None else str(self.last_equity),
            "rolling_closes": [[label, str(close)] for label, close in self.rolling_closes],
            "rolling_drawdown": None if self.rolling_drawdown is None else str(self.rolling_drawdown),
            "pause_start_session": self.pause_start_session,
            "pause_sessions_governed": self.pause_sessions_governed,
        }

    def to_legacy_record(self, config=Config) -> dict[str, str | bool | int | None]:
        """Conservative v2 projection for a rollback to pre-v3 code (never looser).

        Keeps the all-time high-water/drawdown, last session and raw equity so the
        old code chains the next close correctly. The old code cannot represent a
        capped episode, so it pauses whenever v3 is paused or the all-time drawdown
        is at/above the pause line (the old latch), and the recovery streak restarts.
        """
        if self.last_session is None:
            return {"version": 1, "high_water": "0", "drawdown": "0", "paused": False,
                    "recovery_closes": 0, "last_session": None}
        record = {
            "version": 2 if self.last_equity is not None else 1,
            "high_water": str(self.high_water),
            "drawdown": str(self.drawdown),
            "paused": self.paused or self.drawdown >= _decimal(config.DRAWDOWN_PAUSE_PCT),
            "recovery_closes": 0,
            "last_session": self.last_session,
        }
        if self.last_equity is not None:
            record["last_equity"] = str(self.last_equity)
        return record

    @classmethod
    def from_record(cls, record: Mapping | None, *, config=Config) -> "DrawdownState":
        """Parse v1/v2/v3; any inconsistency raises ValueError (callers fail closed)."""
        if record is None:
            return cls()
        if not isinstance(record, Mapping) or type(record.get("version")) is not int or record["version"] not in (1, 2, 3):
            raise ValueError("unrecognized drawdown record")
        if record["version"] == 3:
            return cls._from_v3(record, config)
        legacy = cls._from_legacy(record, config)
        return cls() if legacy.last_session is None else legacy._seeded()

    def _seeded(self) -> "DrawdownState":
        # Addendum 2 migration: the old high-water is one synthetic prior close, so
        # day-one decisions equal the old ones; an old latch starts a capped episode.
        last_close = self.high_water * (1 - self.drawdown)
        return replace(
            self,
            rolling_closes=((None, self.high_water), (self.last_session, last_close)),
            rolling_drawdown=self.drawdown,
            pause_start_session=self.last_session if self.paused else None,
            pause_sessions_governed=0,
        )

    @classmethod
    def _from_legacy(cls, record: Mapping, config) -> "DrawdownState":
        high, loss = _decimal(record.get("high_water")), _decimal(record.get("drawdown"))
        paused, closes = record.get("paused"), record.get("recovery_closes")
        session = record.get("last_session")
        last_equity = _decimal(record.get("last_equity")) if record["version"] == 2 else None
        if (high < 0 or not 0 <= loss < 1 or type(paused) is not bool or type(closes) is not int
                or not 0 <= closes < config.DRAWDOWN_RECOVERY_SESSIONS
                or (not paused and (closes or loss >= _decimal(config.DRAWDOWN_PAUSE_PCT)))
                or (session is None and (high or loss or paused or closes))
                or (session is not None and (high <= 0 or _session_id(session) != session))
                or (last_equity is not None and (session is None or last_equity <= 0))):
            raise ValueError("invalid drawdown record")
        return cls(high, loss, paused, closes, session, last_equity)

    @classmethod
    def _from_v3(cls, record: Mapping, config) -> "DrawdownState":
        def fail(why: str) -> ValueError:
            return ValueError(f"invalid drawdown record: {why}")

        def label(value: object) -> str:
            if not isinstance(value, str) or _session_id(value) != value:
                raise fail("session label")
            return value

        def optional(value: object) -> Decimal | None:
            return None if value is None else _decimal(value)

        expected = {"version", "high_water", "drawdown", "paused", "recovery_closes", "last_session",
                    "last_equity", "rolling_closes", "rolling_drawdown", "pause_start_session",
                    "pause_sessions_governed"}
        if set(record) != expected:
            raise fail("fields")
        paused, closes = record["paused"], record["recovery_closes"]
        governed, raw_window = record["pause_sessions_governed"], record["rolling_closes"]
        if (type(paused) is not bool or type(closes) is not int or type(governed) is not int
                or not isinstance(raw_window, list)):
            raise fail("types")
        high, loss = _decimal(record["high_water"]), _decimal(record["drawdown"])
        rolling, last_equity = optional(record["rolling_drawdown"]), optional(record["last_equity"])
        session = None if record["last_session"] is None else label(record["last_session"])
        start = None if record["pause_start_session"] is None else label(record["pause_start_session"])
        window: list[tuple[str | None, Decimal]] = []
        for i, item in enumerate(raw_window):
            if not isinstance(item, list) or len(item) != 2:
                raise fail("rolling close")
            name = None if item[0] is None and i == 0 else label(item[0])
            window.append((name, _decimal(item[1])))
        if not 0 <= closes < config.DRAWDOWN_RECOVERY_SESSIONS or governed < 0 or (closes and not paused):
            raise fail("recovery or episode counters")
        if session is None:
            if (high or loss or paused or closes or window or rolling is not None or last_equity is not None
                    or start is not None or governed):
                raise fail("empty record carries state")
            return cls()
        labels = [name for name, _ in window if name is not None]
        tolerance = Decimal("1e-12")
        if (not labels or labels[-1] != session or any(a >= b for a, b in zip(labels, labels[1:]))
                or len(window) > config.DRAWDOWN_ROLLING_SESSIONS or any(v <= 0 for _, v in window)):
            raise fail("rolling window")
        if high <= 0 or not 0 <= loss < 1 or rolling is None or not 0 <= rolling < 1 or (
                last_equity is not None and last_equity <= 0):
            raise fail("values")
        last_close, peak = window[-1][1], max(v for _, v in window)
        if (abs(rolling - (peak - last_close) / peak) > tolerance or peak > high * (1 + tolerance)
                or abs(loss - (high - last_close) / high) > tolerance or rolling > loss + tolerance):
            raise fail("drawdowns disagree with closes")
        # Only a v1 migration (synthetic peak + one real close, never advanced) lacks raw equity;
        # any other null would silently re-anchor returns across earlier external flows.
        if last_equity is None and (len(window) != 2 or window[0][0] is not None):
            raise fail("missing last raw equity")
        if start is None:
            # Without any episode anchor a close above the pause line always latches.
            if paused or governed or rolling > _decimal(config.DRAWDOWN_PAUSE_PCT) + Decimal("1e-10"):
                raise fail("episode without anchor")
        else:
            # update_drawdown adds one per applied trusted close after the latch, capped; the
            # window labels are exactly the applied closes, so the clock is recomputable.
            cap = config.DRAWDOWN_PAUSE_MAX_SESSIONS
            after = sum(1 for name in labels if name > start)
            if start > session or governed > cap or (paused and governed >= cap):
                raise fail("episode clock")
            if start in labels:
                if governed != min(after, cap):
                    raise fail("episode clock")
            elif start > labels[0] or not min(after, cap) <= governed:
                # An anchor between retained closes is missing; one before the window has
                # at least every retained close counted (exact count unknowable if short).
                raise fail("episode clock")
        return cls(high, loss, paused, closes, session, last_equity, tuple(window), rolling, start, governed)


@dataclass(frozen=True)
class OwnerOptionScope:
    """Exact owner option-book structure already matched to the ledger claim."""

    symbol: str
    added: date
    positions: tuple[tuple[str, int], ...]
    order_ids: frozenset[str] = frozenset()

    def matches_order(self, order: OpenOrder) -> bool:
        """Parent ID alone is not sufficient; broker legs must match the structure."""
        return (
            order.order_class == "mleg" and order.asset_class != "us_equity"
            and (order.order_id in self.order_ids or order.client_order_id in self.order_ids)
            and sorted(leg.symbol for leg in order.legs) == sorted(symbol for symbol, _ in self.positions)
        )


@dataclass(frozen=True)
class RiskControls:
    pause_new_buys: bool = False
    drawdown: DrawdownState = field(default_factory=DrawdownState)
    option_commitments: tuple[OptionCommitment, ...] = ()
    pending_sectors: Mapping[str, str] = field(default_factory=dict)
    holding_convictions: Mapping[str, int] = field(default_factory=dict)
    owner_entry: OwnerEntryProof | None = None
    owner_option_scope: OwnerOptionScope | None = None


@dataclass(frozen=True)
class MarginStatus:
    equity: Decimal
    maintenance_margin: Decimal
    excess_pct: Decimal
    needs_trim: bool
    margin_call: bool


@dataclass(frozen=True)
class PortfolioTotals:
    equity: Decimal
    free_cash: Decimal
    gross_exposure: Decimal
    issuer_values: Mapping[str, Decimal]
    sector_values: Mapping[CanonicalSector, Decimal]
    cluster_values: Mapping[str, Decimal]
    positions: int
    option_max_loss: Decimal
    option_structures: int
    overnight_buying_power: Decimal | None = None
    maintenance_margin: Decimal | None = None


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    reasons: tuple[str, ...] = ()
    after: PortfolioTotals | None = None
    detect_only: bool = False
    allowed_existing_breaches: tuple["Breach", ...] = ()


@dataclass(frozen=True)
class Breach:
    kind: str
    key: str
    value: Decimal
    cap: Decimal
    at_limit: bool = False


@dataclass(frozen=True)
class StopDistance:
    fraction: Decimal
    fallback: bool


@dataclass(frozen=True)
class _Stock:
    symbol: str
    qty: Decimal
    mark: Decimal
    classification: Classification
    available: Decimal
    stop: Decimal | None


@dataclass
class _Projection:
    equity: Decimal
    cash: Decimal
    stocks: dict[str, _Stock] = field(default_factory=dict)
    pending_values: dict[str, tuple[Classification, Decimal]] = field(default_factory=dict)
    option_loss: Decimal = Decimal(0)
    option_count: int = 0
    unknown_pending: Decimal = Decimal(0)
    unpriced: dict[str, Decimal] = field(default_factory=dict)
    regt_buying_power: Decimal | None = None
    options_buying_power: Decimal | None = None
    maintenance: Decimal | None = None
    margin_call: bool = False

    def reserve(self, amount: Decimal) -> None:
        self.cash -= amount
        if self.regt_buying_power is not None:
            self.regt_buying_power -= amount
        if self.options_buying_power is not None:
            self.options_buying_power -= amount

    def totals(self) -> PortfolioTotals:
        by_issuer: dict[str, Decimal] = {}
        by_sector: dict[CanonicalSector, Decimal] = {}
        by_cluster: dict[str, Decimal] = {}
        for c, value in [
            *((stock.classification, stock.qty * stock.mark)
              for stock in self.stocks.values() if stock.qty > 0),
            *self.pending_values.values(),
        ]:
            by_issuer[c.issuer] = by_issuer.get(c.issuer, Decimal(0)) + value
            by_sector[c.sector] = by_sector.get(c.sector, Decimal(0)) + value
            for cluster in c.clusters:
                by_cluster[cluster] = by_cluster.get(cluster, Decimal(0)) + value
        symbols = {stock.symbol for stock in self.stocks.values() if stock.qty > 0}
        symbols.update(self.pending_values)
        symbols.update(symbol for symbol, qty in self.unpriced.items() if qty > 0)
        return PortfolioTotals(
            self.equity, self.cash, sum(by_issuer.values(), Decimal(0)),
            by_issuer, by_sector, by_cluster, len(symbols),
            self.option_loss, self.option_count, self.regt_buying_power, self.maintenance,
        )

    def add_buy(self, symbol: str, c: Classification, qty: Decimal, price: Decimal, mark: Decimal) -> None:
        prior = self.pending_values.get(symbol)
        notional = qty * max(price, mark)
        self.pending_values[symbol] = (c, notional + (prior[1] if prior else 0))
        self.reserve(qty * price)
        if self.maintenance is not None:
            self.maintenance += notional * _maintenance_rate(symbol, min(price, mark))


def _maintenance_rate(symbol: str, price: Decimal) -> Decimal:
    if price < Decimal("2.50"):
        return Decimal(1)
    if symbol in _LEVERAGED_3X:
        return Decimal(".75")
    if symbol in _LEVERAGED_2X:
        return Decimal(".50")
    return Decimal(".30")


class DailyBar(Protocol):
    @property
    def high(self) -> Decimal | float: ...

    @property
    def low(self) -> Decimal | float: ...

    @property
    def close(self) -> Decimal | float: ...


@dataclass(frozen=True)
class ShadowEntryAnchor:
    position_id: str
    symbol: str
    session_date: date
    fill_price: Decimal
    qty: Decimal
    stop_fraction: Decimal
    stop_price: Decimal | None = None
    atr: Decimal | None = None
    source: str = ""


@dataclass(frozen=True)
class ShadowDailyBar:
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class ShadowStopResult:
    variant: str
    stopped: bool
    exit_session: date | None
    price: Decimal
    return_fraction: Decimal
    pnl: Decimal
    through_session: date


def shadow_stop_results(
    entry_anchor: ShadowEntryAnchor, daily_bars: Sequence[ShadowDailyBar],
) -> tuple[ShadowStopResult, ...]:
    """Illustrative per-lot replay. Same-price-basis bars, no fees or intraday slippage.

    Skip the entry-day bar; its low could predate the real fill. Use an opening
    gap price when at/below the stop, otherwise the stop level when a later
    session's low touches it. Untriggered variants mark at the final close.
    """
    try:
        fill, qty = _decimal(entry_anchor.fill_price), _decimal(entry_anchor.qty)
        fraction = _decimal(entry_anchor.stop_fraction)
        if min(fill, qty, fraction) <= 0 or fraction >= 1:
            return ()
        atr_level = _decimal(entry_anchor.stop_price) if entry_anchor.stop_price is not None else stop_price(
            fill, StopDistance(fraction, False),
        )
        if not 0 < atr_level < fill:
            return ()
        bars: dict[date, tuple[Decimal, Decimal, Decimal]] = {}
        for bar in daily_bars:
            if bar.session_date <= entry_anchor.session_date:
                continue
            opening, high, low, close = map(_decimal, (bar.open, bar.high, bar.low, bar.close))
            if not (0 < low <= min(opening, close) <= max(opening, close) <= high):
                return ()
            values = opening, low, close
            if bar.session_date in bars and bars[bar.session_date] != values:
                return ()
            bars[bar.session_date] = values
        if not bars:
            return ()
        sessions = sorted(bars)
        last = sessions[-1]
        results = []
        levels = [(f"fixed_{pct}", fill * (1 - Decimal(pct) / 100)) for pct in (7, 10, 12)]
        levels += [("atr_placed", atr_level), ("no_stop", None)]
        for name, level in levels:
            price = bars[last][2]
            exit_session = None
            if level is not None:
                for session in sessions:
                    opening, low, _ = bars[session]
                    if low <= level:
                        price = min(opening, level)
                        exit_session = session
                        break
            results.append(ShadowStopResult(
                name, exit_session is not None, exit_session, price, price / fill - 1,
                (price - fill) * qty, last,
            ))
        return tuple(results)
    except (TypeError, ValueError, ArithmeticError, AttributeError):
        return ()


def normalize_sector(raw: str | None) -> CanonicalSector:
    """Collapse provider and older ledger strings into the same sector."""
    if not isinstance(raw, str):
        return CanonicalSector.UNKNOWN
    key = "".join(char for char in raw.casefold() if char.isalnum())
    return _SECTOR_NAMES.get(key, CanonicalSector.UNKNOWN)


def classify_symbol(symbol: str, raw_sector: str | None = None) -> Classification:
    """Use one issuer identity, retaining all overlapping named clusters."""
    symbol = symbol.strip().upper()
    canonical = _ISSUER_ALIASES.get(symbol, symbol)
    aliases = {alias for alias, issuer in _ISSUER_ALIASES.items() if issuer == canonical}
    aliases.update((symbol, canonical))
    sector = _SECTOR_OVERRIDES.get(canonical, normalize_sector(raw_sector))
    if sector == CanonicalSector.UNKNOWN:
        sector = normalize_sector(Config.SECTOR_MAP.get(symbol) or Config.SECTOR_MAP.get(canonical))
    clusters = tuple(name for name, members in Config.SYMBOL_CLUSTERS.items()
                     if aliases.intersection(members))
    return Classification(canonical, sector, clusters)


def _owner_stock_issuer_clear(snapshot: BookSnapshot, owner: OwnerEntryProof,
                              scope: OwnerOptionScope | None) -> bool:
    issuer = classify_symbol(owner.symbol).issuer
    if any(row.qty != 0 and classify_symbol(row.symbol).issuer == issuer for row in snapshot.holdings):
        return False
    if not isinstance(scope, OwnerOptionScope) or (scope.symbol, scope.added) != (owner.symbol, owner.added):
        scope = None

    def same_issuer_order(order: OpenOrder) -> bool:
        symbol = order.symbol
        occ_suffix = symbol[-15:]
        is_occ = (len(symbol) > 15 and occ_suffix[:6].isdigit()
                  and occ_suffix[6].upper() in ("C", "P") and occ_suffix[7:].isdigit())
        return order.remaining_qty > 0 and (
            classify_symbol(symbol).issuer == issuer
            or ((order.asset_class == "us_option" or is_occ) and classify_symbol(symbol[:-15]).issuer == issuer)
        )

    for order in snapshot.open_orders:
        if (same_issuer_order(order) or any(same_issuer_order(leg) for leg in order.legs)) and (
            scope is None or not scope.matches_order(order)
        ):
            return False
    positions = [(row.occ_symbol, row.qty) for row in snapshot.legs
                 if row.qty != 0 and classify_symbol(row.underlying).issuer == issuer]
    # Only the reconciled signed positions of the exact structure may remain.
    # Do not mask extra contracts, naked legs or other option positions.
    return not positions or (scope is not None and sorted(positions) == sorted(scope.positions))


_SECTOR_NAMES = {
    "".join(char for char in sector.value.casefold() if char.isalnum()): sector
    for sector in CanonicalSector
}
_SECTOR_NAMES.update({
    "technology": CanonicalSector.INFORMATION_TECHNOLOGY,
    "financialservices": CanonicalSector.FINANCIALS,
    "finance": CanonicalSector.FINANCIALS,
    "consumercyclical": CanonicalSector.CONSUMER_DISCRETIONARY,
    "consumerdefensive": CanonicalSector.CONSUMER_STAPLES,
    "basicmaterials": CanonicalSector.MATERIALS,
})
_ISSUER_ALIASES = {"GOOG": "GOOGL", "BRK.A": "BRK.B", "BRK-A": "BRK.B", "BRK-B": "BRK.B"}
_SECTOR_OVERRIDES = {
    "GOOGL": CanonicalSector.COMMUNICATION_SERVICES,
    "META": CanonicalSector.COMMUNICATION_SERVICES,
}


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a financial quantity")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid financial quantity") from exc
    if not number.is_finite():
        raise ValueError("financial quantity must be finite")
    return number


def atr14(bars: Sequence[DailyBar]) -> Decimal | None:
    """Wilder ATR on completed, sorted, split-adjusted daily bars."""
    period = Config.STOP_ATR_PERIOD
    if len(bars) <= period:
        return None
    ranges = []
    previous = None
    try:
        for bar in bars:
            high, low, close = (_decimal(bar.high), _decimal(bar.low), _decimal(bar.close))
            if not 0 < low <= close <= high:
                return None
            if previous is not None:
                ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
            previous = close
    except (ValueError, AttributeError):
        return None
    value = sum(ranges[:period], Decimal(0)) / period
    for true_range in ranges[period:]:
        value = (value * (period - 1) + true_range) / period
    return value


def stop_fraction(price: Decimal, atr: Decimal | None) -> StopDistance:
    price = _decimal(price)
    if price <= 0:
        raise ValueError("stop reference price must be positive")
    minimum = _decimal(Config.STOP_LOSS_PCT_MIN)
    maximum = _decimal(Config.STOP_LOSS_PCT_MAX)
    try:
        value = _decimal(atr)
    except ValueError:
        return StopDistance(minimum, True)
    if value < 0:
        return StopDistance(minimum, True)
    return StopDistance(
        min(maximum, max(minimum, _decimal(Config.STOP_ATR_MULTIPLE) * value / price)), False
    )


def stop_price(price: Decimal, distance: StopDistance) -> Decimal:
    """Round up to the valid price tick so the stop never risks extra distance."""
    price = _decimal(price)
    if price <= 0:
        raise ValueError("stop reference price must be positive")
    value = price * (1 - distance.fraction)
    tick = Decimal("0.01") if value >= 1 else Decimal("0.0001")
    return value.quantize(tick, rounding=ROUND_CEILING)


def update_drawdown(
    state: DrawdownState, session: str, close_equity: Decimal, *,
    previous_session: str | None = None,
    net_external_flow: Decimal = Decimal(0),
    config=Config,
) -> DrawdownState:
    """Chain trusted close returns, treating signed outside flows as interval-end.

    ``net_external_flow`` includes only external deposits (+) and withdrawals
    (-) after the last applied close. Cut-over equity starts a new index;
    legacy v1 state used raw equity, so its implied prior index is also its
    prior raw equity. Replayed sessions do not apply a flow twice.
    """
    session = _session_id(session)
    if previous_session is not None:
        previous_session = _session_id(previous_session)
        if previous_session >= session:
            raise ValueError("expected previous close must precede the new close")
    if not session or (state.last_session is not None and session <= state.last_session):
        return state
    close_equity = _decimal(close_equity)
    if close_equity <= 0:
        raise ValueError("drawdown requires positive trusted equity")
    adjusted_close = close_equity
    history = state.rolling_closes
    if state.last_session is not None:
        prior_adjusted = history[-1][1] if history else state.high_water * (1 - state.drawdown)
        prior_raw = state.last_equity if state.last_equity is not None else prior_adjusted
        period_end = close_equity - _decimal(net_external_flow)
        if min(prior_adjusted, prior_raw, period_end) <= 0:
            raise ValueError("drawdown requires a positive flow-adjusted close interval")
        adjusted_close = prior_adjusted * period_end / prior_raw
        if not history:  # Support callers constructing a legacy state directly.
            history = ((None, state.high_water), (state.last_session, prior_adjusted))

    window = (*history, (session, adjusted_close))[-config.DRAWDOWN_ROLLING_SESSIONS:]
    rolling_high = max(close for _, close in window)
    rolling_loss = (rolling_high - adjusted_close) / rolling_high
    start = state.pause_start_session
    # In-memory legacy state can lack an episode anchor before its first updated close.
    if start is None and state.paused:
        start = state.last_session or session
    governed = state.pause_sessions_governed
    if start is not None and start != session:
        governed = min(config.DRAWDOWN_PAUSE_MAX_SESSIONS, governed + 1)
    expired = governed >= config.DRAWDOWN_PAUSE_MAX_SESSIONS
    paused = state.paused and not expired
    # A fresh breach is strictly above the pause line, not equality (unlike
    # the shipped latch). Use Addendum 2's tolerance for both crossing sides.
    pause_line = _decimal(config.DRAWDOWN_PAUSE_PCT) + Decimal("1e-10")
    if (start is None or expired) and state.decision_drawdown <= pause_line < rolling_loss:
        start, governed, paused = session, 0, True
    # Same exact Decimal boundary as the shipped throttle, strictly below for recovery.
    recovering = paused and rolling_loss < _decimal(config.DRAWDOWN_THROTTLE_PCT)
    streak = (state.recovery_closes if previous_session is None or previous_session == state.last_session else 0)
    closes = streak + 1 if recovering else 0
    if closes >= config.DRAWDOWN_RECOVERY_SESSIONS:
        paused, closes = False, 0

    high_water = max(adjusted_close, state.high_water)
    drawdown = (high_water - adjusted_close) / high_water
    return DrawdownState(high_water, drawdown, paused, closes, session, close_equity,
                         window, rolling_loss, start, governed)


def _session_id(day: str) -> str:
    try:
        parsed = date.fromisoformat(day)
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid drawdown session label") from exc
    return parsed.isoformat()


class RiskManager:
    def __init__(self, config: Config):
        self.config = config

    def evaluate_trade(
        self, snapshot: "BookSnapshot", intent: TradeIntent, *,
        now: datetime | None = None, controls: RiskControls | None = None,
    ) -> RiskVerdict:
        """Project an intent on broker data. No broker calls or state writes."""
        controls = controls or RiskControls()
        try:
            kind = TradeKind(intent.kind)
            symbol = intent.symbol.strip().upper()
            qty, price, fee = map(_decimal, (intent.qty, intent.price, intent.fee))
            if not symbol or qty < 0 or price < 0 or fee < 0:
                return RiskVerdict(False, ("invalid_intent",))
            if kind == TradeKind.OPTION_DELIVERY:
                projection = self._project_book(snapshot, controls)
                breaches = self._breaches(projection)
                return RiskVerdict(not breaches, tuple(b.kind for b in breaches),
                                   projection.totals(), detect_only=True)
            if qty <= 0:
                return RiskVerdict(False, ("invalid_quantity",))
            if kind == TradeKind.COVERED_CALL:
                return RiskVerdict(False, ("covered_calls_deferred",))
            if kind in {TradeKind.SELL, TradeKind.BUY_TO_COVER, TradeKind.OPTION_CLOSE}:
                return self._check_reduction(snapshot, kind, symbol, qty, price, intent)
            reasons = self._entry_reasons(snapshot, intent, kind, now, controls)
            if reasons:
                return RiskVerdict(False, tuple(reasons))
            projection = self._project_book(snapshot, controls, stock_mark_floor=self._add_mark_floor(intent, kind))
            if any(stock.qty < 0 for stock in projection.stocks.values()):
                return RiskVerdict(False, ("unexpected_short",), projection.totals())
            if kind in {TradeKind.OPTION_OPEN, TradeKind.CASH_SECURED_PUT}:
                return self._evaluate_option(projection, intent, kind, qty, fee, controls)
            c = classify_symbol(symbol, intent.sector)
            current = projection.stocks.get(symbol)
            if current is not None:
                if current.classification.sector != CanonicalSector.UNKNOWN:
                    c = current.classification
                elif c.sector != CanonicalSector.UNKNOWN:
                    projection.stocks[symbol] = replace(current, classification=c)
            target, held, condition = self._stock_target(projection, intent, kind, controls)
            if condition:
                return RiskVerdict(False, (condition,), projection.totals())
            if qty != qty.to_integral_value() or price <= 0:
                return RiskVerdict(False, ("invalid_quantity_or_limit",))
            mark = price if intent.mark_price is None else _decimal(intent.mark_price)
            if mark <= 0:
                return RiskVerdict(False, ("invalid_mark",))
            entry_value = qty * (max(price, mark) if kind == TradeKind.ADD else price)
            if entry_value > target - held:
                return RiskVerdict(False, ("sizing_limit",), projection.totals())
            before = self._breaches(projection)
            equity_before = projection.equity
            projection.add_buy(symbol, c, qty, price, mark)
            projection.reserve(fee)
            # Known fee and a fill at the unfavorable bound erode marked equity.
            projection.equity -= fee + qty * max(Decimal(0), price - mark)
            if projection.equity <= 0:
                return RiskVerdict(False, ("nonpositive_projected_equity",))
            if (self.config.MARGIN_ENABLED and controls.drawdown.is_throttled(self.config)
                    and projection.cash < 0):
                return RiskVerdict(False, ("drawdown_no_new_borrowing",), projection.totals())
            stop_distance = _decimal(self.config.STOP_LOSS_PCT if intent.stop_distance is None
                                     else intent.stop_distance)
            loss_budget = projection.equity * self._ratio("MAX_INITIAL_STOP_RISK_PCT")
            if controls.drawdown.is_throttled(self.config):
                loss_budget *= self._ratio("DRAWDOWN_THROTTLE_SCALE")
            if (held + entry_value) * stop_distance > loss_budget:
                return RiskVerdict(False, ("stop_risk_budget",), projection.totals())
            breaches = self._breaches(projection)
            unchanged = self._unchanged_breaches(before, breaches, equity_before, projection.equity)
            reasons = tuple(dict.fromkeys(b.kind for b in breaches
                                         if b.kind != "option_budget" and b not in unchanged))
            return RiskVerdict(not reasons, reasons, projection.totals(),
                               allowed_existing_breaches=unchanged if not reasons else ())
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return RiskVerdict(False, ("invalid_risk_input",))

    def propose_equity_qty(
        self, snapshot: "BookSnapshot", intent: TradeIntent, *,
        now: datetime | None = None, controls: RiskControls | None = None,
    ) -> int:
        """Find the largest whole-share size that passes the identical final gate."""
        controls = controls or RiskControls()
        try:
            kind = TradeKind(intent.kind)
            if kind not in {TradeKind.BUY, TradeKind.ADD}:
                return 0
            if self._entry_reasons(snapshot, intent, kind, now, controls):
                return 0
            projection = self._project_book(snapshot, controls, stock_mark_floor=self._add_mark_floor(intent, kind))
            target, held, condition = self._stock_target(projection, intent, kind, controls)
            price = _decimal(intent.price)
            if condition or price <= 0 or target <= held:
                return 0
            if kind == TradeKind.ADD and intent.mark_price is not None:
                price = max(price, _decimal(intent.mark_price))
            low, high = 0, int((target - held) / price)
            while low < high:
                qty = (low + high + 1) // 2
                if self.evaluate_trade(snapshot, replace(intent, qty=Decimal(qty)),
                                       now=now, controls=controls).ok:
                    low = qty
                else:
                    high = qty - 1
            return low
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return 0

    def evaluate_portfolio(
        self, snapshot: "BookSnapshot", *, controls: RiskControls | None = None,
        include_pending: bool = True,
    ) -> tuple[Breach, ...]:
        """Report proven breaches and separately identify holdings without usable marks."""
        try:
            return self._breaches(
                self._project_book(snapshot, controls or RiskControls(),
                                   include_pending=include_pending, allow_unpriced=True),
                include_at_limit=True,
            )
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return (Breach("invalid_risk_input", "", Decimal(0), Decimal(0)),)

    def advance_drift_counters(
        self, breaches: Sequence[Breach], previous: Mapping[tuple[str, str], int],
    ) -> dict[tuple[str, str], int]:
        """Compute one close's consecutive counters. Caller owns session idempotence."""
        return {
            (b.kind, b.key): previous.get((b.kind, b.key), 0) + 1
            for b in breaches if self._outside_drift_band(b)
        }

    def _outside_drift_band(self, breach: Breach) -> bool:
        buffer = _decimal(self.config.RISK_DRIFT_BUFFER_PCT)
        if breach.kind == "cash_floor":
            return breach.value < breach.cap - buffer
        return (breach.kind in {"issuer_cap", "sector_cap", "cluster_cap", "gross_cap"}
                and breach.value > breach.cap + buffer)

    def plan_remediation(
        self, snapshot: "BookSnapshot", *,
        consecutive_closes: Mapping[tuple[str, str], int] | None = None,
        broker_action: bool = False, broker_symbol: str | None = None,
        controls: RiskControls | None = None,
    ) -> tuple[TradeIntent, ...]:
        """Recommend one executable next action. Reconcile before doing another."""
        controls = controls or RiskControls()
        try:
            book = self._project_book(snapshot, controls, include_pending=False, allow_unpriced=True)
            delivered = book.stocks.get(broker_symbol.strip().upper()) if broker_symbol else None
            if broker_action and broker_symbol and delivered is None:
                return ()  # Do not repair unrelated holdings when the delivery's mark is unavailable.
            for stock in book.stocks.values():
                if stock.qty < 0 and (delivered is None or stock == delivered):
                    return (TradeIntent(TradeKind.BUY_TO_COVER, stock.symbol, -stock.qty,
                                        stock.mark, reason="unexpected_short"),)
            breaches = self._breaches(book)
            if not breaches:
                return ()
            counters = consecutive_closes or {}
            headroom = _decimal(self.config.RISK_TRIM_HEADROOM_PCT)
            for breach in breaches:
                if broker_action and delivered is not None and not self._contributes(delivered, breach):
                    continue
                if breach.kind == "position_count" and broker_action:
                    # Prefer the newly delivered name if fully saleable.
                    candidates = [
                        stock for stock in book.stocks.values()
                        if stock.qty > 0 and stock.qty == stock.qty.to_integral_value()
                        and self._covered_shares(snapshot, stock.symbol) == 0
                        and (delivered is None or stock == delivered)
                    ]
                    candidates.sort(key=lambda s: (s.symbol != broker_symbol, s.qty * s.mark, s.symbol))
                    if candidates:
                        stock = candidates[0]
                        return (TradeIntent(TradeKind.SELL, stock.symbol, stock.qty, stock.mark,
                                            reason=breach.kind),)
                if breach.kind not in {"issuer_cap", "sector_cap", "cluster_cap", "gross_cap", "cash_floor"}:
                    continue
                if not broker_action:
                    if (not self._outside_drift_band(breach)
                            or counters.get((breach.kind, breach.key), 0) < self.config.RISK_DRIFT_CONFIRM_SESSIONS):
                        continue
                candidates = [s for s in book.stocks.values()
                              if s.qty > 0 and self._contributes(s, breach)
                              and (not broker_action or delivered is None or s == delivered)]
                candidates.sort(key=lambda s: (s.symbol != broker_symbol, -s.qty * s.mark, s.symbol))
                cushion = Decimal(0) if broker_action else headroom
                distance = (breach.cap + cushion - breach.value if breach.kind == "cash_floor"
                            else breach.value - max(Decimal(0), breach.cap - cushion))
                for stock in candidates:
                    sellable = max(Decimal(0), stock.qty - self._covered_shares(snapshot, stock.symbol))
                    desired = (distance * book.equity / stock.mark).to_integral_value(rounding=ROUND_CEILING)
                    qty = min(Decimal(int(sellable)), desired)
                    if qty > 0:
                        return (TradeIntent(TradeKind.SELL, stock.symbol, qty, stock.mark,
                                            reason=breach.kind),)
            return ()
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return ()

    def _ratio(self, name: str) -> Decimal:
        ratio = _decimal(getattr(self.config, name))
        if not 0 < ratio <= 1:
            raise ValueError(f"invalid risk setting {name}")
        return ratio

    def margin_status(self, snapshot: BookSnapshot) -> MarginStatus | None:
        """Calculate actual account excess (no unfilled orders or assumptions about future fills)."""
        if not self.config.MARGIN_ENABLED:
            return None
        try:
            book = self._project_book(snapshot, RiskControls(), include_pending=False, allow_unpriced=True)
            return self._margin_status(book)
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return None

    def _margin_status(self, book: _Projection) -> MarginStatus | None:
        if book.maintenance is None:
            return None
        excess = (book.equity - book.maintenance) / book.equity
        return MarginStatus(book.equity, book.maintenance, excess,
                            book.margin_call or excess < self._ratio("MARGIN_TRIM_EXCESS_PCT"),
                            book.margin_call)

    def plan_margin_trim(
        self, snapshot: BookSnapshot, *, controls: RiskControls | None = None, force: bool = False,
    ) -> tuple[TradeIntent, ...]:
        """Plan whole-share sales toward target; caller reconciles before each next sale."""
        if not self.config.MARGIN_ENABLED:
            return ()
        controls = controls or RiskControls()
        try:
            book = self._project_book(snapshot, RiskControls(), include_pending=False, allow_unpriced=True)
            status = self._margin_status(book)
            if status is None or not (status.needs_trim or force):
                return ()
            required = status.maintenance_margin - status.equity * (1 - self._ratio("MARGIN_MIN_EXCESS_PCT"))
            if required <= 0:
                return ()
            ranked = []
            for stock in book.stocks.values():
                if stock.qty <= 0:
                    continue
                holding = snapshot.holding(stock.symbol)
                assert holding is not None
                meta = holding.meta
                grade = controls.holding_convictions.get(stock.symbol)
                if grade is None and meta is not None:
                    grade = meta.notes.get("effective_conviction")
                grade = grade if type(grade) is int and 1 <= grade <= 10 else 0
                ranked.append((grade, (stock.mark - _decimal(holding.avg_cost)) * stock.qty, stock.symbol))
            plan = []
            for _, _, symbol in sorted(ranked):
                stock = book.stocks[symbol]
                meta = self._metadata(snapshot, symbol)
                stop_id = getattr(meta, "stop_order_id", None)
                # The exit executor can cancel its own stop and re-protect the remainder.
                # Other working sell orders and any pledged shares stay unavailable.
                own_stop_qty = sum(
                    (_decimal(order.remaining_qty) for order in snapshot.open_orders_for(symbol)
                     if stop_id and order.order_id == stop_id and order.side == "sell"
                     and order.type in {"stop", "stop_limit"}), _ZERO,
                )
                pledged = self._covered_shares(snapshot, symbol)
                available = min(stock.qty - pledged, stock.available + own_stop_qty)
                relief = stock.mark * _maintenance_rate(symbol, stock.mark)
                qty = min(Decimal(max(0, int(available))),
                          (required / relief).to_integral_value(rounding=ROUND_CEILING))
                if qty > 0:
                    plan.append(TradeIntent(TradeKind.SELL, symbol, qty, stock.mark, reason="margin_buffer"))
                    required -= qty * relief
                if required <= 0:
                    break
            return tuple(plan)
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return ()

    @staticmethod
    def _metadata(snapshot: "BookSnapshot", symbol: str) -> object | None:
        stock = snapshot.holding(symbol)
        return stock.meta if stock else None

    def _project_book(
        self, snapshot: "BookSnapshot", controls: RiskControls, *,
        include_pending: bool = True, allow_unpriced: bool = False,
        stock_mark_floor: tuple[str, Decimal] | None = None,
    ) -> _Projection:
        equity, cash = _decimal(snapshot.equity), _decimal(snapshot.cash)
        if equity <= 0:
            raise ValueError("nonpositive broker equity")
        book = _Projection(equity, cash)
        guess_maintenance = False
        if self.config.MARGIN_ENABLED:
            regt = getattr(snapshot, "regt_buying_power", None)
            book.regt_buying_power = max(_ZERO, cash) if regt is None else _decimal(regt)
            option_bp = getattr(snapshot, "options_buying_power", None)
            if option_bp is None:
                option_bp = getattr(snapshot, "non_marginable_buying_power", None)
            book.options_buying_power = max(_ZERO, cash) if option_bp is None else _decimal(option_bp)
            maintenance = getattr(snapshot, "maintenance_margin", None)
            guess_maintenance = maintenance is None
            book.maintenance = _ZERO if guess_maintenance else _decimal(maintenance)
            if book.maintenance < 0:
                raise ValueError("invalid broker maintenance margin")
            book.margin_call = getattr(snapshot, "margin_call", None) is True
        commitments: set[str] = set()
        reserved_orders: set[str] = set()
        for option in controls.option_commitments:
            loss, reserved = _decimal(option.max_loss), _decimal(option.cash_reserved)
            if not option.structure_id or option.structure_id in commitments or min(loss, reserved) < 0:
                raise ValueError("invalid or duplicate option commitment")
            if reserved_orders.intersection(option.order_ids):
                raise ValueError("one broker order linked to multiple option commitments")
            commitments.add(option.structure_id)
            reserved_orders.update(option.order_ids)
            book.option_loss += loss
            book.reserve(reserved)
        book.option_count = len(commitments)
        for stock in snapshot.holdings:
            symbol = stock.symbol.strip().upper()
            qty = _decimal(stock.qty)
            if qty == 0:
                continue
            if not symbol or symbol in book.stocks or symbol in book.unpriced:
                raise ValueError("missing or duplicate holding symbol")
            try:
                mark = _decimal(stock.price) if stock.price_ok else Decimal(0)
            except ValueError:
                mark = Decimal(0)
            if mark <= 0:
                if not allow_unpriced:
                    raise ValueError("invalid holding mark")
                book.unpriced[symbol] = qty
                if guess_maintenance and qty > 0:
                    conservative_value = max(_ZERO, _decimal(stock.market_value), qty * _decimal(stock.avg_cost))
                    book.maintenance += conservative_value  # Unknown mark: no discount to the fallback.
                continue
            if stock_mark_floor is not None and stock_mark_floor[0] == symbol:
                mark = max(mark, stock_mark_floor[1])
            meta = self._metadata(snapshot, symbol)
            sector = getattr(meta, "sector", None) or getattr(stock, "sector", None)
            stop = getattr(meta, "stop_price", None)
            book.stocks[symbol] = _Stock(
                symbol, qty, mark, classify_symbol(symbol, sector),
                _decimal(getattr(stock, "qty_available", qty)), _decimal(stop) if stop else None,
            )
            if guess_maintenance and qty > 0:
                book.maintenance += qty * mark * _maintenance_rate(symbol, mark)
        if include_pending:
            seen: set[str] = set()
            for order in snapshot.open_orders:
                order_id = order.order_id or order.client_order_id
                if order_id and order_id in seen:
                    continue
                if order_id:
                    seen.add(order_id)
                if order.side != "buy" and getattr(order, "order_class", None) != "mleg":
                    continue
                quantity = _decimal(order.remaining_qty)
                if quantity <= 0:
                    continue
                symbol = order.symbol.strip().upper()
                limit = _decimal(order.limit_price)
                is_option = (order.asset_class == "us_option" or order.order_class == "mleg"
                             or any(leg.asset_class == "us_option" for leg in order.legs))
                position_intent = getattr(order, "position_intent", None)
                if is_option:
                    if order_id not in reserved_orders and order.client_order_id not in reserved_orders:
                        closing = position_intent in {"buy_to_close", "sell_to_close"} or (
                            bool(order.legs) and all(getattr(leg, "position_intent", None)
                                                     in {"buy_to_close", "sell_to_close"} for leg in order.legs)
                        )
                        if limit < 0 and closing:
                            continue  # Credit not usable before fill.
                        if limit <= 0:
                            raise ValueError("open option debit has no executable upper bound")
                        reserve = quantity * limit * 100
                        book.reserve(reserve)
                        if not closing:
                            book.option_loss += reserve
                            book.option_count += 1
                    continue
                if limit <= 0:
                    raise ValueError("open buy has no executable upper bound")
                if not symbol:
                    raise ValueError("open stock buy has no symbol")
                held = book.stocks.get(symbol)
                if position_intent == "buy_to_close" or (held is not None and held.qty < 0):
                    book.reserve(quantity * limit)
                    continue
                meta = self._metadata(snapshot, symbol)
                c = held.classification if held else classify_symbol(
                    symbol, controls.pending_sectors.get(symbol) or getattr(meta, "sector", None),
                )
                book.add_buy(symbol, c, quantity, limit, held.mark if held else limit)
                if c.sector == CanonicalSector.UNKNOWN:
                    book.unknown_pending += quantity * limit
        return book

    def _breaches(self, book: _Projection, *, include_at_limit: bool = False) -> tuple[Breach, ...]:
        totals = book.totals()
        breached = []
        for symbol, stock in book.stocks.items():
            if stock.qty < 0:
                breached.append(Breach("unexpected_short", symbol, -stock.qty, Decimal(0)))
        for symbol, qty in book.unpriced.items():
            breached.append(Breach("unpriced_holding", symbol, abs(qty), Decimal(0)))
            if qty < 0:
                breached.append(Breach("unexpected_short", symbol, -qty, Decimal(0)))
        floor_amount, floor_key = totals.free_cash, "cash_floor"
        if self.config.MARGIN_ENABLED:
            floor_amount, floor_key = book.regt_buying_power, "buying_power_floor"
            status = self._margin_status(book)
            margin_min = self._ratio("MARGIN_MIN_EXCESS_PCT")
            if status is not None:
                if status.excess_pct < margin_min:
                    breached.append(Breach("margin_buffer", "", status.excess_pct, margin_min))
                if status.margin_call:
                    breached.append(Breach("margin_call", "", _ZERO, _ZERO))
        floor = _decimal(self.config.MIN_FREE_CASH_PCT)
        floor_pct = floor_amount / totals.equity
        if floor_pct < floor:
            breached.append(Breach(floor_key, "", floor_pct, floor))
        gross_pct = totals.gross_exposure / totals.equity
        gross_cap = _decimal(self.config.effective_max_gross_exposure_pct)
        if gross_pct > gross_cap:
            breached.append(Breach("gross_cap", "", gross_pct, gross_cap))
        if totals.positions > self.config.MAX_POSITIONS:
            breached.append(Breach("position_count", "", Decimal(totals.positions),
                                   Decimal(self.config.MAX_POSITIONS)))
        for kind, cap, buckets in (
            ("issuer_cap", self._ratio("MAX_POSITION_PCT"), totals.issuer_values),
            ("sector_cap", self._ratio("MAX_SECTOR_PCT"), totals.sector_values),
            ("cluster_cap", self._ratio("MAX_CLUSTER_PCT"), totals.cluster_values),
        ):
            for key, value in buckets.items():
                if kind == "sector_cap" and key != CanonicalSector.UNKNOWN:
                    value += book.unknown_pending
                pct = value / totals.equity
                if pct > cap or (include_at_limit and pct == cap):
                    breached.append(Breach(kind, key, pct, cap, at_limit=(pct == cap)))
        loss_pct = totals.option_max_loss / totals.equity
        if loss_pct > self._ratio("RISK_OPTION_TOTAL_LOSS_PCT"):
            breached.append(Breach("option_budget", "", loss_pct,
                                   self._ratio("RISK_OPTION_TOTAL_LOSS_PCT")))
        if totals.option_structures > self.config.RISK_OPTION_MAX_STRUCTURES:
            breached.append(Breach("option_budget", "count", Decimal(totals.option_structures),
                                   Decimal(self.config.RISK_OPTION_MAX_STRUCTURES)))
        return tuple(breached)

    @staticmethod
    def _unchanged_breaches(
        before: tuple[Breach, ...], after: tuple[Breach, ...],
        equity_before: Decimal, equity_after: Decimal,
    ) -> tuple[Breach, ...]:
        prior = {(b.kind, b.key): b for b in before}
        allowed = []
        for breach in after:
            old = prior.get((breach.kind, breach.key))
            if old is None:
                continue
            if breach.kind == "position_count" and breach.value <= old.value:
                allowed.append(breach)
            elif breach.kind in {"issuer_cap", "sector_cap", "cluster_cap"}:
                # Same-bucket risk is judged on nominal exposure, not tiny changes
                # in NAV from an unrelated bounded order's documented fee/slippage.
                before_value, after_value = old.value * equity_before, breach.value * equity_after
                if after_value <= before_value + Decimal("0.000001"):
                    allowed.append(breach)
        return tuple(allowed)

    def _entry_reasons(
        self, snapshot: "BookSnapshot", intent: TradeIntent, kind: TradeKind,
        now: datetime | None, controls: RiskControls,
    ) -> list[str]:
        reasons = []
        option = kind in {TradeKind.OPTION_OPEN, TradeKind.CASH_SECURED_PUT}
        if controls.pause_new_buys or self.config.RISK_PAUSE_NEW_BUYS:
            reasons.append("entries_paused")
        if controls.drawdown.paused or (option and controls.drawdown.is_throttled(self.config)):
            reasons.append("drawdown_pause")
        if (intent.symbol.strip().upper() in snapshot.blocked_symbols
                or any(snapshot.account_flags.get(x) is True for x in
                       ("account_blocked", "trading_blocked", "trade_suspended_by_user"))):
            reasons.append("broker_book_blocked")
        status = snapshot.account_flags.get("status")
        if status and str(status).lower() != "active":
            reasons.append("broker_account_inactive")
        raw, effective = intent.raw_conviction, intent.effective_conviction
        floor = self.config.MIN_CONVICTION
        effective_floor = max(8 if option else floor, intent.minimum_conviction)
        now = now or snapshot.as_of
        owner = controls.owner_entry
        if owner is not None:
            lane = {TradeKind.BUY: "stock", TradeKind.OPTION_OPEN: "option"}.get(kind)
            valid = (
                isinstance(owner, OwnerEntryProof) and lane is not None and owner.lane == lane
                and intent.symbol.strip().upper() == owner.symbol
                and owner.generated_at == intent.signal_at and now.utcoffset() is not None
                and owner.added <= now.astimezone(UTC).date() <= owner.expires
                and type(raw) is int and 1 <= raw <= 10 and owner.raw_conviction == raw
                and type(effective) is int and max(floor, effective_floor, 7) <= effective <= 9
                and owner.effective_conviction == effective
            )
            if valid and lane == "stock":
                valid = _owner_stock_issuer_clear(snapshot, owner, controls.owner_option_scope)
            if not valid:
                reasons.append("owner_directive_scope_mismatch")
        else:
            if (type(raw) is not int or type(effective) is not int
                    or not floor <= raw <= 10 or not effective_floor <= effective <= 10):
                reasons.append("conviction_floor")
            elif abs(raw - effective) > self.config.CONVICTION_MAX_ADJUSTMENT:
                reasons.append("conviction_adjustment")
        if not 0 <= (now - snapshot.as_of).total_seconds() <= self.config.MAX_ENTRY_QUOTE_AGE_SECONDS:
            reasons.append("stale_broker_book")
        if intent.signal_at is None or not (
            0 <= (now - intent.signal_at).total_seconds() <= self.config.MAX_ENTRY_SIGNAL_HOURS * 3600
        ):
            reasons.append("stale_signal")
        if not option:
            if intent.quote_at is None or not 0 <= (now - intent.quote_at).total_seconds() <= self.config.MAX_ENTRY_QUOTE_AGE_SECONDS:
                reasons.append("stale_quote")
            analysis_price = _decimal(intent.analysis_price)
            mark = _decimal(intent.price if intent.mark_price is None else intent.mark_price)
            if analysis_price <= 0 or mark <= 0:
                reasons.append("invalid_analysis_price")
            elif abs(mark / analysis_price - 1) > self._ratio("MAX_ENTRY_PRICE_DRIFT_PCT"):
                reasons.append("price_drift")
        return reasons

    @staticmethod
    def _add_mark_floor(intent: TradeIntent, kind: TradeKind) -> tuple[str, Decimal] | None:
        if kind == TradeKind.ADD and intent.mark_price is not None:
            # A lower snapshot mark must not hide the live value of held or
            # already working shares. Keep the higher mark for both.
            return intent.symbol.strip().upper(), _decimal(intent.mark_price)
        return None

    def _stock_target(
        self, book: _Projection, intent: TradeIntent, kind: TradeKind, controls: RiskControls,
    ) -> tuple[Decimal, Decimal, str | None]:
        sector = classify_symbol(intent.symbol, intent.sector)
        price = _decimal(intent.price)
        mark = _decimal(intent.price if intent.mark_price is None else intent.mark_price)
        stop_distance = _decimal(self.config.STOP_LOSS_PCT if intent.stop_distance is None
                                 else intent.stop_distance)
        if not self._ratio("STOP_LOSS_PCT_MIN") <= stop_distance <= self._ratio("STOP_LOSS_PCT_MAX"):
            return Decimal(0), Decimal(0), "invalid_stop_distance"
        tier = self.config.CONVICTION_SIZE_MAP.get(intent.effective_conviction)
        if tier is None:
            return Decimal(0), Decimal(0), "invalid_sizing_tier"
        pct = min(self._ratio("MAX_POSITION_PCT"), _decimal(tier),
                  self._ratio("MAX_INITIAL_STOP_RISK_PCT") / stop_distance)
        target = pct * book.equity
        if controls.drawdown.is_throttled(self.config):
            target *= self._ratio("DRAWDOWN_THROTTLE_SCALE")
        held = book.totals().issuer_values.get(sector.issuer, Decimal(0))
        stock = book.stocks.get(intent.symbol.strip().upper())
        if kind == TradeKind.ADD:
            if stock is None or stock.qty <= 0:
                return target, held, "top_up_requires_holding"
            prior = intent.last_filled_conviction
            if intent.effective_conviction < 8 or (
                prior is None and not intent.initial_reaffirmation
            ) or (
                prior is not None
                and (type(prior) is not int or not 1 <= prior <= 10 or intent.effective_conviction < prior)
            ):
                return target, held, "top_up_conviction"
            if intent.thesis_change not in ("strengthened", "unchanged"):
                return target, held, "top_up_thesis_change"
            if stock.stop is None or stock.stop <= 0 or mark <= stock.stop:
                return target, held, "top_up_below_stop"
            if held >= target * min(Decimal(".90"), self._ratio("TOP_UP_TARGET_TOLERANCE")):
                return target, held, "near_target"
        elif stock is not None and stock.qty > 0:
            return target, held, "top_up_intent_required"
        if price <= 0:
            return target, held, "invalid_limit"
        return target, held, None

    def _evaluate_option(
        self, book: _Projection, intent: TradeIntent, kind: TradeKind,
        qty: Decimal, fee: Decimal, controls: RiskControls,
    ) -> RiskVerdict:
        if qty != qty.to_integral_value():
            return RiskVerdict(False, ("invalid_contract_quantity",))
        max_loss, debit = _decimal(intent.option_max_loss), _decimal(intent.option_cash_debit)
        if max_loss <= 0 or debit < 0 or (kind == TradeKind.OPTION_OPEN and debit <= 0):
            return RiskVerdict(False, ("invalid_option_cost",))
        if not intent.structure_id or any(x.structure_id == intent.structure_id for x in controls.option_commitments):
            return RiskVerdict(False, ("missing_or_duplicate_structure_id",))
        before = self._breaches(book)
        equity_before = book.equity
        book.reserve(debit + fee)
        book.equity -= fee
        book.option_loss += max_loss
        book.option_count += 1
        reasons = []
        if book.equity <= 0:
            return RiskVerdict(False, ("nonpositive_projected_equity",))
        if max_loss > book.equity * self._ratio("RISK_OPTION_MAX_LOSS_PCT"):
            reasons.append("option_single_budget")
        if book.option_loss > book.equity * self._ratio("RISK_OPTION_TOTAL_LOSS_PCT"):
            reasons.append("option_total_budget")
        if book.option_count > self.config.RISK_OPTION_MAX_STRUCTURES:
            reasons.append("option_count")
        exposure = book
        if kind == TradeKind.CASH_SECURED_PUT:
            shares, strike = _decimal(intent.assignment_shares), _decimal(intent.assignment_strike)
            mark = strike if intent.mark_price is None else _decimal(intent.mark_price)
            if shares != qty * 100 or min(strike, mark) <= 0:
                return RiskVerdict(False, ("invalid_assignment",))
            book.reserve(shares * strike)
            if self.config.MARGIN_ENABLED and book.cash < 0:
                reasons.append("cash_secured_put_unfunded")
            # Only a cash-secured put projects assigned shares at entry. Do
            # not double-count the reserved collateral in its cash check.
            symbol = intent.symbol.strip().upper()
            stock = book.stocks.get(symbol)
            prior = book.pending_values.get(symbol)
            c = classify_symbol(symbol, intent.sector)
            if prior and prior[0].sector != CanonicalSector.UNKNOWN:
                c = prior[0]
            if stock is not None and stock.classification.sector != CanonicalSector.UNKNOWN:
                c = stock.classification
            pending = dict(book.pending_values)
            pending[symbol] = (c, shares * max(strike, mark) + (prior[1] if prior else 0))
            exposure = replace(book, pending_values=pending)
        if self.config.MARGIN_ENABLED and book.options_buying_power < 0:
            reasons.append("options_buying_power")
        after = self._breaches(exposure)
        tolerated = self._unchanged_breaches(before, after, equity_before, exposure.equity)
        reasons.extend(b.kind for b in after if b.kind != "option_budget" and b not in tolerated)
        return RiskVerdict(not reasons, tuple(dict.fromkeys(reasons)), book.totals(),
                           allowed_existing_breaches=tolerated if not reasons else ())

    @staticmethod
    def _covered_shares(snapshot: "BookSnapshot", symbol: str) -> Decimal:
        calls = [leg for leg in snapshot.legs_for(symbol) if leg.right.lower() in {"c", "call"}]
        hedges = {leg.occ_symbol: leg.qty for leg in calls if leg.qty > 0}
        reserved = Decimal(0)
        for leg in sorted((leg for leg in calls if leg.qty < 0), key=lambda leg: leg.strike):
            unhedged = -leg.qty
            for hedge in calls:
                available = hedges.get(hedge.occ_symbol, Decimal(0))
                if (available <= 0 or hedge.expiry != leg.expiry or hedge.multiplier != leg.multiplier
                        or hedge.strike >= leg.strike):
                    continue
                paired = min(available, unhedged)
                hedges[hedge.occ_symbol] -= paired
                unhedged -= paired
                if unhedged == 0:
                    break
            # Existing broker qty_available is a separate, authoritative ceiling.
            # Long calls serving as spread hedges do not also tie up stock shares.
            reserved += unhedged * leg.multiplier
        return reserved

    @staticmethod
    def _contributes(stock: _Stock, breach: Breach) -> bool:
        if breach.kind == "issuer_cap":
            return stock.classification.issuer == breach.key
        if breach.kind == "sector_cap":
            return stock.classification.sector == breach.key
        if breach.kind == "cluster_cap":
            return breach.key in stock.classification.clusters
        return True

    def _check_reduction(
        self, snapshot: "BookSnapshot", kind: TradeKind, symbol: str, qty: Decimal,
        price: Decimal, intent: TradeIntent,
    ) -> RiskVerdict:
        if kind == TradeKind.OPTION_CLOSE:
            if not any(symbol in {leg.underlying, leg.occ_symbol}
                       and abs(leg.qty) >= qty for leg in snapshot.legs):
                return RiskVerdict(False, ("no_option_to_close",))
            debit = _decimal(intent.option_cash_debit)
            if debit < 0:
                return RiskVerdict(False, ("invalid_close_debit",))
            return RiskVerdict(True)
        stock = next((held for held in snapshot.holdings if held.symbol == symbol), None)
        if stock is None:
            return RiskVerdict(False, ("no_position",))
        held = _decimal(stock.qty)
        if kind == TradeKind.BUY_TO_COVER:
            if held >= 0 or qty > -held:
                return RiskVerdict(False, ("cover_exceeds_short",))
            if price <= 0:
                return RiskVerdict(False, ("invalid_limit",))
            return RiskVerdict(True)
        if held <= 0 or qty > min(held, _decimal(getattr(stock, "qty_available", held)),
                                   max(Decimal(0), held - self._covered_shares(snapshot, symbol))):
            return RiskVerdict(False, ("sell_exceeds_available_shares",))
        return RiskVerdict(True)
