from __future__ import annotations

import warnings
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import Field, field_validator, model_validator

from investment_manager.execution.lifecycle.exit_policy import program_exit_triggered
from investment_manager.execution.models import (
    AccountSnapshot,
    OrderType,
    Position,
    Side,
)
from investment_manager.information.models import IntelligenceEvent
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import require_utc
from investment_manager.kernel.types import (
    FrozenModel,
    floor_to_step,
)
from investment_manager.legacy.decision import estimate_round_trip_cost_amount
from investment_manager.legacy.models import (
    Action,
    SignalCandidate,
    TradeIntent,
)
from investment_manager.legacy.risk import RiskEngine
from investment_manager.legacy.strategy import PriceTrendStrategy, Strategy
from investment_manager.market.features import FeatureEngine
from investment_manager.market.models import (
    MarketBar,
    MarketSnapshot,
)
from investment_manager.research.dataset import (
    HistoricalDataset,
    HistoricalEventDataset,
    HistoricalFundingDataset,
    InstrumentSpec,
)
from investment_manager.risk.models import RiskOutcome
from investment_manager.settings import AppConfig

BACKTEST_MODEL_VERSION = "investment-manager-bar-backtest-v11"


class ResearchStrategy(Strategy, Protocol):
    """A program strategy with a frozen, serializable historical identity."""

    @property
    def research_spec(self) -> object: ...


class BacktestTrade(FrozenModel):
    candidate_id: str
    signal_at: datetime
    opened_at: datetime
    closed_at: datetime
    entry_price: Decimal = Field(gt=0)
    exit_price: Decimal = Field(gt=0)
    quantity: Decimal = Field(gt=0)
    gross_pnl: Decimal
    modeled_cost: Decimal = Field(ge=0)
    net_pnl: Decimal
    gross_return_bps: Decimal
    net_return_bps: Decimal
    exit_reason: Literal["STOP_LOSS", "PROGRAM_SIGNAL", "MAX_HOLDING_TIME"]

    _utc_signal_at = field_validator("signal_at")(require_utc)
    _utc_opened_at = field_validator("opened_at")(require_utc)
    _utc_closed_at = field_validator("closed_at")(require_utc)

    @model_validator(mode="after")
    def times_are_ordered(self):
        if not self.signal_at < self.opened_at <= self.closed_at:
            raise ValueError("回测交易时间顺序非法")
        return self


class BacktestMetrics(FrozenModel):
    starting_equity: Decimal = Field(gt=0)
    ending_equity: Decimal = Field(gt=0)
    # Optional only so immutable v1-v8 artifacts remain readable. New runs always
    # populate both fields and validate the cost decomposition below.
    gross_pnl: Decimal | None = None
    modeled_cost: Decimal | None = Field(default=None, ge=0)
    net_pnl: Decimal
    return_fraction: Decimal
    trade_count: int = Field(ge=0)
    win_rate: Decimal | None = Field(default=None, ge=0, le=1)
    profit_factor: Decimal | None = Field(default=None, ge=0)
    maximum_drawdown_fraction: Decimal = Field(ge=0)
    average_gross_return_bps: Decimal | None = None
    average_modeled_cost_bps: Decimal | None = Field(default=None, ge=0)
    average_net_return_bps: Decimal | None = None
    average_net_return_bps_lower_bound: Decimal | None = None
    benchmark_buy_hold_bps: Decimal

    @model_validator(mode="after")
    def pnl_decomposition_matches(self):
        if (self.gross_pnl is None) != (self.modeled_cost is None):
            raise ValueError("回测毛收益与模型化成本必须同时存在或同时缺失")
        if (
            self.gross_pnl is not None
            and self.modeled_cost is not None
            and self.gross_pnl - self.modeled_cost != self.net_pnl
        ):
            raise ValueError("回测净收益必须等于毛收益减模型化成本")
        return self


class BacktestRun(FrozenModel):
    run_id: str
    dataset_id: str
    event_dataset_id: str | None = None
    funding_dataset_id: str | None = None
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine: str
    engine_version: str
    backtest_model_version: str = "investment-manager-bar-backtest-v1"
    symbol: str
    interval: str
    signal_start: datetime
    signal_end: datetime
    completed: bool
    signal_count: int = Field(ge=0)
    rejection_counts: tuple[tuple[str, int], ...]
    terminal_candidate_ids: tuple[str, ...] = ()
    terminal_candidate_signals: tuple[datetime, ...] = ()
    order_failure_reasons: tuple[str, ...] = ()
    assumptions: tuple[str, ...]
    trades: tuple[BacktestTrade, ...]
    metrics: BacktestMetrics

    _utc_signal_start = field_validator("signal_start")(require_utc)
    _utc_signal_end = field_validator("signal_end")(require_utc)


