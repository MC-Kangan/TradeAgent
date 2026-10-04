"""Monthly cross-sectional signal evaluation on normalized total returns."""

from __future__ import annotations

import calendar
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from statistics import fmean, median, stdev
from typing import Literal, Self

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator
from scipy.stats import rankdata, spearmanr

from trade_research.domain import (
    AnalystResult,
    InstrumentId,
    LimitationKind,
    Observation,
    ReportStatus,
)
from trade_research.domain.models import (
    CrossSectionalSignalPresentation,
    CrossSectionAssetCoverage,
    CrossSectionPeriod,
    CrossSectionQuantileReturn,
    CrossSectionQuantileSummary,
    CrossSectionSegmentSummary,
    FactorReturnSeries,
)
from trade_research.providers import CapabilityName, ProviderRegistry
from trade_research.skills.factor_data import month_end, prepare_returns

MAX_CROSS_SECTION_ASSETS = 64


class CrossSectionUniverseMember(BaseModel):
    """Dated classification metadata for one research-universe constituent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    instrument: InstrumentId
    sector: str | None = Field(default=None, min_length=1, max_length=64)
    country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    member_from: date | None = None
    member_to: date | None = None

    @model_validator(mode="after")
    def validate_membership(self) -> Self:
        if self.member_from and self.member_to and self.member_from > self.member_to:
            raise ValueError("member_from cannot follow member_to")
        return self


class CrossSectionalSignalParameters(BaseModel):
    """Bounded cross-sectional study settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal: str = Field(default="momentum_12_1", pattern=r"^momentum_12_1$")
    start_date: date | None = None
    end_date: date | None = None
    quantiles: int = Field(default=5, ge=3, le=10)
    minimum_assets: int = Field(default=5, ge=3, le=MAX_CROSS_SECTION_ASSETS)
    universe_mode: Literal["current_watchlist", "point_in_time"] = "current_watchlist"
    members: tuple[CrossSectionUniverseMember, ...] = Field(
        default=(), max_length=MAX_CROSS_SECTION_ASSETS
    )
    grouping: Literal["overall", "sector", "country", "sector_country"] = "overall"
    minimum_group_assets: int = Field(default=5, ge=3, le=MAX_CROSS_SECTION_ASSETS)
    transaction_cost_bps: float = Field(default=0, ge=0, le=500)
    holdout_start_date: date | None = None
    bootstrap_samples: int = Field(default=500, ge=100, le=5000)
    bootstrap_seed: int = Field(default=7, ge=0, le=2**32 - 1)
    bootstrap_block_length: int = Field(default=3, ge=1, le=24)

    @model_validator(mode="after")
    def validate_study(self) -> CrossSectionalSignalParameters:
        if self.minimum_assets < self.quantiles:
            raise ValueError("minimum_assets must be at least quantiles")
        if self.minimum_group_assets < self.quantiles:
            raise ValueError("minimum_group_assets must be at least quantiles")
        if self.start_date and self.end_date and self.start_date >= self.end_date:
            raise ValueError("start_date must precede end_date")
        if self.holdout_start_date and self.start_date:
            if self.holdout_start_date <= self.start_date:
                raise ValueError("holdout_start_date must follow start_date")
        if self.holdout_start_date and self.end_date:
            if self.holdout_start_date > self.end_date:
                raise ValueError("holdout_start_date cannot follow end_date")
        identities = [member.instrument for member in self.members]
        if len(identities) != len(set(identities)):
            raise ValueError("universe members must be unique")
        if self.universe_mode == "point_in_time" and any(
            member.member_from is None for member in self.members
        ):
            raise ValueError("point-in-time members require member_from")
        return self

    def resolve_dates(self, as_of: date) -> CrossSectionalSignalParameters:
        end = self.end_date or as_of
        start = self.start_date or _shift_month(end, -60)
        if end > as_of:
            raise ValueError("end_date cannot be in the future")
        resolved = self.model_copy(update={"start_date": start, "end_date": end})
        return CrossSectionalSignalParameters.model_validate(resolved.model_dump())

    def validate_instruments(
        self, instruments: tuple[InstrumentId, ...]
    ) -> CrossSectionalSignalParameters:
        if self.members and {member.instrument for member in self.members} != set(instruments):
            raise ValueError("universe members must exactly match portfolio instruments")
        if self.universe_mode == "point_in_time" and not self.members:
            raise ValueError("point-in-time mode requires dated universe members")
        if self.grouping != "overall":
            if not self.members:
                raise ValueError("grouped analysis requires universe member metadata")
            if self.grouping in {"sector", "sector_country"} and any(
                member.sector is None for member in self.members
            ):
                raise ValueError("sector grouping requires every member's sector")
            if self.grouping in {"country", "sector_country"} and any(
                member.country is None for member in self.members
            ):
                raise ValueError("country grouping requires every member's country")
        return self


