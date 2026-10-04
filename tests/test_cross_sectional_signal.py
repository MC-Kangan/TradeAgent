"""Acceptance tests for the first cross-sectional signal evaluator."""

from datetime import UTC, date, datetime

import pytest

from trade_research.domain import AnalysisRequest, InstrumentId
from trade_research.domain.models import FactorReturnPoint, FactorReturnSeries
from trade_research.engine import ResearchEngine
from trade_research.reporting import render_markdown
from trade_research.skills.cross_sectional_signal import (
    CrossSectionalSignalParameters,
    CrossSectionUniverseMember,
    _Candidate,
    _evaluate_group,
    _momentum_score,
    evaluate_cross_sectional_momentum,
)
from trade_research.skills.factor_data import session_dates


def _histories(count: int = 5) -> tuple[FactorReturnSeries, ...]:
    sessions = session_dates("US", date(2022, 10, 1), date(2025, 2, 7))
    histories = []
    for index in range(count):
        instrument = InstrumentId(symbol=f"TEST{index}", market="US")
        histories.append(
            FactorReturnSeries(
                instrument=instrument,
                currency="USD",
                return_basis="adjusted_close_return",
                source="fixture",
                vendor_field="ADJ_CLOSE",
                retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
                points=tuple(
                    FactorReturnPoint(
                        start_date=start,
                        end_date=end,
                        value=0.0001 * (index + 1),
                    )
                    for start, end in zip(sessions[:-1], sessions[1:], strict=True)
                ),
            )
        )
    return tuple(histories)


def _parameters() -> CrossSectionalSignalParameters:
    return CrossSectionalSignalParameters(
        start_date=date(2023, 12, 1),
        end_date=date(2025, 1, 31),
        quantiles=5,
        minimum_assets=5,
    )


def test_momentum_ranks_predict_next_month_returns_without_same_close_entry() -> None:
    study = evaluate_cross_sectional_momentum(_histories(), _parameters())
    valid = [period for period in study.periods if period.rank_ic is not None]

    assert valid
    assert all(period.rank_ic == pytest.approx(1) for period in valid)
    assert all(period.top_minus_bottom > 0 for period in valid)
    assert study.average_rank_ic == pytest.approx(1)
    assert valid[0].top_turnover == pytest.approx(1)
    assert all(period.top_turnover == pytest.approx(0) for period in valid[1:])
    assert study.average_top_turnover == pytest.approx(1 / len(valid))
    assert [row.quantile for row in study.average_quantile_returns] == [1, 2, 3, 4, 5]
    assert [row.mean_forward_return for row in study.average_quantile_returns] == sorted(
        row.mean_forward_return for row in study.average_quantile_returns
    )
    assert study.entry_convention == "next_session_close"
    assert study.diagnostics == (
        "current_universe_only",
        "survivorship_bias_uncontrolled",
        "transaction_costs_excluded",
        "holdout_not_configured",
    )


def test_momentum_12_1_omits_the_most_recent_completed_month() -> None:
    formation = date(2024, 1, 31)
    monthly = {
        date(2023, month, 28 if month == 2 else 30 if month in {4, 6, 9, 11} else 31): (
            date(2023, month, 1),
            date(2023, month, 28 if month == 2 else 30 if month in {4, 6, 9, 11} else 31),
            0.01,
        )
        for month in range(1, 13)
    }
    baseline = _momentum_score(monthly, formation)
    monthly[date(2023, 12, 31)] = (
        date(2023, 12, 1),
        date(2023, 12, 31),
        -0.9,
    )

    assert baseline is not None
    assert _momentum_score(monthly, formation) == pytest.approx(baseline)


def test_tied_scores_that_leave_an_empty_quantile_are_rejected_cleanly() -> None:
    scores = (0, 1, 1, 1, 1, 1, 1, 2, 3, 4)
    candidates = tuple(
        _Candidate(
            instrument=InstrumentId(symbol=f"TIE{index}", market="US"),
            score=float(score),
            outcome=float(index) / 100,
            group="overall",
        )
        for index, score in enumerate(scores)
    )

    result, diagnostic = _evaluate_group(candidates, 5)

    assert result is None
    assert diagnostic == "insufficient_distinct_scores"


def test_portfolio_turnover_restarts_after_an_unevaluable_month() -> None:
    histories = tuple(
        history.model_copy(
            update={
                "points": tuple(
                    point for point in history.points if point.end_date != date(2024, 7, 15)
                )
            }
        )
        for history in _histories()
    )

    study = evaluate_cross_sectional_momentum(histories, _parameters())
    missing = next(row for row in study.periods if row.formation_date == date(2024, 6, 30))
    restarted = next(row for row in study.periods if row.formation_date == date(2024, 7, 31))

    assert missing.rank_ic is None
    assert restarted.rank_ic is not None
    assert restarted.top_turnover == pytest.approx(1)


def test_future_return_changes_do_not_change_earlier_formation_results() -> None:
    histories = _histories()
    first = evaluate_cross_sectional_momentum(histories, _parameters())
    changed = tuple(
        history.model_copy(
            update={
                "points": tuple(
                    point
                    if point.end_date <= date(2024, 9, 1)
                    else point.model_copy(update={"value": -point.value})
                    for point in history.points
                )
            }
        )
        for history in histories
    )
    second = evaluate_cross_sectional_momentum(changed, _parameters())

    assert [row for row in first.periods if row.formation_date < date(2024, 7, 1)] == [
        row for row in second.periods if row.formation_date < date(2024, 7, 1)
    ]