def artifact_hash(config: AppConfig, *, strategy_spec: object | None = None) -> str:
    """只绑定会改变历史策略输出、规模或成本的冻结组件。"""

    return content_hash(
        {
            "feature": config.feature,
            "strategy": strategy_spec if strategy_spec is not None else config.strategy,
            "frequency": config.frequency,
            "risk": config.risk,
            "execution": config.execution,
            "backtest_model_version": BACKTEST_MODEL_VERSION,
        }
    )


def run_bar_backtest(
    *,
    dataset: HistoricalDataset,
    event_dataset: HistoricalEventDataset | None = None,
    funding_dataset: HistoricalFundingDataset | None = None,
    config: AppConfig,
    signal_start: datetime,
    signal_end: datetime,
    replay_start: datetime | None = None,
    replay_end: datetime | None = None,
    starting_equity: Decimal = Decimal("10000"),
    spread_bps: Decimal = Decimal("1"),
    strategy: ResearchStrategy | None = None,
) -> BacktestRun:
    """用 NautilusTrader 撮合冻结的 Investment Manager 程序策略；不调用 Codex。"""

    if not isinstance(dataset, HistoricalDataset):
        raise TypeError("精确回测只能使用完成全量验证的 HistoricalDataset")

    try:
        import nautilus_trader
        from nautilus_trader.backtest.config import BacktestEngineConfig
        from nautilus_trader.backtest.engine import BacktestEngine
        from nautilus_trader.config import LoggingConfig
        from nautilus_trader.model.enums import AccountType, OmsType
        from nautilus_trader.model.identifiers import Venue
        from nautilus_trader.model.objects import Money
    except ModuleNotFoundError as exc:  # pragma: no cover - 环境契约由 CLI 覆盖
        raise RuntimeError("回测需要安装 investment-manager[research]") from exc

    signal_start = require_utc(signal_start)
    signal_end = require_utc(signal_end)
    replay_start = require_utc(replay_start) if replay_start is not None else None
    replay_end = require_utc(replay_end) if replay_end is not None else None
    if signal_start >= signal_end:
        raise ValueError("回测信号窗口起点必须早于终点")
    if spread_bps < 0:
        raise ValueError("回测价差不能为负")
    if starting_equity <= 0:
        raise ValueError("回测初始权益必须为正")
    if dataset.manifest.symbol not in config.risk.symbol_allowlist:
        raise ValueError("回测品种不在当前 RiskPolicy allowlist")
    if dataset.manifest.interval != config.market_data.interval:
        raise ValueError("历史数据周期与冻结 MarketDataPolicy 不一致")

    if replay_start is not None and replay_end is not None and replay_start >= replay_end:
        raise ValueError("回放窗口起点必须早于终点")
    if replay_start is not None and replay_start > signal_start:
        raise ValueError("回放必须覆盖信号窗口之前的预热数据")
    if replay_end is not None and replay_end < signal_end:
        raise ValueError("回放必须覆盖完整信号窗口")
    effective_replay_start = replay_start or dataset.manifest.first_open_time
    effective_replay_end = replay_end or (
        dataset.manifest.last_close_time + timedelta(microseconds=1)
    )
    if event_dataset is not None and (
        event_dataset.manifest.requested_start > effective_replay_start
        or event_dataset.manifest.requested_end < effective_replay_end
    ):
        raise ValueError("历史事件数据集必须覆盖完整回放窗口")
    if funding_dataset is not None and (
        funding_dataset.manifest.symbol != dataset.manifest.symbol
        or funding_dataset.manifest.requested_start > effective_replay_start
        or funding_dataset.manifest.requested_end < effective_replay_end
    ):
        raise ValueError("历史资金费率数据集必须匹配品种并覆盖完整回放窗口")

    instrument = _build_instrument(dataset.manifest.instrument)
    bar_type, events = _to_nautilus_events(
        dataset,
        instrument,
        replay_start=replay_start,
        replay_end=replay_end,
    )
    core_strategy: ResearchStrategy = strategy or PriceTrendStrategy(config.strategy)
    strategy_spec = core_strategy.research_spec
    expected_funding_dataset_id = getattr(
        strategy_spec,
        "funding_dataset_id",
        None,
    )
    observed_funding_dataset_id = (
        funding_dataset.manifest.dataset_id if funding_dataset is not None else None
    )
    if expected_funding_dataset_id != observed_funding_dataset_id:
        raise ValueError("历史策略与资金费率数据集身份不一致")
    adapter = _BarBacktestStrategy(
        _AdapterConfig(
            instrument_id=instrument.id,
            bar_type=bar_type,
            signal_start_ns=_to_nanoseconds(signal_start),
            signal_end_ns=_to_nanoseconds(signal_end),
        ),
        app_config=config,
        core_strategy=core_strategy,
        events=event_dataset.events if event_dataset is not None else (),
        starting_equity=starting_equity,
        spread_bps=spread_bps,
    )
    engine = BacktestEngine(
        BacktestEngineConfig(
            logging=LoggingConfig(bypass_logging=True),
        )
    )
    engine.add_venue(
        venue=Venue("BINANCE"),
        oms_type=OmsType.NETTING,
        account_type=AccountType.CASH,
        starting_balances=[Money(starting_equity, instrument.quote_currency)],
        base_currency=None,
        reject_stop_orders=False,
        bar_execution=True,
        trade_execution=True,
        use_random_ids=False,
    )
    engine.add_instrument(instrument)
    engine.add_strategy(adapter)
    engine.add_data(events, sort=True)
    try:
        # Nautilus 1.231.0 内部仍调用 pandas.Timestamp.utcnow；精确屏蔽其已知弃用告警，
        # 避免污染 CLI 的机器可读 JSON。其他告警保持可见。
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"Timestamp\.utcnow is deprecated.*",
            )
            engine.run()
        completed = adapter.completed
        metrics = _metrics(
            trades=adapter.trades,
            starting_equity=starting_equity,
            ending_equity=adapter.adjusted_equity,
            maximum_drawdown=adapter.maximum_drawdown_fraction,
            benchmark_buy_hold_bps=_benchmark(dataset, signal_start, signal_end),
        )
    finally:
        engine.dispose()

    frozen_artifact = artifact_hash(config, strategy_spec=strategy_spec)
    run_identity = {
        "dataset_id": dataset.manifest.dataset_id,
        "event_dataset_id": (
            event_dataset.manifest.dataset_id if event_dataset is not None else None
        ),
        "funding_dataset_id": observed_funding_dataset_id,
        "artifact_hash": frozen_artifact,
        "engine": "nautilus-trader",
        "engine_version": nautilus_trader.__version__,
        "signal_start": signal_start,
        "signal_end": signal_end,
        "replay_start": replay_start,
        "replay_end": replay_end,
        "starting_equity": starting_equity,
        "spread_bps": spread_bps,
    }
    return BacktestRun(
        run_id=stable_id("historical_backtest", run_identity),
        dataset_id=dataset.manifest.dataset_id,
        event_dataset_id=(
            event_dataset.manifest.dataset_id if event_dataset is not None else None
        ),
        funding_dataset_id=observed_funding_dataset_id,
        artifact_hash=frozen_artifact,
        engine="nautilus-trader",
        engine_version=nautilus_trader.__version__,
        backtest_model_version=BACKTEST_MODEL_VERSION,
        symbol=dataset.manifest.symbol,
        interval=dataset.manifest.interval,
        signal_start=signal_start,
        signal_end=signal_end,
        completed=completed,
        signal_count=adapter.signal_count,
        rejection_counts=tuple(sorted(adapter.rejection_counts.items())),
        terminal_candidate_ids=adapter.terminal_candidate_ids,
        terminal_candidate_signals=adapter.terminal_candidate_signals,
        order_failure_reasons=tuple(adapter.order_failure_reasons),
        assumptions=(
            "BAR_CLOSE_SIGNAL_NEXT_BAR_OPEN",
            "ONE_POSITION_PER_SYMBOL",
            "ROUND_TRIP_COST_POST_PROCESSED_FROM_FROZEN_POLICIES",
            "INTRABAR_STOP_MATCHED_BY_NAUTILUS_BAR_ENGINE",
            "PROTECTION_SIZED_FROM_FINAL_ENTRY_POSITION",
            "PROGRAM_EXIT_EVALUATED_FROM_MATCHED_CLOSED_BARS",
            "DRAWDOWN_MARKED_TO_EACH_BAR_CLOSE",
            "DAILY_AND_COOLDOWN_FREQUENCY_LIMITS_APPLIED",
            "CALIBRATION_EDGE_GATE_EXCLUDED_FOR_RAW_SIGNAL_EVALUATION",
            "NO_CODEX_REPLAY",
        )
        + (
            (
                "EVENTS_VISIBLE_ONLY_AFTER_OBSERVED_AT",
                "EVENT_STRATEGY_EVALUATED_AT_BAR_CLOSE",
                "EVENT_VISIBILITY_MATCHES_PRODUCTION_100_ITEM_BOUND",
                "TRIGGER_PLAN_NOT_REPLAYED_BAR_CLOCK_ONLY",
            )
            if event_dataset is not None
            else ()
        )
        + (
            ("FUNDING_VISIBLE_AFTER_FROZEN_SETTLEMENT_LAG",)
            if funding_dataset is not None
            else ()
        ),
        trades=adapter.trades,
        metrics=metrics,
    )