@dataclass(frozen=True, slots=True)
class _PreparedAsset:
    series: FactorReturnSeries
    monthly: dict[date, tuple[date, date, float]]
    daily: dict[date, tuple[date, date, float]]
    expected_daily: tuple[date, ...]


@dataclass(frozen=True, slots=True)
class _Candidate:
    instrument: InstrumentId
    score: float
    outcome: float
    group: str


@dataclass(frozen=True, slots=True)
class CrossSectionalSignalSkill:
    """Evaluate whether trailing momentum ranks predict next-month returns."""

    instruments: tuple[InstrumentId, ...] = ()
    parameters: CrossSectionalSignalParameters = field(
        default_factory=CrossSectionalSignalParameters
    )
    _name: str = field(default="cross-sectional-signal", init=False, repr=False)

    @property
    def name(self) -> str:
        return self._name

    @property
    def required_capabilities(self) -> tuple[CapabilityName, ...]:
        return (CapabilityName.FACTOR_RETURNS,)

    def analyze(self, instrument: InstrumentId, providers: ProviderRegistry) -> AnalystResult:
        assert self.parameters.start_date is not None
        assert self.parameters.end_date is not None
        presentation = evaluate_cross_sectional_momentum(
            tuple(
                providers.factor_returns(
                    item,
                    self.parameters.start_date - timedelta(days=400),
                    self.parameters.end_date,
                )
                for item in self.instruments
            ),
            self.parameters,
        )
        valid_periods = tuple(
            period for period in presentation.periods if period.rank_ic is not None
        )
        observed_at = datetime.combine(
            presentation.requested_end, datetime.min.time(), tzinfo=UTC
        )
        observations: list[Observation] = []
        for metric, value in (
            ("cross_section_rank_ic", presentation.average_rank_ic),
            ("cross_section_top_minus_bottom", presentation.average_top_minus_bottom),
            ("cross_section_top_turnover", presentation.average_top_turnover),
        ):
            if value is not None:
                observations.append(
                    Observation(
                        instrument=instrument,
                        metric=metric,
                        value=value,
                        source="derived",
                        observed_at=observed_at,
                        provenance={
                            "provider_kind": "derived",
                            "algorithm": "cross_sectional_signal_evaluation",
                            "window": "monthly_momentum_12_1",
                            "series_ref": presentation.configuration_ref,
                            "point_count": len(valid_periods),
                        },
                    )
                )
        complete = bool(valid_periods)
        return AnalystResult(
            analyst=self.name,
            instrument=instrument,
            summary=(
                f"evaluated momentum across {len(self.instruments)} assets and "
                f"{len(valid_periods)} valid formation dates"
            ),
            status=ReportStatus.COMPLETE if complete else ReportStatus.PARTIAL,
            limitations=(
                (
                    LimitationKind.BOUNDED_INPUT,
                    LimitationKind.INSUFFICIENT_HISTORY,
                )
                if not complete
                else (LimitationKind.BOUNDED_INPUT,)
            ),
            observations=tuple(observations),
            presentation=presentation,
        )


