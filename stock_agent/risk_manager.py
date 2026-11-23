import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from enum import StrEnum
from typing import Protocol

from stock_agent.config import Config
from stock_agent.ledger import BookSnapshot
from stock_agent.models import PortfolioState, Trade

logger = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class OptionCommitment:
    structure_id: str
    max_loss: Decimal
    cash_reserved: Decimal = Decimal(0)
    order_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DrawdownState:
    high_water: Decimal = Decimal(0)
    drawdown: Decimal = Decimal(0)
    paused: bool = False
    recovery_closes: int = 0
    last_session: str | None = None

    @property
    def throttled(self) -> bool:
        return self.paused or self.drawdown >= _decimal(Config.DRAWDOWN_THROTTLE_PCT)

    @property
    def alert(self) -> bool:
        return self.drawdown >= _decimal(Config.DRAWDOWN_ALERT_PCT)


@dataclass(frozen=True)
class RiskControls:
    pause_new_buys: bool = False
    drawdown: DrawdownState = field(default_factory=DrawdownState)
    option_commitments: tuple[OptionCommitment, ...] = ()
    pending_sectors: Mapping[str, str] = field(default_factory=dict)


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
        return PortfolioTotals(
            self.equity, self.cash, sum(by_issuer.values(), Decimal(0)),
            by_issuer, by_sector, by_cluster, len(by_issuer),
            self.option_loss, self.option_count,
        )

    def add_buy(self, symbol: str, c: Classification, qty: Decimal, price: Decimal) -> None:
        prior = self.pending_values.get(symbol)
        self.pending_values[symbol] = (c, qty * price + (prior[1] if prior else 0))
        self.cash -= qty * price