def _build_instrument(spec: InstrumentSpec):
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, Venue
    from nautilus_trader.model.instruments import CurrencyPair
    from nautilus_trader.model.objects import Currency, Money, Price, Quantity

    price_precision = _decimal_precision(spec.price_increment)
    size_precision = _decimal_precision(spec.quantity_increment)
    quote = Currency.from_str(spec.quote_asset)
    return CurrencyPair(
        instrument_id=InstrumentId(Symbol(spec.symbol), Venue("BINANCE")),
        raw_symbol=Symbol(spec.symbol),
        base_currency=Currency.from_str(spec.base_asset),
        quote_currency=quote,
        price_precision=price_precision,
        size_precision=size_precision,
        price_increment=Price.from_str(_fixed(spec.price_increment, price_precision)),
        size_increment=Quantity.from_str(_fixed(spec.quantity_increment, size_precision)),
        lot_size=None,
        max_quantity=Quantity.from_str(_fixed(spec.maximum_quantity, size_precision)),
        min_quantity=Quantity.from_str(_fixed(spec.minimum_quantity, size_precision)),
        max_notional=None,
        min_notional=Money(spec.minimum_notional, quote),
        max_price=Price.from_str(_fixed(spec.maximum_price, price_precision)),
        min_price=Price.from_str(_fixed(spec.minimum_price, price_precision)),
        margin_init=Decimal("0"),
        margin_maint=Decimal("0"),
        # 完整成本由 Investment Manager 冻结口径统一后处理，避免双重扣费。
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0"),
        ts_event=0,
        ts_init=0,
    )