def evaluate_cross_sectional_momentum(
    histories: tuple[FactorReturnSeries, ...],
    parameters: CrossSectionalSignalParameters,
) -> CrossSectionalSignalPresentation:
    """Evaluate 12-1 momentum with next-session entry and monthly rebalancing."""

    if parameters.start_date is None or parameters.end_date is None:
        raise ValueError("study dates must be resolved before evaluation")
    if not 3 <= len(histories) <= MAX_CROSS_SECTION_ASSETS:
        raise ValueError("cross-sectional studies require between 3 and 64 assets")
    identities = tuple(item.instrument for item in histories)
    if len(set(identities)) != len(identities):
        raise ValueError("cross-sectional instruments must be unique")
    parameters.validate_instruments(identities)
    if any(item.frequency != "daily" for item in histories):
        raise ValueError("next-session evaluation requires daily total-return histories")
    currencies = {item.currency for item in histories}
    bases = {item.return_basis for item in histories}
    if len(currencies) != 1:
        raise ValueError("cross-sectional histories must use one study currency")
    if len(bases) != 1:
        raise ValueError("cross-sectional histories must use one return basis")

    fetch_start = parameters.start_date - timedelta(days=400)
    prepared: dict[InstrumentId, _PreparedAsset] = {}
    for history in histories:
        monthly = prepare_returns(
            history,
            "monthly",
            fetch_start,
            parameters.end_date,
            calendar_market=history.calendar or history.instrument.market,
        )
        daily = prepare_returns(
            history,
            "daily",
            fetch_start,
            parameters.end_date,
            calendar_market=history.calendar or history.instrument.market,
        )
        prepared[history.instrument] = _PreparedAsset(
            series=history,
            monthly=dict(monthly.rows),
            daily=dict(daily.rows),
            expected_daily=daily.expected,
        )

    members = _member_map(parameters, identities)
    formation_dates = _formation_dates(parameters.start_date, parameters.end_date)[-121:]
    period_rows: list[CrossSectionPeriod] = []
    evaluated_counts = {instrument: 0 for instrument in identities}
    previous_weights: dict[InstrumentId, float] | None = None
    for formation_date in formation_dates:
        candidates: list[_Candidate] = []
        for identity, asset in prepared.items():
            member = members[identity]
            if not _is_member_active(member, formation_date, parameters.universe_mode):
                continue
            score = _momentum_score(asset.monthly, formation_date)
            outcome = _forward_return(asset, formation_date)
            if score is not None and outcome is not None:
                candidates.append(
                    _Candidate(
                        instrument=identity,
                        score=score,
                        outcome=outcome,
                        group=_group_key(member, parameters.grouping),
                    )
                )
        period, weights, evaluated = _evaluate_period(
            formation_date, candidates, parameters, previous_weights
        )
        period_rows.append(period)
        previous_weights = weights
        if weights is not None:
            for identity in evaluated:
                evaluated_counts[identity] += 1

    rank_ics = [row.rank_ic for row in period_rows if row.rank_ic is not None]
    spreads = [
        row.top_minus_bottom for row in period_rows if row.top_minus_bottom is not None
    ]
    turnovers = [row.top_turnover for row in period_rows if row.top_turnover is not None]
    top_returns = [row.top_return for row in period_rows if row.top_return is not None]
    net_top_returns = [
        row.net_top_return for row in period_rows if row.net_top_return is not None
    ]
    universe_returns = [
        row.universe_return for row in period_rows if row.universe_return is not None
    ]
    excess_returns = [
        row.top_excess_return for row in period_rows if row.top_excess_return is not None
    ]
    average_quantiles = tuple(
        CrossSectionQuantileSummary(
            quantile=quantile,
            period_count=len(values),
            mean_forward_return=fmean(values),
        )
        for quantile in range(1, parameters.quantiles + 1)
        if (
            values := [
                item.mean_forward_return
                for row in period_rows
                for item in row.quantile_returns
                if item.quantile == quantile
            ]
        )
    )
    configuration_ref = _configuration_ref(parameters, identities)
    diagnostics: list[str] = []
    if parameters.universe_mode == "current_watchlist":
        diagnostics.extend(("current_universe_only", "survivorship_bias_uncontrolled"))
    else:
        diagnostics.append("terminal_returns_unverified")
    diagnostics.append(
        "transaction_costs_excluded"
        if parameters.transaction_cost_bps == 0
        else "transaction_costs_illustrative"
    )
    if parameters.holdout_start_date is None:
        diagnostics.append("holdout_not_configured")
    if not rank_ics:
        diagnostics.append("insufficient_valid_periods")
    ic_std = stdev(rank_ics) if len(rank_ics) > 1 else None
    lower, upper = _block_bootstrap_interval(
        rank_ics,
        samples=parameters.bootstrap_samples,
        seed=parameters.bootstrap_seed,
        block_length=parameters.bootstrap_block_length,
    )
    valid_portfolio_periods = [
        row for row in period_rows if row.net_top_return is not None
    ]
    portfolio_is_contiguous = _periods_are_contiguous(valid_portfolio_periods)
    if valid_portfolio_periods and not portfolio_is_contiguous:
        diagnostics.append("discontinuous_evaluation_periods")
    segment_rows = _segment_summaries(period_rows, parameters.holdout_start_date)
    return CrossSectionalSignalPresentation(
        quantile_count=parameters.quantiles,
        minimum_assets=parameters.minimum_assets,
        universe_mode=parameters.universe_mode,
        grouping=parameters.grouping,
        currency=next(iter(currencies)),
        return_basis=next(iter(bases)),
        requested_start=parameters.start_date,
        requested_end=parameters.end_date,
        average_rank_ic=fmean(rank_ics) if rank_ics else None,
        median_rank_ic=median(rank_ics) if rank_ics else None,
        rank_ic_standard_deviation=ic_std,
        rank_ic_information_ratio=(
            fmean(rank_ics) / ic_std if ic_std is not None and ic_std > 0 else None
        ),
        positive_rank_ic_fraction=(
            sum(value > 0 for value in rank_ics) / len(rank_ics) if rank_ics else None
        ),
        rank_ic_lower_95=lower,
        rank_ic_upper_95=upper,
        average_top_minus_bottom=fmean(spreads) if spreads else None,
        average_top_turnover=fmean(turnovers) if turnovers else None,
        average_top_return=fmean(top_returns) if top_returns else None,
        average_net_top_return=fmean(net_top_returns) if net_top_returns else None,
        average_universe_return=fmean(universe_returns) if universe_returns else None,
        average_top_excess_return=fmean(excess_returns) if excess_returns else None,
        annualized_net_top_return=(
            _annualized_return(net_top_returns) if portfolio_is_contiguous else None
        ),
        annualized_universe_return=(
            _annualized_return(universe_returns) if portfolio_is_contiguous else None
        ),
        annualized_net_top_volatility=(
            stdev(net_top_returns) * math.sqrt(12)
            if portfolio_is_contiguous and len(net_top_returns) > 1
            else None
        ),
        net_top_max_drawdown=(
            _max_drawdown(net_top_returns) if portfolio_is_contiguous else None
        ),
        transaction_cost_bps=parameters.transaction_cost_bps,
        average_quantile_returns=average_quantiles,
        periods=tuple(period_rows[-121:]),
        segments=segment_rows,
        coverage=tuple(
            CrossSectionAssetCoverage(
                instrument=identity,
                sector=members[identity].sector,
                country=members[identity].country,
                member_from=members[identity].member_from,
                member_to=members[identity].member_to,
                monthly_period_count=len(prepared[identity].monthly),
                evaluated_period_count=evaluated_counts[identity],
            )
            for identity in identities
        ),
        diagnostics=tuple(diagnostics),
        configuration_ref=configuration_ref,
    )


