"""Local Streamlit showcase for Trade Research signal evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import streamlit as st

from trade_research.domain import InstrumentId
from trade_research.providers import PricePoint, YahooPriceProvider
from trade_research.skills.signal_evaluation import EvaluationSummary, SignalOutcome
from trade_research.strategy_showcase import (
    EvaluationSettings,
    StrategyEvaluationResult,
    available_strategies,
    build_chart_rows,
    evaluate_strategy,
)

st.set_page_config(page_title="Trade Research evaluator", page_icon="📈", layout="wide")


def _fetch_yahoo(symbol: str, timeframe: str) -> tuple[PricePoint, ...]:
    instrument = InstrumentId(
        symbol=symbol,
        market="INDEX" if symbol.startswith("^") else "US",
    )
    if timeframe == "intraday":
        provider = YahooPriceProvider(range_="60d", interval="30m")
    else:
        provider = YahooPriceProvider(range_="10y", interval="1d")
    return provider.price_history(instrument)


def _format_number(value: float | None, *, digits: int = 2) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def _format_percent(value: float | None) -> str:
    return "—" if value is None else f"{value:.1%}"


def _metric_rows(result: StrategyEvaluationResult) -> list[dict[str, Any]]:
    assert result.study is not None
    rows: list[dict[str, Any]] = []
    for view, summary in (
        ("Fixed horizon", result.study.fixed_horizon),
        ("Target / stop / time", result.study.triple_barrier),
    ):
        rows.append(
            {
                "View": view,
                "Events": summary.event_count,
                "Independent events": summary.non_overlapping_event_count,
                "Win rate": _format_percent(summary.win_rate),
                "Independent win rate": _format_percent(
                    summary.non_overlapping_win_rate
                ),
                "Reward / risk": _format_number(summary.reward_risk_ratio),
                "Expected R": _format_number(summary.expected_r),
                "Independent expected R": _format_number(
                    summary.non_overlapping_expected_r
                ),
                "95% expected-R interval": _interval(summary),
                "Profit factor": _format_number(summary.profit_factor),
            }
        )
    return rows


def _interval(summary: EvaluationSummary) -> str:
    lower = summary.non_overlapping_expected_r_lower_95
    upper = summary.non_overlapping_expected_r_upper_95
    if lower is None or upper is None:
        return "—"
    return f"{lower:.2f} to {upper:.2f}"


def _event_rows(events: Sequence[SignalOutcome]) -> list[dict[str, Any]]:
    return [
        {
            "Signal": event.signal_at,
            "Entry": event.entry_at,
            "Exit": event.exit_at,
            "Direction": event.direction,
            "Entry value": round(event.entry_value, 4),
            "Exit value": round(event.exit_value, 4),
            "Return": _format_percent(event.change),
            "R multiple": round(event.r_multiple, 3),
            "Exit reason": event.exit_reason.replace("_", " "),
            "Bars held": event.duration_bars,
            "Same-bar ambiguity": event.same_bar_ambiguous,
        }
        for event in reversed(events)
    ]


def _chart_spec() -> dict[str, Any]:
    shared = {
        "x": {"field": "timestamp", "type": "temporal", "title": None},
        "y": {
            "field": "value",
            "type": "quantitative",
            "title": "Price",
            "scale": {"zero": False},
        },
    }
    tooltip = [
        {"field": "timestamp", "type": "temporal", "title": "Time"},
        {"field": "value", "type": "quantitative", "title": "Value", "format": ".4f"},
        {"field": "label", "type": "nominal", "title": "Action"},
        {"field": "details", "type": "nominal", "title": "Details"},
    ]
    return {
        "height": 520,
        "layer": [
            {
                "transform": [{"filter": "datum.kind === 'price'"}],
                "mark": {"type": "line", "color": "#62748e", "strokeWidth": 1.5},
                "encoding": shared,
            },
            _action_layer("buy", "triangle-up", "#16a34a", shared, tooltip),
            _action_layer("sell", "triangle-down", "#dc2626", shared, tooltip),
            _action_layer("exit", "diamond", "#f59e0b", shared, tooltip),
            _label_layer("buy", -14, "#15803d", shared),
            _label_layer("sell", 16, "#b91c1c", shared),
            _label_layer("exit", -14, "#b45309", shared),
        ],
    }


def _action_layer(
    kind: str,
    shape: str,
    color: str,
    shared: dict[str, Any],
    tooltip: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "transform": [{"filter": f"datum.kind === '{kind}'"}],
        "mark": {
            "type": "point",
            "shape": shape,
            "filled": True,
            "color": color,
            "size": 110,
        },
        "encoding": {**shared, "tooltip": tooltip},
    }


def _label_layer(
    kind: str,
    dy: int,
    color: str,
    shared: dict[str, Any],
) -> dict[str, Any]:
    return {
        "transform": [{"filter": f"datum.kind === '{kind}'"}],
        "mark": {"type": "text", "dy": dy, "fontSize": 9, "color": color},
        "encoding": {
            **shared,
            "text": {"field": "label", "type": "nominal"},
        },
    }


def _show_result(
    prices: tuple[PricePoint, ...],
    result: StrategyEvaluationResult,
    *,
    ticker: str,
    chart_bars: int | None,
) -> None:
    st.caption(
        f"{ticker} · Yahoo · {result.strategy.timeframe} · {len(prices):,} bars · "
        f"{prices[0].observed_at.date()} to {prices[-1].observed_at.date()}"
    )
    if result.study is None:
        st.warning(
            f"The strategy produced {len(result.raw_instructions)} signal(s), but none had "
            "enough eligible forward data to evaluate. Try another ticker or strategy."
        )
        return

    summary = result.study.triple_barrier
    columns = st.columns(6)
    columns[0].metric("Win rate", _format_percent(summary.win_rate))
    columns[1].metric("Reward / risk", _format_number(summary.reward_risk_ratio))
    columns[2].metric("Expected R", _format_number(summary.expected_r))
    columns[3].metric(
        "Independent expected R", _format_number(summary.non_overlapping_expected_r)
    )
    columns[4].metric("Profit factor", _format_number(summary.profit_factor))
    columns[5].metric("Independent N", str(summary.non_overlapping_event_count))

    st.subheader("Price and evaluated actions")
    st.caption(
        "BUY and SELL are actual next-bar entries from long and short instructions. "
        "EXIT is the evaluator's target, stop, or time-limit exit; it is not a broker order."
    )
    chart_rows = build_chart_rows(prices, result, max_bars=chart_bars)
    st.vega_lite_chart(chart_rows, _chart_spec(), width="stretch")

    st.subheader("Evaluation metrics")
    st.dataframe(_metric_rows(result), width="stretch", hide_index=True)
    st.caption(
        "Independent metrics greedily remove overlapping outcomes. The 95% interval uses "
        "the evaluator's moving-block bootstrap. This local showcase does not model costs."
    )

    if result.study.periods:
        holdout = next(
            (period for period in result.study.periods if period.name == "holdout"),
            None,
        )
        if holdout is not None:
            with st.expander("Holdout evidence"):
                st.write(
                    f"Chronological final 20%: {holdout.start_at.date()} to "
                    f"{holdout.end_at.date()}"
                )
                st.dataframe(
                    [
                        {
                            "Events": holdout.triple_barrier.event_count,
                            "Independent events": (
                                holdout.triple_barrier.non_overlapping_event_count
                            ),
                            "Win rate": _format_percent(holdout.triple_barrier.win_rate),
                            "Expected R": _format_number(
                                holdout.triple_barrier.expected_r
                            ),
                            "Independent expected R": _format_number(
                                holdout.triple_barrier.non_overlapping_expected_r
                            ),
                        }
                    ],
                    width="stretch",
                    hide_index=True,
                )

    with st.expander("Evaluated event ledger"):
        st.dataframe(
            _event_rows(summary.events),
            width="stretch",
            hide_index=True,
        )


st.title("Strategy signal evaluator")
st.write(
    "Fetch Yahoo prices, generate causal Trade Research signals, and inspect the same "
    "fixed-horizon and target/stop/time-limit evaluator used by the framework."
)

strategies = available_strategies()
with st.sidebar:
    st.header("Study setup")
    ticker_input = st.text_input("Yahoo ticker", value="^GSPC").strip().upper()
    selected_slug = st.selectbox(
        "Strategy",
        options=[strategy.slug for strategy in strategies],
        format_func=lambda slug: next(
            strategy.name for strategy in strategies if strategy.slug == slug
        ),
    )
    selected_strategy = next(
        strategy for strategy in strategies if strategy.slug == selected_slug
    )
    st.markdown(f"**Entry:** {selected_strategy.entry_criteria}")
    st.markdown(f"**Exit:** {selected_strategy.exit_criteria}")
    st.caption(
        "Daily strategies request Yahoo's 10-year daily window. Intraday strategies "
        "request its 60-day, 30-minute window."
    )

    if selected_strategy.timeframe == "intraday":
        default_horizon, default_target, default_stop, default_holding = 2, 0.5, 0.35, 4
    else:
        default_horizon, default_target, default_stop, default_holding = 10, 3.0, 2.0, 20

    with st.expander("Outcome rules"):
        fixed_horizon = int(
            st.number_input(
                "Fixed horizon (bars)",
                1,
                250,
                default_horizon,
                key=f"fixed-horizon-{selected_strategy.timeframe}",
            )
        )
        profit_target = float(
            st.number_input(
                "Profit target (%)",
                0.01,
                99.0,
                default_target,
                step=0.1,
                key=f"profit-target-{selected_strategy.timeframe}",
            )
        )
        stop_loss = float(
            st.number_input(
                "Stop loss (%)",
                0.01,
                99.0,
                default_stop,
                step=0.1,
                key=f"stop-loss-{selected_strategy.timeframe}",
            )
        )
        max_holding = int(
            st.number_input(
                "Maximum holding (bars)",
                1,
                500,
                default_holding,
                key=f"max-holding-{selected_strategy.timeframe}",
            )
        )
    chart_window = st.selectbox(
        "Chart window",
        options=(126, 252, 500, None),
        format_func=lambda bars: "All bars" if bars is None else f"Last {bars} bars",
        index=1,
    )
    run = st.button("Fetch and evaluate", type="primary", width="stretch")

if run:
    if not ticker_input:
        st.error("Enter a Yahoo ticker.")
    else:
        try:
            with st.spinner("Fetching Yahoo data and evaluating signals…"):
                fetched_prices = _fetch_yahoo(
                    ticker_input,
                    selected_strategy.timeframe,
                )
                evaluated = evaluate_strategy(
                    fetched_prices,
                    selected_slug,
                    EvaluationSettings(
                        fixed_horizon_bars=fixed_horizon,
                        profit_target=profit_target / 100,
                        stop_loss=stop_loss / 100,
                        max_holding_bars=max_holding,
                    ),
                )
            st.session_state["showcase_result"] = (
                fetched_prices,
                evaluated,
                ticker_input,
            )
        except Exception as error:  # Streamlit must turn bounded provider errors into UI text.
            st.error(f"Unable to evaluate {ticker_input}: {error}")

stored = st.session_state.get("showcase_result")
if stored is None:
    st.info("Choose a ticker and strategy, then select **Fetch and evaluate**.")
else:
    stored_prices, stored_result, stored_ticker = stored
    _show_result(
        stored_prices,
        stored_result,
        ticker=stored_ticker,
        chart_bars=chart_window,
    )

st.divider()
st.caption(
    "Research only. Yahoo data may be delayed or incomplete. Metrics are historical "
    "descriptions, not investment advice or evidence of future performance."
)