def _to_nautilus_events(
    dataset: HistoricalDataset,
    instrument,
    *,
    replay_start: datetime | None,
    replay_end: datetime | None,
) -> tuple[Any, list[Any]]:
    from nautilus_trader.model.data import Bar, BarType, QuoteTick
    from nautilus_trader.model.objects import Quantity

    interval = dataset.manifest.interval
    if interval.endswith("d"):
        aggregation = f"{interval[:-1]}-DAY"
    elif interval.endswith("h"):
        aggregation = f"{interval[:-1]}-HOUR"
    else:
        aggregation = f"{interval[:-1]}-MINUTE"
    bar_type = BarType.from_str(
        f"{instrument.id}-{aggregation}-LAST-EXTERNAL"
    )
    size_precision = instrument.size_precision
    quote_size = Quantity.from_str(
        _fixed(max(instrument.min_quantity.as_decimal(), Decimal("1")), size_precision)
    )
    events: list[Any] = []
    for item in dataset.bars:
        if replay_start is not None and item.close_time < replay_start:
            continue
        if replay_end is not None and item.open_time >= replay_end:
            break
        open_ns = _to_nanoseconds(item.open_time)
        close_ns = _to_nanoseconds(item.close_time)
        open_price = instrument.make_price(item.open)
        events.append(
            QuoteTick(
                instrument.id,
                open_price,
                open_price,
                quote_size,
                quote_size,
                open_ns,
                open_ns,
            )
        )
        events.append(
            Bar(
                bar_type,
                instrument.make_price(item.open),
                instrument.make_price(item.high),
                instrument.make_price(item.low),
                instrument.make_price(item.close),
                Quantity.from_str(_fixed(item.volume, size_precision)),
                close_ns,
                close_ns,
            )
        )
    return bar_type, events


def _metrics(
    *,
    trades: tuple[BacktestTrade, ...],
    starting_equity: Decimal,
    ending_equity: Decimal,
    maximum_drawdown: Decimal,
    benchmark_buy_hold_bps: Decimal,
) -> BacktestMetrics:
    gross = sum((item.gross_pnl for item in trades), Decimal("0"))
    modeled_cost = sum((item.modeled_cost for item in trades), Decimal("0"))
    average_gross_bps = (
        sum((item.gross_return_bps for item in trades), Decimal("0"))
        / Decimal(len(trades))
        if trades
        else None
    )
    average_net_bps = (
        sum((item.net_return_bps for item in trades), Decimal("0"))
        / Decimal(len(trades))
        if trades
        else None
    )
    net = ending_equity - starting_equity
    wins = [item.net_pnl for item in trades if item.net_pnl > 0]
    losses = [-item.net_pnl for item in trades if item.net_pnl < 0]
    profit_factor = None
    if losses:
        profit_factor = sum(wins, Decimal("0")) / sum(losses, Decimal("0"))
    return BacktestMetrics(
        starting_equity=starting_equity,
        ending_equity=ending_equity,
        gross_pnl=gross,
        modeled_cost=modeled_cost,
        net_pnl=net,
        return_fraction=net / starting_equity,
        trade_count=len(trades),
        win_rate=(
            Decimal(len(wins)) / Decimal(len(trades))
            if trades
            else None
        ),
        profit_factor=profit_factor,
        maximum_drawdown_fraction=maximum_drawdown,
        average_gross_return_bps=average_gross_bps,
        average_modeled_cost_bps=(
            average_gross_bps - average_net_bps
            if average_gross_bps is not None and average_net_bps is not None
            else None
        ),
        average_net_return_bps=average_net_bps,
        average_net_return_bps_lower_bound=_mean_lower_bound(
            tuple(item.net_return_bps for item in trades)
        ),
        benchmark_buy_hold_bps=benchmark_buy_hold_bps,
    )