@dataclass(frozen=True, slots=True)
class _GroupResult:
    candidates: tuple[_Candidate, ...]
    rank_ic: float
    quantiles: tuple[CrossSectionQuantileReturn, ...]
    top: frozenset[InstrumentId]
    top_return: float
    bottom_return: float
    universe_return: float


def _evaluate_period(
    formation_date: date,
    candidates: list[_Candidate],
    parameters: CrossSectionalSignalParameters,
    previous_weights: dict[InstrumentId, float] | None,
) -> tuple[
    CrossSectionPeriod,
    dict[InstrumentId, float] | None,
    frozenset[InstrumentId],
]:
    if len(candidates) < parameters.minimum_assets:
        return (
            CrossSectionPeriod(
                formation_date=formation_date,
                eligible_asset_count=len(candidates),
                diagnostics=("insufficient_assets",),
            ),
            None,
            frozenset(),
        )

    diagnostic: Literal[
        "insufficient_group_assets",
        "insufficient_distinct_scores",
        "constant_forward_returns",
    ] | None = None
    group_results: tuple[_GroupResult, ...]
    if parameters.grouping == "overall":
        result, diagnostic = _evaluate_group(tuple(candidates), parameters.quantiles)
        group_results = () if result is None else (result,)
    else:
        grouped: dict[str, list[_Candidate]] = {}
        for candidate in candidates:
            grouped.setdefault(candidate.group, []).append(candidate)
        eligible_groups = tuple(
            tuple(rows)
            for rows in grouped.values()
            if len(rows) >= parameters.minimum_group_assets
        )
        if len(eligible_groups) != len(grouped):
            diagnostic = "insufficient_group_assets"
        evaluated_groups = tuple(
            _evaluate_group(group, parameters.quantiles) for group in eligible_groups
        )
        group_results = tuple(
            result for result, _ in evaluated_groups if result is not None
        )
        if len(group_results) != len(evaluated_groups) and diagnostic is None:
            diagnostic = next(
                (reason for _, reason in evaluated_groups if reason is not None),
                "insufficient_group_assets",
            )
        if not group_results and evaluated_groups:
            diagnostic = next(
                (reason for _, reason in evaluated_groups if reason is not None),
                "insufficient_group_assets",
            )

    eligible_count = sum(len(result.candidates) for result in group_results)
    if eligible_count < parameters.minimum_assets or not group_results:
        return (
            CrossSectionPeriod(
                formation_date=formation_date,
                eligible_asset_count=eligible_count,
                diagnostics=(diagnostic or "insufficient_group_assets",),
            ),
            None,
            frozenset(),
        )

    quantile_rows = tuple(
        CrossSectionQuantileReturn(
            quantile=quantile,
            asset_count=sum(
                item.asset_count
                for result in group_results
                for item in result.quantiles
                if item.quantile == quantile
            ),
            mean_forward_return=fmean(
                item.mean_forward_return
                for result in group_results
                for item in result.quantiles
                if item.quantile == quantile
            ),
        )
        for quantile in range(1, parameters.quantiles + 1)
    )
    weights = {
        identity: 1 / len(group_results) / len(result.top)
        for result in group_results
        for identity in result.top
    }
    top_return = fmean(result.top_return for result in group_results)
    bottom_return = fmean(result.bottom_return for result in group_results)
    universe_return = fmean(result.universe_return for result in group_results)
    turnover = (
        1.0
        if previous_weights is None
        else 0.5
        * sum(
            abs(weights.get(identity, 0) - previous_weights.get(identity, 0))
            for identity in set(weights) | set(previous_weights)
        )
    )
    net_top_return = max(
        -1.0,
        top_return - turnover * parameters.transaction_cost_bps / 10_000,
    )
    diagnostics = () if diagnostic is None else (diagnostic,)
    evaluated_assets = frozenset(
        candidate.instrument
        for result in group_results
        for candidate in result.candidates
    )
    return (
        CrossSectionPeriod(
            formation_date=formation_date,
            eligible_asset_count=eligible_count,
            rank_ic=fmean(result.rank_ic for result in group_results),
            quantile_returns=quantile_rows,
            top_minus_bottom=top_return - bottom_return,
            top_turnover=turnover,
            top_return=top_return,
            net_top_return=net_top_return,
            universe_return=universe_return,
            top_excess_return=top_return - universe_return,
            diagnostics=diagnostics,
        ),
        weights,
        evaluated_assets,
    )