class DailyBar(Protocol):
    @property
    def high(self) -> Decimal | float: ...

    @property
    def low(self) -> Decimal | float: ...

    @property
    def close(self) -> Decimal | float: ...


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
) -> DrawdownState:
    """Apply one trusted (flow-adjusted when applicable) close, once."""
    if not session or (state.last_session is not None and session <= state.last_session):
        return state
    close_equity = _decimal(close_equity)
    if close_equity <= 0:
        raise ValueError("drawdown requires positive trusted equity")
    high_water = max(close_equity, state.high_water)
    drawdown = (high_water - close_equity) / high_water
    paused = state.paused or drawdown >= _decimal(Config.DRAWDOWN_PAUSE_PCT)
    recovering = paused and drawdown < _decimal(Config.DRAWDOWN_THROTTLE_PCT)
    streak = (state.recovery_closes if previous_session is None or previous_session == state.last_session
              else 0)
    closes = streak + 1 if recovering else 0
    if closes >= Config.DRAWDOWN_RECOVERY_SESSIONS:
        paused, closes = False, 0
    return DrawdownState(high_water, drawdown, paused, closes, session)


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
                return self._check_reduction(snapshot, kind, symbol, qty, price, fee, intent, controls)
            reasons = self._entry_reasons(snapshot, intent, kind, now, controls)
            if reasons:
                return RiskVerdict(False, tuple(reasons))
            projection = self._project_book(snapshot, controls)
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
            if price * qty > target - held:
                return RiskVerdict(False, ("sizing_limit",), projection.totals())
            mark = price if intent.mark_price is None else _decimal(intent.mark_price)
            if mark <= 0:
                return RiskVerdict(False, ("invalid_mark",))
            before = self._breaches(projection)
            equity_before = projection.equity
            projection.add_buy(symbol, c, qty, price)
            if mark > price:
                extra = (mark - price) * qty
                projection.pending_values[symbol] = (c, projection.pending_values[symbol][1] + extra)
            projection.cash -= fee
            # Known fee and a fill at the unfavorable bound erode marked equity.
            projection.equity -= fee + qty * max(Decimal(0), price - mark)
            if projection.equity <= 0:
                return RiskVerdict(False, ("nonpositive_projected_equity",))
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
            projection = self._project_book(snapshot, controls)
            target, held, condition = self._stock_target(projection, intent, kind, controls)
            price = _decimal(intent.price)
            if condition or price <= 0 or target <= held:
                return 0
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
    ) -> tuple[Breach, ...]:
        try:
            return self._breaches(
                self._project_book(snapshot, controls or RiskControls()), include_at_limit=True,
            )
        except (ValueError, TypeError, ArithmeticError, AttributeError):
            return (Breach("invalid_risk_input", "", Decimal(0), Decimal(0)),)

    def plan_remediation(
        self, snapshot: "BookSnapshot", *,
        consecutive_closes: Mapping[tuple[str, str], int] | None = None,
        broker_action: bool = False, broker_symbol: str | None = None,
        controls: RiskControls | None = None,
    ) -> tuple[TradeIntent, ...]:
        """Recommend one executable next action. Reconcile before doing another."""
        controls = controls or RiskControls()
        try:
            # One unpriced unrelated holding cannot suppress a funded short cover.
            for held in snapshot.holdings:
                if held.qty < 0 and held.price_ok and held.price is not None and held.price > 0:
                    return (TradeIntent(TradeKind.BUY_TO_COVER, held.symbol, -held.qty,
                                        held.price, reason="unexpected_short"),)
            book = self._project_book(snapshot, controls, include_pending=False)
            breaches = self._breaches(book)
            if not breaches:
                return ()
            counters = consecutive_closes or {}
            headroom = _decimal(self.config.RISK_TRIM_HEADROOM_PCT)
            buffer = _decimal(self.config.RISK_DRIFT_BUFFER_PCT)
            for breach in breaches:
                if breach.kind == "position_count" and broker_action:
                    # Count only falls when the last class of the issuer is closed.
                    # Prefer the newly delivered name if fully saleable.
                    candidates = [
                        stock for stock in book.stocks.values()
                        if stock.qty > 0 and stock.qty == stock.qty.to_integral_value()
                        and self._covered_shares(snapshot, stock.symbol) == 0
                        and sum(s.classification.issuer == stock.classification.issuer
                                for s in book.stocks.values() if s.qty > 0) == 1
                    ]
                    candidates.sort(key=lambda s: (s.symbol != broker_symbol, s.qty * s.mark, s.symbol))
                    if candidates:
                        stock = candidates[0]
                        return (TradeIntent(TradeKind.SELL, stock.symbol, stock.qty, stock.mark,
                                            reason=breach.kind),)
                if breach.kind not in {"issuer_cap", "sector_cap", "cluster_cap", "gross_cap", "cash_floor"}:
                    continue
                if not broker_action:
                    if breach.kind == "cash_floor":
                        beyond_band = breach.value < breach.cap - buffer
                    else:
                        beyond_band = breach.value > breach.cap + buffer
                    if not beyond_band or counters.get((breach.kind, breach.key), 0) < self.config.RISK_DRIFT_CONFIRM_SESSIONS:
                        continue
                candidates = [s for s in book.stocks.values()
                              if s.qty > 0 and self._contributes(s, breach)]
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

    @staticmethod
    def _metadata(snapshot: "BookSnapshot", symbol: str) -> object | None:
        stock = snapshot.holding(symbol)
        return stock.meta if stock else None

    def _project_book(
        self, snapshot: "BookSnapshot", controls: RiskControls, *,
        include_pending: bool = True,
    ) -> _Projection:
        equity, cash = _decimal(snapshot.equity), _decimal(snapshot.cash)
        if equity <= 0:
            raise ValueError("nonpositive broker equity")
        book = _Projection(equity, cash)
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
            book.cash -= reserved
        book.option_count = len(commitments)
        for stock in snapshot.holdings:
            symbol = stock.symbol.strip().upper()
            qty = _decimal(stock.qty)
            if qty == 0:
                continue
            mark = _decimal(stock.price)
            if not stock.price_ok or mark <= 0 or symbol in book.stocks:
                raise ValueError("invalid holding or mark")
            meta = self._metadata(snapshot, symbol)
            sector = getattr(meta, "sector", None) or getattr(stock, "sector", None)
            stop = getattr(meta, "stop_price", None)
            book.stocks[symbol] = _Stock(
                symbol, qty, mark, classify_symbol(symbol, sector),
                _decimal(getattr(stock, "qty_available", qty)), _decimal(stop) if stop else None,
            )
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
                        book.cash -= reserve
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
                    book.cash -= quantity * limit
                    continue
                meta = self._metadata(snapshot, symbol)
                c = held.classification if held else classify_symbol(
                    symbol, controls.pending_sectors.get(symbol) or getattr(meta, "sector", None),
                )
                book.add_buy(symbol, c, quantity, limit)
                if c.sector == CanonicalSector.UNKNOWN:
                    book.unknown_pending += quantity * limit
                if held is not None and held.mark > limit:
                    book.pending_values[symbol] = (
                        c, book.pending_values[symbol][1] + (held.mark - limit) * quantity,
                    )
        return book

    def _breaches(self, book: _Projection, *, include_at_limit: bool = False) -> tuple[Breach, ...]:
        totals = book.totals()
        breached = []
        for symbol, stock in book.stocks.items():
            if stock.qty < 0:
                breached.append(Breach("unexpected_short", symbol, -stock.qty, Decimal(0)))
        cash_pct = totals.free_cash / totals.equity
        if cash_pct < self._ratio("MIN_FREE_CASH_PCT"):
            breached.append(Breach("cash_floor", "", cash_pct, self._ratio("MIN_FREE_CASH_PCT")))
        gross_pct = totals.gross_exposure / totals.equity
        if gross_pct > self._ratio("MAX_TOTAL_EXPOSURE"):
            breached.append(Breach("gross_cap", "", gross_pct, self._ratio("MAX_TOTAL_EXPOSURE")))
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
        if controls.drawdown.paused or (option and controls.drawdown.throttled):
            reasons.append("drawdown_pause")
        if (intent.symbol.strip().upper() in snapshot.blocked_symbols
                or any(snapshot.account_flags.get(x) is True for x in
                       ("account_blocked", "trading_blocked", "trade_suspended_by_user"))):
            reasons.append("broker_book_blocked")
        raw, effective = intent.raw_conviction, intent.effective_conviction
        floor = self.config.MIN_CONVICTION
        effective_floor = max(8 if option else floor, intent.minimum_conviction)
        if (type(raw) is not int or type(effective) is not int
                or not floor <= raw <= 10 or not effective_floor <= effective <= 10):
            reasons.append("conviction_floor")
        elif abs(raw - effective) > self.config.CONVICTION_MAX_ADJUSTMENT:
            reasons.append("conviction_adjustment")
        now = now or snapshot.as_of
        if not 0 <= (now - snapshot.as_of).total_seconds() <= self.config.MAX_ENTRY_QUOTE_AGE_SECONDS:
            reasons.append("stale_broker_book")
        if not option or intent.signal_at is not None:
            if intent.signal_at is None or not 0 <= (now - intent.signal_at).total_seconds() <= self.config.MAX_ENTRY_SIGNAL_HOURS * 3600:
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
        if controls.drawdown.throttled:
            target *= self._ratio("DRAWDOWN_THROTTLE_SCALE")
        held = book.totals().issuer_values.get(sector.issuer, Decimal(0))
        stock = book.stocks.get(intent.symbol.strip().upper())
        if kind == TradeKind.ADD:
            if stock is None or stock.qty <= 0:
                return target, held, "top_up_requires_holding"
            if (type(intent.last_filled_conviction) is not int
                    or intent.effective_conviction < intent.last_filled_conviction):
                return target, held, "top_up_conviction"
            if stock.stop is not None and mark <= stock.stop:
                return target, held, "top_up_below_stop"
            if held >= target * self._ratio("TOP_UP_TARGET_TOLERANCE"):
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
        book.cash -= debit + fee
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
        if kind == TradeKind.CASH_SECURED_PUT:
            shares, strike = _decimal(intent.assignment_shares), _decimal(intent.assignment_strike)
            if shares != qty * 100 or strike <= 0:
                return RiskVerdict(False, ("invalid_assignment",))
            c = classify_symbol(intent.symbol, intent.sector)
            prior = book.totals().issuer_values.get(c.issuer, Decimal(0))
            if prior + shares * max(strike, _decimal(intent.mark_price or strike)) > (
                book.equity * self._ratio("MAX_POSITION_PCT")
            ):
                reasons.append("issuer_cap")
            book.cash -= shares * strike
        if book.cash < book.equity * self._ratio("MIN_FREE_CASH_PCT"):
            reasons.append("cash_floor")
        if book.totals().gross_exposure > book.equity * self._ratio("MAX_TOTAL_EXPOSURE"):
            reasons.append("gross_cap")
        tolerated = tuple(b for b in before if b.kind in {"issuer_cap", "sector_cap", "cluster_cap",
                                                         "position_count"})
        return RiskVerdict(not reasons, tuple(reasons), book.totals(),
                           allowed_existing_breaches=tolerated if not reasons else ())

    @staticmethod
    def _covered_shares(snapshot: "BookSnapshot", symbol: str) -> Decimal:
        total = Decimal(0)
        for leg in snapshot.legs:
            right = getattr(leg, "right", "")
            qty = _decimal(leg.qty)
            short = qty < 0 or getattr(leg, "side", "") == "short"
            if leg.underlying == symbol and str(right).lower() in {"c", "call"} and short:
                # A short call with no explicit stock coverage need not pledge stock.
                # Until the option manager clears it, never sell potential covering shares.
                total += abs(qty) * _decimal(leg.multiplier)
        return total

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
        price: Decimal, fee: Decimal, intent: TradeIntent, controls: RiskControls,
    ) -> RiskVerdict:
        if kind == TradeKind.OPTION_CLOSE:
            if not any(symbol in {leg.underlying, leg.occ_symbol}
                       and abs(leg.qty) >= qty for leg in snapshot.legs):
                return RiskVerdict(False, ("no_option_to_close",))
            debit = _decimal(intent.option_cash_debit)
            if debit < 0 or (debit + fee > 0 and debit + fee > self._reduction_cash(snapshot, controls)):
                return RiskVerdict(False, ("close_requires_cash",))
            return RiskVerdict(True)
        stock = next((held for held in snapshot.holdings if held.symbol == symbol), None)
        if stock is None:
            return RiskVerdict(False, ("no_position",))
        held = _decimal(stock.qty)
        if kind == TradeKind.BUY_TO_COVER:
            if held >= 0 or qty > -held:
                return RiskVerdict(False, ("cover_exceeds_short",))
            if price <= 0 or qty * price + fee > self._reduction_cash(snapshot, controls):
                return RiskVerdict(False, ("cover_requires_funded_limit",))
            return RiskVerdict(True)
        if held <= 0 or qty > min(held, _decimal(getattr(stock, "qty_available", held)),
                                   max(Decimal(0), held - self._covered_shares(snapshot, symbol))):
            return RiskVerdict(False, ("sell_exceeds_available_shares",))
        return RiskVerdict(True)

    @staticmethod
    def _reduction_cash(snapshot: "BookSnapshot", controls: RiskControls) -> Decimal:
        """No borrowing to close, even if market data is otherwise unavailable."""
        cash = _decimal(snapshot.cash)
        seen = set()
        for option in controls.option_commitments:
            cash -= _decimal(option.cash_reserved)
            seen.update(option.order_ids)
        for order in snapshot.open_orders:
            key = order.order_id or order.client_order_id
            if key in seen or order.client_order_id in seen:
                continue
            if key:
                seen.add(key)
            if order.side != "buy" and order.order_class != "mleg":
                continue
            limit = _decimal(order.limit_price)
            quantity = _decimal(order.remaining_qty)
            if quantity > 0:
                cash -= quantity * max(limit, Decimal(0)) * (
                    100 if order.order_class == "mleg" or order.asset_class == "us_option" else 1
                )
        return cash

    def can_open_position(
        self,
        state: PortfolioState,
        symbol: str,
        sector: str,
        price: float,
        conviction: int = 7,
    ) -> tuple[bool, str]:
        """Check whether a new or additional position is allowed.

        Returns (True, "ADD_TO_EXISTING") when topping up an existing
        position that is still below its effective target weight, or
        (True, "OK") for a new position.

        A hard per-symbol ceiling (config.MAX_POSITION_PCT) is always
        enforced — no new shares are acquired once a symbol crosses it,
        even if conviction is high.
        """
        existing = None
        for p in state.positions:
            if p.symbol == symbol:
                existing = p
                break

        portfolio_value = state.cash + sum(
            p.current_price * p.shares for p in state.positions
        )
        if portfolio_value <= 0:
            return False, "Portfolio value is zero or negative"

        hard_cap = self.config.MAX_POSITION_PCT
        conviction_target = self.config.CONVICTION_SIZE_MAP.get(
            conviction, self.config.CONVICTION_SIZE_MAP.get(7, 0.05)
        )
        # Effective target is the smaller of conviction-based size and hard cap.
        effective_target = min(conviction_target, hard_cap)

        if existing:
            current_weight = (existing.current_price * existing.shares) / portfolio_value
            # Hard cap check first — blocks additions even if conviction jumps.
            if current_weight >= hard_cap:
                return False, (
                    f"{symbol} at {current_weight:.1%} ≥ hard cap {hard_cap:.0%} — no adds"
                )
            if current_weight >= effective_target * 0.9:
                return False, (
                    f"Already holding {symbol} at {current_weight:.1%} "
                    f"(target {effective_target:.0%})"
                )
            return True, "ADD_TO_EXISTING"

        # 2. Position count (only for NEW positions, not add-ons)
        if len(state.positions) >= self.config.MAX_POSITIONS:
            return False, f"Max positions ({self.config.MAX_POSITIONS}) reached"

        # 3. Total exposure
        market_exposure = sum(p.current_price * p.shares for p in state.positions)
        exposure_pct = market_exposure / portfolio_value
        if exposure_pct >= self.config.MAX_TOTAL_EXPOSURE:
            return False, f"Total exposure {exposure_pct:.1%} >= limit {self.config.MAX_TOTAL_EXPOSURE:.0%}"

        # 4. Sector concentration (uses SECTOR_MAP as fallback if position
        # sector was not populated, so the rule works even on imported positions).
        sector_map = getattr(self.config, "SECTOR_MAP", {})
        def _sec_of(p):
            s = getattr(p, "sector", None) or "Unknown"
            if s == "Unknown":
                s = sector_map.get(p.symbol, "Unknown")
            return s
        resolved_sector = sector if sector and sector != "Unknown" else sector_map.get(symbol, "Unknown")
        sector_val = sum(
            p.current_price * p.shares
            for p in state.positions
            if _sec_of(p) == resolved_sector
        )
        sector_pct = sector_val / portfolio_value
        if resolved_sector != "Unknown" and sector_pct + (conviction_target or 0) * 0.5 >= self.config.MAX_SECTOR_PCT:
            # Block if adding a reasonable new position would push the sector
            # past the cap. (We check `sector_pct >= cap` AND a soft-block so
            # a single new full-size buy can't slam into the ceiling.)
            if sector_pct >= self.config.MAX_SECTOR_PCT:
                return False, (
                    f"Sector '{resolved_sector}' at {sector_pct:.1%} >= limit "
                    f"{self.config.MAX_SECTOR_PCT:.0%}"
                )

        # 4b. Correlation-cluster concentration. Symbols can belong to multiple
        # clusters (e.g. NVDA is both Semis and MegaCapTech); we check each.
        clusters = getattr(self.config, "SYMBOL_CLUSTERS", {}) or {}
        cluster_cap = getattr(self.config, "MAX_CLUSTER_PCT", 0.45)
        for cluster_name, members in clusters.items():
            if symbol not in members:
                continue
            cluster_val = sum(
                p.current_price * p.shares
                for p in state.positions
                if p.symbol in members
            )
            cluster_pct = cluster_val / portfolio_value
            if cluster_pct >= cluster_cap:
                return False, (
                    f"Cluster '{cluster_name}' at {cluster_pct:.1%} >= limit "
                    f"{cluster_cap:.0%} (includes {symbol})"
                )

        # 5. Enough cash for minimum order
        if state.cash < price:
            return False, f"Insufficient cash (${state.cash:,.0f}) for even 1 share at ${price:.2f}"

        return True, "OK"

    def calculate_position_size(
        self,
        portfolio_value: float,
        price: float,
        conviction: int,
    ) -> int:
        """Calculate number of shares using conviction-tiered sizing.

        Conviction → portfolio %:
            7 → 5%
            8 → 7%
            9 → 8%
            10 → 10%
        """
        if price <= 0 or portfolio_value <= 0:
            return 0

        # Look up conviction tier; default to minimum 3% for conviction 7
        size_pct = self.config.CONVICTION_SIZE_MAP.get(
            conviction, self.config.CONVICTION_SIZE_MAP.get(7, 0.03)
        )
        dollar_amount = portfolio_value * size_pct
        shares = int(dollar_amount / price)

        return max(shares, 0)

    def get_stop_loss_price(self, entry_price: float, beta: float | None = None) -> float:
        """Calculate volatility-adjusted stop-loss price.

        Formula: stop_pct = beta * 5%, clamped to [5%, 10%].
        If beta is not available, defaults to 5%.
        """
        if beta is not None and beta > 0:
            stop_pct = beta * 0.05
            # Clamp to configured range
            stop_pct = max(self.config.STOP_LOSS_PCT_MIN, min(stop_pct, self.config.STOP_LOSS_PCT_MAX))
        else:
            stop_pct = self.config.STOP_LOSS_PCT_MIN  # Default 5%

        return round(entry_price * (1 - stop_pct), 2)

    def check_pdt_compliance(self, trade_history: list[Trade]) -> bool:
        """Check Pattern Day Trader rule: max 3 day trades per rolling 5 business days.

        A day trade = buying and selling the same security on the same day.
        """
        now = datetime.now(timezone.utc)
        five_days_ago = now - timedelta(days=7)  # 7 calendar days ≈ 5 business days

        recent_trades = [
            t for t in trade_history
            if t.timestamp >= five_days_ago
        ]

        # Find day trades: same symbol bought and sold on same calendar day
        day_trades = 0
        buys_by_day: dict[str, set[str]] = {}  # date_str -> set of symbols bought
        sells_by_day: dict[str, set[str]] = {}

        for t in recent_trades:
            day_key = t.timestamp.strftime("%Y-%m-%d")
            if t.action == "BUY":
                buys_by_day.setdefault(day_key, set()).add(t.symbol)
            elif t.action == "SELL":
                sells_by_day.setdefault(day_key, set()).add(t.symbol)

        for day_key, buy_syms in buys_by_day.items():
            sell_syms = sells_by_day.get(day_key, set())
            day_trades += len(buy_syms & sell_syms)

        compliant = day_trades < 3
        if not compliant:
            logger.warning("PDT limit reached: %d day trades in last 5 days", day_trades)
        return compliant

    def should_stop_loss(self, entry_price: float, current_price: float, beta: float | None = None) -> bool:
        """Check if current price has breached stop-loss level."""
        stop = self.get_stop_loss_price(entry_price, beta)
        return current_price <= stop

    def check_position_health(
        self,
        state: PortfolioState,
    ) -> list[dict]:
        """Check all positions for stop-loss breaches. Returns list of alerts."""
        alerts = []
        for pos in state.positions:
            if pos.current_price <= 0:
                continue
            # Use beta from thesis if available
            beta = getattr(pos.thesis, "beta", None) if pos.thesis else None
            if self.should_stop_loss(pos.entry_price, pos.current_price, beta):
                alerts.append({
                    "symbol": pos.symbol,
                    "action": "STOP_LOSS",
                    "entry_price": pos.entry_price,
                    "current_price": pos.current_price,
                    "loss_pct": (pos.current_price - pos.entry_price) / pos.entry_price,
                    "reason": f"Stop-loss triggered: ${pos.current_price:.2f} <= ${self.get_stop_loss_price(pos.entry_price, beta):.2f}",
                })
        return alerts