def test_negative_and_null_forward_relationships_are_explicit() -> None:
    histories = _histories()
    study_parameters = CrossSectionalSignalParameters(
        start_date=date(2023, 12, 1),
        end_date=date(2024, 2, 7),
        quantiles=5,
        minimum_assets=5,
    )
    negative = tuple(
        history.model_copy(
            update={
                "points": tuple(
                    point.model_copy(update={"value": -point.value})
                    if date(2024, 1, 2) < point.end_date <= date(2024, 2, 1)
                    else point
                    for point in history.points
                )
            }
        )
        for history in histories
    )
    negative_study = evaluate_cross_sectional_momentum(negative, study_parameters)
    assert negative_study.average_rank_ic == pytest.approx(-1)
    assert negative_study.average_top_minus_bottom < 0  # type: ignore[operator]

    null = tuple(
        history.model_copy(
            update={
                "points": tuple(
                    point.model_copy(update={"value": 0.0003})
                    if date(2024, 1, 2) < point.end_date <= date(2024, 2, 1)
                    else point
                    for point in history.points
                )
            }
        )
        for history in histories
    )
    null_study = evaluate_cross_sectional_momentum(null, study_parameters)
    december = next(
        row for row in null_study.periods if row.formation_date == date(2023, 12, 31)
    )
    assert december.diagnostics == ("constant_forward_returns",)


def test_mixed_currency_and_monthly_only_inputs_are_rejected() -> None:
    histories = _histories()
    with pytest.raises(ValueError, match="one study currency"):
        evaluate_cross_sectional_momentum(
            histories[:-1]
            + (histories[-1].model_copy(update={"currency": "EUR"}),),
            _parameters(),
        )
    with pytest.raises(ValueError, match="daily total-return"):
        evaluate_cross_sectional_momentum(
            (histories[0].model_copy(update={"frequency": "monthly"}),) + histories[1:],
            _parameters(),
        )


def test_point_in_time_membership_controls_formation_eligibility() -> None:
    histories = _histories()
    members = tuple(
        CrossSectionUniverseMember(
            instrument=history.instrument,
            sector="Energy",
            country="US",
            member_from=(date(2024, 6, 1) if index == 4 else date(2020, 1, 1)),
        )
        for index, history in enumerate(histories)
    )
    parameters = _parameters().model_copy(
        update={"universe_mode": "point_in_time", "members": members}
    )
    study = evaluate_cross_sectional_momentum(histories, parameters)

    december = next(
        row for row in study.periods if row.formation_date == date(2023, 12, 31)
    )
    assert december.eligible_asset_count == 4
    assert december.diagnostics == ("insufficient_assets",)
    assert "current_universe_only" not in study.diagnostics
    assert "terminal_returns_unverified" in study.diagnostics


def test_sector_grouping_equal_weights_valid_groups() -> None:
    histories = _histories(10)
    members = tuple(
        CrossSectionUniverseMember(
            instrument=history.instrument,
            sector="Energy" if index < 5 else "Health Care",
            country="US",
        )
        for index, history in enumerate(histories)
    )
    parameters = CrossSectionalSignalParameters(
        start_date=date(2023, 12, 1),
        end_date=date(2025, 1, 31),
        quantiles=5,
        minimum_assets=10,
        grouping="sector",
        minimum_group_assets=5,
        members=members,
    )
    study = evaluate_cross_sectional_momentum(histories, parameters)

    assert study.grouping == "sector"
    assert study.average_rank_ic == pytest.approx(1)
    valid = next(row for row in study.periods if row.rank_ic is not None)
    assert valid.eligible_asset_count == 10
    assert [row.asset_count for row in valid.quantile_returns] == [2, 2, 2, 2, 2]


def test_holdout_costs_and_block_bootstrap_are_reported_deterministically() -> None:
    parameters = _parameters().model_copy(
        update={
            "holdout_start_date": date(2024, 7, 1),
            "transaction_cost_bps": 50,
            "bootstrap_samples": 200,
        }
    )
    first = evaluate_cross_sectional_momentum(_histories(), parameters)
    second = evaluate_cross_sectional_momentum(_histories(), parameters)

    assert first.rank_ic_lower_95 == second.rank_ic_lower_95 == pytest.approx(1)
    assert first.rank_ic_upper_95 == second.rank_ic_upper_95 == pytest.approx(1)
    assert [segment.name for segment in first.segments] == ["development", "holdout"]
    assert first.segments[0].purged_period_count == 1
    assert first.average_net_top_return < first.average_top_return  # type: ignore[operator]
    assert first.annualized_net_top_return is not None
    assert first.net_top_max_drawdown == pytest.approx(0)
    assert "transaction_costs_illustrative" in first.diagnostics


@pytest.mark.asyncio
async def test_registered_skill_runs_through_the_common_report_contract() -> None:
    histories = _histories()
    engine = ResearchEngine.from_settings()
    assert "cross-sectional-signal" in engine.skills.names
    request = AnalysisRequest(
        instrument=InstrumentId(symbol="BASKET", market="PORTFOLIO"),
        scope="portfolio",
        portfolio_instruments=tuple(history.instrument for history in histories),
        analysts=("cross-sectional-signal",),
        factor_series=histories,
        skill_parameters={
            "cross-sectional-signal": {
                "start_date": "2023-12-01",
                "end_date": "2025-01-31",
                "quantiles": 5,
                "minimum_assets": 5,
            }
        },
    )

    report = await engine.analyze(request)
    result = report.results[0]

    assert result.status == "complete"
    assert result.presentation is not None
    assert result.presentation.schema_version == "cross-sectional-signal-v2"
    assert result.observations[0].metric == "cross_section_rank_ic"
    markdown = render_markdown(report)
    assert "Average equal-weighted forward return by score quantile" in markdown
    assert "current_universe_only" in markdown