def _evaluate_group(
    candidates: tuple[_Candidate, ...], quantile_count: int
) -> tuple[
    _GroupResult | None,
    Literal["insufficient_distinct_scores", "constant_forward_returns"] | None,
]:
    scores = np.asarray([candidate.score for candidate in candidates], dtype=float)
    outcomes = np.asarray([candidate.outcome for candidate in candidates], dtype=float)
    if len(np.unique(scores)) < quantile_count:
        return None, "insufficient_distinct_scores"
    if len(np.unique(outcomes)) < 2:
        return None, "constant_forward_returns"
    ranks = rankdata(scores, method="average")
    buckets = np.minimum(
        quantile_count,
        np.ceil(ranks * quantile_count / len(candidates)).astype(int),
    )
    if any(not np.any(buckets == quantile) for quantile in range(1, quantile_count + 1)):
        return None, "insufficient_distinct_scores"
    quantiles = tuple(
        CrossSectionQuantileReturn(
            quantile=quantile,
            asset_count=int(np.sum(buckets == quantile)),
            mean_forward_return=float(np.mean(outcomes[buckets == quantile])),
        )
        for quantile in range(1, quantile_count + 1)
    )
    top = frozenset(
        candidate.instrument
        for candidate, bucket in zip(candidates, buckets, strict=True)
        if bucket == quantile_count
    )
    return (
        _GroupResult(
            candidates=candidates,
            rank_ic=float(spearmanr(scores, outcomes).statistic),
            quantiles=quantiles,
            top=top,
            top_return=float(np.mean(outcomes[buckets == quantile_count])),
            bottom_return=float(np.mean(outcomes[buckets == 1])),
            universe_return=float(np.mean(outcomes)),
        ),
        None,
    )