def _mean_lower_bound(
    values: tuple[Decimal, ...], *, z: Decimal = Decimal("1.96")
) -> Decimal | None:
    """Normal-approximation lower bound over non-overlapping realized trades."""

    if len(values) < 2:
        return None
    count = Decimal(len(values))
    mean = sum(values, Decimal("0")) / count
    variance = sum(((item - mean) ** 2 for item in values), Decimal("0")) / (
        count - 1
    )
    standard_error = (variance / count).sqrt()
    return mean - z * standard_error


def _benchmark(
    dataset: HistoricalDataset, signal_start: datetime, signal_end: datetime
) -> Decimal:
    visible = [
        item
        for item in dataset.bars
        if item.open_time >= signal_start and item.close_time < signal_end
    ]
    if len(visible) < 2:
        return Decimal("0")
    return (visible[-1].close / visible[0].open - 1) * Decimal("10000")


def _interval_minutes(interval: str) -> int:
    unit = interval[-1]
    value = int(interval[:-1])
    if unit == "m":
        return value
    if unit == "h":
        return value * 60
    if unit == "d":
        return value * 1440
    raise ValueError(f"Nautilus K 线回测不支持周期: {interval}")


def _decimal_precision(value: Decimal) -> int:
    return max(0, -value.normalize().as_tuple().exponent)


def _fixed(value: Decimal, precision: int) -> str:
    return f"{value:.{precision}f}"


def _to_nanoseconds(value: datetime) -> int:
    return int(require_utc(value).timestamp() * 1_000_000_000)


