from datetime import UTC, datetime, timedelta

from trade_research.providers import PricePoint
from trade_research.skills.classic_strategy_signals import (
    CLASSIC_STRATEGIES,
    checkmate_signals,
    dolphin_signals,
    dual_thrust_signals,
    ema_momentum_signals,
    escalator_signals,
    fairy_four_price_signals,
    gap_signals,
    r_breaker_signals,
    turtle_signals,
)


def _bar(
    index: int,
    *,
    close: float,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    minutes: int = 1440,
) -> PricePoint:
    return PricePoint(
        observed_at=datetime(2024, 1, 1, 15, tzinfo=UTC)
        + timedelta(minutes=index * minutes),
        open=close if open_ is None else open_,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=1.0,
        source="yahoo",
        provenance={"reference": f"bar-{index}"},
    )


def test_catalog_covers_all_ten_and_flags_martingale_as_sizing_only() -> None:
    assert [definition.number for definition in CLASSIC_STRATEGIES] == list(range(1, 11))
    martingale = CLASSIC_STRATEGIES[3]
    assert martingale.slug == "martingale"
    assert martingale.signal_evaluable is False
    assert martingale.category == "position-sizing-overlay"


def test_turtle_uses_prior_channel_without_lookahead() -> None:
    prices = tuple(
        _bar(index, close=9, high=10, low=8) for index in range(20)
    ) + (_bar(20, close=11, high=11, low=9),)

    signals = turtle_signals(prices, entry_window=20, exit_window=10)

    assert [(signal.observed_at, signal.direction) for signal in signals] == [
        (prices[-1].observed_at, "long")
    ]


def test_gap_uses_current_open_and_prior_atr() -> None:
    prices = tuple(
        _bar(index, open_=100, high=101, low=99, close=100) for index in range(15)
    ) + (_bar(15, open_=97, high=100, low=96, close=99),)

    signals = gap_signals(prices, atr_period=14, gap_atr=0.5)

    assert signals[-1].observed_at == prices[-1].observed_at
    assert signals[-1].direction == "long"


def test_dolphin_requires_ema_trend_and_prior_two_bar_breakout() -> None:
    prices = tuple(
        _bar(index, close=10, high=10.5, low=9.5, minutes=30)
        for index in range(20)
    ) + (
        _bar(20, close=12, high=12, low=10, minutes=30),
    )

    signals = dolphin_signals(prices)

    assert signals[-1].observed_at == prices[-1].observed_at
    assert signals[-1].direction == "long"


def test_r_breaker_article_proxy_detects_intraday_breakout() -> None:
    prices = (
        _bar(0, open_=100, high=110, low=100, close=105, minutes=30),
        _bar(1, open_=105, high=105, low=90, close=100, minutes=30),
        _bar(48, open_=100, high=106, low=99, close=105, minutes=30),
        _bar(49, open_=105, high=109, low=104, close=108, minutes=30),
    )

    signals = r_breaker_signals(prices, breakout_fraction=0.35)

    assert signals[-1].observed_at == prices[-1].observed_at
    assert signals[-1].direction == "long"


def test_r_breaker_article_proxy_waits_for_recovery_after_deep_support() -> None:
    prices = (
        _bar(0, open_=100, high=110, low=90, close=100, minutes=30),
        _bar(48, open_=100, high=101, low=79, close=85, minutes=30),
        _bar(49, open_=85, high=95, low=84, close=95, minutes=30),
    )

    signals = r_breaker_signals(prices, breakout_fraction=0.35)

    assert [(signal.observed_at, signal.direction) for signal in signals] == [
        (prices[-1].observed_at, "long")
    ]


def test_dual_thrust_uses_two_prior_daily_ranges() -> None:
    prices = (
        _bar(0, open_=100, high=105, low=95, close=100, minutes=30),
        _bar(48, open_=100, high=110, low=90, close=100, minutes=30),
        _bar(96, open_=100, high=108, low=99, close=107, minutes=30),
        _bar(97, open_=107, high=111, low=106, close=111, minutes=30),
    )

    signals = dual_thrust_signals(prices, coefficient=0.5)

    assert signals[-1].observed_at == prices[-1].observed_at
    assert signals[-1].direction == "long"


def test_fairy_four_price_uses_previous_session_extremes() -> None:
    prices = (
        _bar(0, open_=100, high=105, low=95, close=100, minutes=30),
        _bar(48, open_=100, high=104, low=99, close=103, minutes=30),
        _bar(49, open_=103, high=106, low=102, close=106, minutes=30),
    )

    signals = fairy_four_price_signals(prices)

    assert signals[-1].observed_at == prices[-1].observed_at
    assert signals[-1].direction == "long"


def test_ema_momentum_emits_crossover_instructions() -> None:
    prices = tuple(
        _bar(index, close=value)
        for index, value in enumerate([10, 10, 10, 9, 8, 12, 14, 8, 6])
    )

    signals = ema_momentum_signals(prices, fast_window=2, slow_window=3)

    assert [signal.direction for signal in signals] == ["short", "long", "short"]


def test_escalator_enters_on_prior_n_bar_breakout() -> None:
    prices = tuple(
        _bar(index, close=10, high=11, low=9) for index in range(3)
    ) + (_bar(3, close=12, high=12, low=10),)

    signals = escalator_signals(prices, breakout_window=3, atr_period=2)

    assert [(signal.observed_at, signal.direction) for signal in signals] == [
        (prices[-1].observed_at, "long")
    ]


def test_checkmate_applies_breakout_and_atr_expansion_filter() -> None:
    calm = tuple(
        _bar(index, close=100, high=101, low=99) for index in range(40)
    )
    volatile = tuple(
        _bar(index, close=100, high=102, low=98) for index in range(40, 60)
    )
    prices = calm + volatile + (_bar(60, close=103, high=103, low=99),)

    signals = checkmate_signals(
        prices,
        breakout_window=40,
        fast_atr_period=20,
        slow_atr_period=60,
        expansion_ratio=1.1,
    )

    assert [(signal.observed_at, signal.direction) for signal in signals] == [
        (prices[-1].observed_at, "long")
    ]