def _member_map(
    parameters: CrossSectionalSignalParameters,
    identities: tuple[InstrumentId, ...],
) -> dict[InstrumentId, CrossSectionUniverseMember]:
    supplied = {member.instrument: member for member in parameters.members}
    return {
        identity: supplied.get(identity, CrossSectionUniverseMember(instrument=identity))
        for identity in identities
    }


def _is_member_active(
    member: CrossSectionUniverseMember,
    formation_date: date,
    universe_mode: Literal["current_watchlist", "point_in_time"],
) -> bool:
    if universe_mode == "current_watchlist":
        return True
    assert member.member_from is not None
    return member.member_from <= formation_date and (
        member.member_to is None or formation_date <= member.member_to
    )


def _group_key(
    member: CrossSectionUniverseMember,
    grouping: Literal["overall", "sector", "country", "sector_country"],
) -> str:
    if grouping == "sector":
        return str(member.sector)
    if grouping == "country":
        return str(member.country)
    if grouping == "sector_country":
        return f"{member.sector}|{member.country}"
    return "overall"


def _block_bootstrap_interval(
    values: list[float], *, samples: int, seed: int, block_length: int
) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    array = np.asarray(values, dtype=float)
    block = min(block_length, len(array))
    rng = np.random.default_rng(seed)
    means = np.empty(samples, dtype=float)
    for sample in range(samples):
        drawn: list[float] = []
        while len(drawn) < len(array):
            start = int(rng.integers(0, len(array)))
            drawn.extend(array[(start + offset) % len(array)] for offset in range(block))
        means[sample] = float(np.mean(drawn[: len(array)]))
    lower, upper = np.quantile(means, (0.025, 0.975))
    return max(-1.0, float(lower)), min(1.0, float(upper))