def _from_nanoseconds(value: int) -> datetime:
    seconds, nanos = divmod(value, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanos // 1000)


# Nautilus 是可选研究依赖；类定义只在显式导入本模块时加载。
from nautilus_trader.config import StrategyConfig  # noqa: E402
from nautilus_trader.model.data import Bar, QuoteTick  # noqa: E402
from nautilus_trader.model.enums import OrderSide, TimeInForce  # noqa: E402
from nautilus_trader.model.events import (  # noqa: E402
    OrderCanceled,
    OrderDenied,
    OrderFilled,
    OrderRejected,
    PositionChanged,
    PositionClosed,
    PositionOpened,
)
from nautilus_trader.trading.strategy import Strategy as NautilusStrategy  # noqa: E402


class _AdapterConfig(StrategyConfig, frozen=True):
    instrument_id: Any
    bar_type: Any
    signal_start_ns: int
    signal_end_ns: int


class _BarBacktestStrategy(NautilusStrategy):
    def __init__(
        self,
        config: _AdapterConfig,
        *,
        app_config: AppConfig,
        core_strategy: Strategy,
        events: tuple[IntelligenceEvent, ...],
        starting_equity: Decimal,
        spread_bps: Decimal,
    ) -> None:
        super().__init__(config)
        self._app = app_config
        self._core_strategy = core_strategy
        self._events = events
        self._event_cursor = 0
        self._observed_events: list[IntelligenceEvent] = []
        self._strategy_events: tuple[IntelligenceEvent, ...] = ()
        self._spread_bps = spread_bps
        self._bars: deque[MarketBar] = deque(maxlen=app_config.market_data.bar_window)
        self._pending: SignalCandidate | None = None
        self._active: SignalCandidate | None = None
        self._active_entry_price: Decimal | None = None
        self._active_quantity: Decimal | None = None
        self._entry_order_id: Any | None = None
        self._stop_order_id: Any | None = None
        self._horizon_timer: str | None = None
        self._forced_exit_reason: Literal[
            "PROGRAM_SIGNAL", "MAX_HOLDING_TIME"
        ] | None = None
        self._equity = starting_equity
        self._high_water = starting_equity
        self._maximum_drawdown = Decimal("0")
        self._daily_pnl: dict[object, Decimal] = {}
        self._daily_entries: dict[object, int] = {}
        self._last_entry_at: datetime | None = None
        self._trades: list[BacktestTrade] = []
        self.order_failure_reasons: list[str] = []
        self.signal_count = 0
        self.rejection_counts: dict[str, int] = {}

    @property
    def trades(self) -> tuple[BacktestTrade, ...]:
        return tuple(self._trades)

    @property
    def adjusted_equity(self) -> Decimal:
        return self._equity

    @property
    def maximum_drawdown_fraction(self) -> Decimal:
        return self._maximum_drawdown

    @property
    def terminal_candidate_ids(self) -> tuple[str, ...]:
        return tuple(
            item.candidate_id for item in (self._pending, self._active) if item is not None
        )

    @property
    def terminal_candidate_signals(self) -> tuple[datetime, ...]:
        return tuple(
            item.signal_observed_at for item in (self._pending, self._active) if item is not None
        )

    @property
    def completed(self) -> bool:
        return self._pending is None and self._active is None

    def on_start(self) -> None:
        self.subscribe_bars(self.config.bar_type)
        self.subscribe_quote_ticks(self.config.instrument_id)

    def on_bar(self, bar: Bar) -> None:
        at = _from_nanoseconds(bar.ts_event)
        self._advance_events(at)
        current = MarketBar(
            event_time=at - timedelta(minutes=_interval_minutes(self._app.market_data.interval)),
            observed_at=at,
            open=Decimal(str(bar.open)),
            high=Decimal(str(bar.high)),
            low=Decimal(str(bar.low)),
            close=Decimal(str(bar.close)),
            volume=Decimal(str(bar.volume)),
        )
        self._bars.append(current)
        if self._active is not None and self._active_entry_price and self._active_quantity:
            modeled_cost = self._modeled_cost(
                self._active_entry_price,
                current.close,
                self._active_quantity,
                entry_order_type=self._active.entry.order_type,
            )
            marked_equity = self._equity + self._active_quantity * (
                current.close - self._active_entry_price
            ) - modeled_cost
            self._observe_equity(marked_equity)
            if self._forced_exit_reason is None and program_exit_triggered(
                self._active.program_exit,
                self._market_at_close(bar, at),
            ):
                self._request_forced_exit("PROGRAM_SIGNAL")
        if not (self.config.signal_start_ns <= bar.ts_event < self.config.signal_end_ns):
            return
        if len(self._bars) < self._app.market_data.bar_window:
            return
        if self._pending is not None or self._active is not None:
            return
        if not self.portfolio.is_flat(self.config.instrument_id):
            return
        market = self._market_at_close(bar, at)
        account = self._account(cycle_id=market.cycle_id, as_of=at)
        features = FeatureEngine(self._app.feature).compute(market)
        candidates = self._core_strategy.evaluate(
            market=market,
            account=account,
            features=features,
            events=self._strategy_events,
        )
        if not candidates:
            return
        self.signal_count += len(candidates)
        if len(candidates) != 1:
            self._reject("MULTIPLE_PROGRAM_CANDIDATES")
            return
        self._pending = candidates[0]

    def _advance_events(self, at: datetime) -> None:
        changed = False
        while (
            self._event_cursor < len(self._events)
            and self._events[self._event_cursor].observed_at <= at
        ):
            event = self._events[self._event_cursor]
            self._event_cursor += 1
            if self._app.market_data.symbols and self.config.instrument_id.symbol.value not in (
                event.symbols
            ):
                continue
            self._observed_events.append(event)
            changed = True
        if not changed:
            return
        self._observed_events = sorted(
            self._observed_events,
            key=lambda item: (item.event_time, item.evidence_id),
            reverse=True,
        )[:100]
        self._strategy_events = tuple(
            sorted(
                self._observed_events,
                key=lambda item: (item.event_time, item.evidence_id),
            )
        )

    def on_quote_tick(self, tick: QuoteTick) -> None:
        candidate = self._pending
        if candidate is None or tick.ts_event <= _to_nanoseconds(candidate.signal_observed_at):
            return
        at = _from_nanoseconds(tick.ts_event)
        self._pending = None
        if candidate.valid_until <= at:
            self._reject("CANDIDATE_EXPIRED_BEFORE_NEXT_OPEN")
            return
        if self._daily_entries.get(at.date(), 0) >= self._app.frequency.maximum_orders_per_day:
            self._reject("DAILY_ORDER_BUDGET_EXHAUSTED")
            return
        if self._last_entry_at is not None and (
            at - self._last_entry_at
        ) < timedelta(minutes=self._app.frequency.cooldown_minutes):
            self._reject("SYMBOL_COOLDOWN_ACTIVE")
            return
        market = MarketSnapshot(
            cycle_id=candidate.cycle_id,
            symbol=candidate.symbol,
            as_of=at,
            observed_at=at,
            bid=Decimal(str(tick.bid_price)),
            ask=Decimal(str(tick.ask_price)),
            last=(Decimal(str(tick.bid_price)) + Decimal(str(tick.ask_price))) / 2,
            bars=tuple(self._bars),
            source="nautilus-historical-quote",
        )
        intent = TradeIntent(
            intent_id=stable_id("backtest_intent", candidate.candidate_id),
            cycle_id=candidate.cycle_id,
            pipeline_version=self._app.pipeline.version,
            composition_policy_version=self._app.composition.version,
            action=Action.OPEN,
            symbol=candidate.symbol,
            side=Side.BUY,
            candidate_ids=(candidate.candidate_id,),
            entry=candidate.entry,
            stop_price=candidate.stop_price,
            max_holding_minutes=candidate.horizon_minutes,
            valid_until=candidate.valid_until,
            signal_observed_at=candidate.signal_observed_at,
            reference_price=candidate.reference_price,
            expected_edge_half_life_seconds=candidate.expected_edge_half_life_seconds,
            expected_gross_bps=candidate.expected_gross_bps,
            program_exit=candidate.program_exit,
        )
        risk = RiskEngine(self._app.risk).evaluate(
            intent=intent,
            market=market,
            account=self._account(cycle_id=candidate.cycle_id, as_of=at),
        )
        if risk.outcome != RiskOutcome.APPROVED or risk.quantity is None:
            failed = next(
                (item.reason_code for item in risk.rule_results if item.state.value != "PASS"),
                "RISK_REJECTED",
            )
            self._reject(failed)
            return
        instrument = self.cache.instrument(self.config.instrument_id)
        step = instrument.size_increment.as_decimal()
        executable_quantity = floor_to_step(risk.quantity, step)
        minimum_notional = max(
            self._app.risk.minimum_order_notional,
            instrument.min_notional.as_decimal(),
        )
        if executable_quantity <= 0 or executable_quantity * market.ask < minimum_notional:
            self._reject("ORDER_BELOW_EXCHANGE_MINIMUM_AFTER_QUANTIZATION")
            return
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.BUY,
            quantity=instrument.make_qty(executable_quantity),
            time_in_force=TimeInForce.GTC,
        )
        self._active = candidate
        self._entry_order_id = order.client_order_id
        self.submit_order(order)

    def on_position_opened(self, event: PositionOpened) -> None:
        self._sync_filled_entry(event.position_id)

    def on_position_changed(self, event: PositionChanged) -> None:
        self._sync_filled_entry(event.position_id)

    def on_order_filled(self, event: OrderFilled) -> None:
        if event.client_order_id == self._entry_order_id:
            self._sync_filled_entry(event.position_id)

    def _sync_filled_entry(self, position_id) -> None:
        """在市价开仓全部成交后，按最终净持仓一次性建立保护。"""

        if self._active is None or self._stop_order_id is not None:
            return
        position = self.cache.position(position_id)
        if position is None or not position.is_open:
            return
        self._active_entry_price = Decimal(str(position.avg_px_open))
        self._active_quantity = Decimal(str(position.quantity))
        entry = self.cache.order(self._entry_order_id) if self._entry_order_id else None
        # 一笔市价单可能对应多次 PositionOpened/Changed。首个部分成交就挂止损，
        # 会让保护数量小于最终持仓并在退出时留下无法成交的 dust。
        if entry is None or not entry.is_closed:
            return
        self._arm_entry_protection(_from_nanoseconds(position.ts_opened))

    def _arm_entry_protection(self, opened_at: datetime) -> None:
        if (
            self._active is None
            or self._active_quantity is None
            or self._stop_order_id is not None
        ):
            return
        instrument = self.cache.instrument(self.config.instrument_id)
        self._last_entry_at = opened_at
        self._daily_entries[opened_at.date()] = self._daily_entries.get(opened_at.date(), 0) + 1
        stop = self.order_factory.stop_market(
            instrument_id=self.config.instrument_id,
            order_side=OrderSide.SELL,
            quantity=instrument.make_qty(self._active_quantity),
            trigger_price=instrument.make_price(self._active.stop_price),
            time_in_force=TimeInForce.GTC,
        )
        self._stop_order_id = stop.client_order_id
        self.submit_order(stop)
        self._horizon_timer = f"horizon-{self._active.candidate_id}"
        self.clock.set_time_alert(
            self._horizon_timer,
            opened_at + timedelta(minutes=self._active.horizon_minutes),
            callback=self._on_horizon,
        )

    def _on_horizon(self, _event) -> None:
        if self._active is None:
            return
        self._request_forced_exit("MAX_HOLDING_TIME")

    def _request_forced_exit(
        self,
        reason: Literal["PROGRAM_SIGNAL", "MAX_HOLDING_TIME"],
    ) -> None:
        if self._active is None or self._forced_exit_reason is not None:
            return
        self._forced_exit_reason = reason
        stop = self.cache.order(self._stop_order_id) if self._stop_order_id else None
        if stop is not None and not stop.is_closed:
            # 现货保护单会锁定基础资产；必须等取消事实落地后再提交市价退出。
            self.cancel_order(stop)
            return
        self.close_all_positions(self.config.instrument_id)

    def on_order_canceled(self, event: OrderCanceled) -> None:
        if self._forced_exit_reason and event.client_order_id == self._stop_order_id:
            self.close_all_positions(self.config.instrument_id)
        elif event.client_order_id == self._entry_order_id:
            position = self.cache.position_for_order(event.client_order_id)
            if position is not None:
                self._sync_filled_entry(position.id)

    def on_order_denied(self, event: OrderDenied) -> None:
        self._handle_order_failure(
            event.client_order_id,
            "NAUTILUS_ORDER_DENIED",
            str(event.reason),
        )

    def on_order_rejected(self, event: OrderRejected) -> None:
        self._handle_order_failure(
            event.client_order_id,
            "NAUTILUS_ORDER_REJECTED",
            str(event.reason),
        )

    def on_position_closed(self, event: PositionClosed) -> None:
        candidate = self._active
        if candidate is None or self._active_quantity is None:
            return
        entry = Decimal(str(event.avg_px_open))
        exit_price = Decimal(str(event.avg_px_close))
        # PositionClosed.quantity 已归零；冻结开仓成交量才是该笔交易的真实规模。
        quantity = self._active_quantity
        notional = entry * quantity
        gross = quantity * (exit_price - entry)
        modeled_cost = self._modeled_cost(
            entry,
            exit_price,
            quantity,
            entry_order_type=candidate.entry.order_type,
        )
        net = gross - modeled_cost
        closed_at = _from_nanoseconds(event.ts_closed)
        trade = BacktestTrade(
            candidate_id=candidate.candidate_id,
            signal_at=candidate.signal_observed_at,
            opened_at=_from_nanoseconds(event.ts_opened),
            closed_at=closed_at,
            entry_price=entry,
            exit_price=exit_price,
            quantity=quantity,
            gross_pnl=gross,
            modeled_cost=modeled_cost,
            net_pnl=net,
            gross_return_bps=(exit_price / entry - 1) * Decimal("10000"),
            net_return_bps=(net / notional) * Decimal("10000"),
            exit_reason=self._forced_exit_reason or "STOP_LOSS",
        )
        self._trades.append(trade)
        self._equity += net
        self._daily_pnl[closed_at.date()] = (
            self._daily_pnl.get(closed_at.date(), Decimal("0")) + net
        )
        self._observe_equity(self._equity)
        if self._horizon_timer and self._horizon_timer in self.clock.timer_names:
            self.clock.cancel_timer(self._horizon_timer)
        self._active = None
        self._active_entry_price = None
        self._active_quantity = None
        self._entry_order_id = None
        self._stop_order_id = None
        self._horizon_timer = None
        self._forced_exit_reason = None

    def on_stop(self) -> None:
        if self._pending is not None:
            self._reject("PENDING_SIGNAL_AT_REPLAY_END")
        if self._active is not None:
            self._reject("OPEN_POSITION_AT_REPLAY_END")
        self.unsubscribe_bars(self.config.bar_type)
        self.unsubscribe_quote_ticks(self.config.instrument_id)

    def _market_at_close(self, bar: Bar, at: datetime) -> MarketSnapshot:
        close = Decimal(str(bar.close))
        half_spread = self._spread_bps / Decimal("20000")
        return MarketSnapshot(
            cycle_id=stable_id("backtest_cycle", self.config.instrument_id, at),
            symbol=str(self.config.instrument_id).split(".", 1)[0],
            as_of=at,
            observed_at=at,
            bid=close * (1 - half_spread),
            ask=close * (1 + half_spread),
            last=close,
            bars=tuple(self._bars),
            source="nautilus-historical-bar",
        )

    def _account(self, *, cycle_id: str, as_of: datetime) -> AccountSnapshot:
        positions: tuple[Position, ...] = ()
        if self._active is not None and self._active_entry_price and self._active_quantity:
            positions = (
                Position(
                    symbol=self._active.symbol,
                    quantity=self._active_quantity,
                    average_price=self._active_entry_price,
                ),
            )
        drawdown = (
            max(Decimal("0"), (self._high_water - self._equity) / self._high_water)
            if self._high_water > 0
            else Decimal("0")
        )
        return AccountSnapshot(
            cycle_id=cycle_id,
            as_of=as_of,
            observed_at=as_of,
            quote_balance=self._equity,
            positions=positions,
            daily_pnl=self._daily_pnl.get(as_of.date(), Decimal("0")),
            drawdown_fraction=min(Decimal("1"), drawdown),
            equity=self._equity,
            equity_high_water=self._high_water,
            reconciled=True,
        )

    def _observe_equity(self, value: Decimal) -> None:
        self._high_water = max(self._high_water, value)
        if self._high_water > 0:
            drawdown = max(Decimal("0"), (self._high_water - value) / self._high_water)
            self._maximum_drawdown = max(self._maximum_drawdown, drawdown)

    def _modeled_cost(
        self,
        entry_price: Decimal,
        exit_price: Decimal,
        quantity: Decimal,
        *,
        entry_order_type: OrderType,
    ) -> Decimal:
        return estimate_round_trip_cost_amount(
            entry_price=entry_price,
            exit_price=exit_price,
            quantity=quantity,
            entry_order_type=entry_order_type,
            spread_bps=self._spread_bps,
            frequency=self._app.frequency,
            execution=self._app.execution,
        )

    def _reject(self, reason: str) -> None:
        self.rejection_counts[reason] = self.rejection_counts.get(reason, 0) + 1

    def _handle_order_failure(
        self, client_order_id, reason: str, detail: str
    ) -> None:
        self._reject(reason)
        role = (
            "ENTRY"
            if client_order_id == self._entry_order_id
            else "STOP"
            if client_order_id == self._stop_order_id
            else "EXIT_OR_UNKNOWN"
        )
        net_position = self.portfolio.net_position(self.config.instrument_id)
        self.order_failure_reasons.append(
            f"{role}:{detail}:active_quantity={self._active_quantity}:"
            f"net_position={net_position}"
        )
        if client_order_id == self._entry_order_id:
            position = self.cache.position_for_order(client_order_id)
            if position is not None and position.is_open:
                self._sync_filled_entry(position.id)
            elif self._active_entry_price is None:
                self._active = None
                self._entry_order_id = None