def _annualized_return(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = math.prod(1 + value for value in values)
    if wealth < 0:
        return None
    return wealth ** (12 / len(values)) - 1


def _max_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    wealth = 1.0
    peak = 1.0
    drawdown = 0.0
    for value in values:
        wealth *= 1 + value
        peak = max(peak, wealth)
        drawdown = min(drawdown, wealth / peak - 1)
    return drawdown


def _segment_summaries(
    periods: list[CrossSectionPeriod], holdout_start: date | None
) -> tuple[CrossSectionSegmentSummary, ...]:
    if holdout_start is None:
        return ()
    development_candidates = [
        row for row in periods if row.formation_date < holdout_start
    ]
    development = [
        row
        for row in development_candidates
        if _shift_month(row.formation_date, 1) < holdout_start
    ]
    purged = len(development_candidates) - len(development)
    groups = (
        ("development", development, purged),
        ("holdout", [row for row in periods if row.formation_date >= holdout_start], 0),
    )
    summaries: list[CrossSectionSegmentSummary] = []
    for name, rows, purged_count in groups:
        if not rows:
            continue
        valid = [row for row in rows if row.rank_ic is not None]
        ics = [float(row.rank_ic) for row in valid if row.rank_ic is not None]
        net = [float(row.net_top_return) for row in valid if row.net_top_return is not None]
        universe = [
            float(row.universe_return) for row in valid if row.universe_return is not None
        ]
        excess = [
            float(row.top_excess_return)
            for row in valid
            if row.top_excess_return is not None
        ]
        ic_std = stdev(ics) if len(ics) > 1 else None
        summaries.append(
            CrossSectionSegmentSummary(
                name=name,
                start_date=rows[0].formation_date,
                end_date=rows[-1].formation_date,
                period_count=len(valid),
                purged_period_count=purged_count,
                average_rank_ic=fmean(ics) if ics else None,
                median_rank_ic=median(ics) if ics else None,
                rank_ic_information_ratio=(
                    fmean(ics) / ic_std if ic_std is not None and ic_std > 0 else None
                ),
                positive_rank_ic_fraction=(
                    sum(value > 0 for value in ics) / len(ics) if ics else None
                ),
                average_top_excess_return=fmean(excess) if excess else None,
                annualized_net_top_return=(
                    _annualized_return(net) if _periods_are_contiguous(valid) else None
                ),
                annualized_universe_return=(
                    _annualized_return(universe) if _periods_are_contiguous(valid) else None
                ),
            )
        )
    return tuple(summaries)


def _periods_are_contiguous(periods: list[CrossSectionPeriod]) -> bool:
    return all(
        _shift_month(first.formation_date, 1) == second.formation_date
        for first, second in zip(periods[:-1], periods[1:], strict=True)
    )


def _momentum_score(
    monthly: dict[date, tuple[date, date, float]], formation_date: date
) -> float | None:
    labels = tuple(_shift_month(formation_date, -offset) for offset in range(12, 1, -1))
    if any(label not in monthly for label in labels):
        return None
    return math.prod(1 + monthly[label][2] for label in labels) - 1


def _forward_return(asset: _PreparedAsset, formation_date: date) -> float | None:
    dates = asset.expected_daily
    entry = next((day for day in dates if day > formation_date), None)
    next_rebalance = _shift_month(formation_date, 1)
    exit_date = next((day for day in dates if day > next_rebalance), None)
    if entry is None or exit_date is None:
        return None
    holding_dates = tuple(day for day in dates if entry < day <= exit_date)
    if not holding_dates or any(day not in asset.daily for day in holding_dates):
        return None
    return math.prod(1 + asset.daily[day][2] for day in holding_dates) - 1


def _formation_dates(start: date, end: date) -> tuple[date, ...]:
    current = month_end(start)
    rows: list[date] = []
    while current <= end:
        rows.append(current)
        current = _shift_month(current, 1)
    return tuple(rows)


def _shift_month(value: date, months: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_zero = divmod(month_index, 12)
    month = month_zero + 1
    return date(year, month, calendar.monthrange(year, month)[1])


def _configuration_ref(
    parameters: CrossSectionalSignalParameters,
    instruments: tuple[InstrumentId, ...],
) -> str:
    payload = {
        "parameters": parameters.model_dump(mode="json"),
        "instruments": [item.model_dump(mode="json") for item in instruments],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
