"""Calendar-aware frequency processing with explicit coverage accounting."""

from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import date, timedelta

import exchange_calendars as xc

from trade_research.domain.models import FactorFrequency, FactorReturnSeries, FxLevelSeries

CALENDARS = {
    "US": "XNYS",
    "NASDAQ": "XNYS",
    "NYSE": "XNYS",
    "AMEX": "XNYS",
    "ETF": "XNYS",
    "OTC": "XNYS",
    "XETRA": "XETR",
    "LSE": "XLON",
    "UK": "XLON",
    "AIM": "XLON",
    "SIX": "XSWX",
    "BME": "XMAD",
    "BORSA_ITALIANA": "XMIL",
    "EURONEXT_PARIS": "XPAR",
    "EURONEXT_AMSTERDAM": "XAMS",
    "EURONEXT_BRUSSELS": "XBRU",
    "EURONEXT_LISBON": "XLIS",
    "COPENHAGEN": "XCSE",
    "HELSINKI": "XHEL",
    "EURONEXT_DUBLIN": "XDUB",
    "OSLO": "XOSL",
    "STOCKHOLM": "XSTO",
    "VIENNA": "XWBO",
}
type PeriodRows = dict[date, tuple[date, date, float]]


@dataclass(frozen=True)
class PreparedReturns:
    rows: PeriodRows
    expected: tuple[date, ...]
    invalid_count: int

    @property
    def expected_count(self) -> int:
        return len(self.expected)


def session_dates(market: str, start: date, end: date) -> list[date]:
    if market == "weekdays":
        return [
            start + timedelta(days=i)
            for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() < 5
        ]
    name = CALENDARS.get(market, market)
    if name not in xc.get_calendar_names():
        raise ValueError("a supported exchange calendar is required")
    cal = xc.get_calendar(name, start=start - timedelta(days=7), end=end + timedelta(days=7))
    return list(cal.sessions_in_range(start, end).date)


def month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def prepare_returns(
    series: FactorReturnSeries,
    frequency: FactorFrequency,
    start: date,
    end: date,
    *,
    calendar_market: str | None = None,
) -> PreparedReturns:
    if series.frequency == "monthly" and frequency != "monthly":
        raise ValueError("source frequency cannot be upsampled")
    days = session_dates(
        calendar_market or series.calendar or series.instrument.market,
        start - timedelta(days=40),
        month_end(end),
    )
    previous = dict(zip(days[1:], days[:-1], strict=True))
    supplied = {p.end_date: p for p in series.points}
    expected_daily = tuple(
        day for day in days if day <= end and previous.get(day, start) >= start and day > start
    )
    daily = {p.end_date: p for p in series.points if previous.get(p.end_date) == p.start_date}
    if frequency == "daily":
        rows = {
            day: (daily[day].start_date, day, daily[day].value)
            for day in expected_daily
            if day in daily
        }
        return PreparedReturns(rows, expected_daily, len(expected_daily) - len(rows))
    months: dict[date, list[date]] = {}
    for day in days:
        months.setdefault(month_end(day), []).append(day)
    expected = tuple(
        label for label in months if date(label.year, label.month, 1) >= start and label <= end
    )
    rows = {}
    for label in expected:
        sessions = months[label]
        opening = previous[sessions[0]]
        closing = sessions[-1]
        if series.frequency == "monthly":
            point = supplied.get(closing)
            if point is not None and point.start_date == opening:
                rows[label] = (opening, closing, point.value)
        elif all(day in daily for day in sessions):
            rows[label] = (
                opening,
                closing,
                math.prod(1 + daily[day].value for day in sessions) - 1,
            )
    return PreparedReturns(rows, expected, len(expected) - len(rows))


def period_returns(
    series: FactorReturnSeries,
    frequency: FactorFrequency,
    start: date,
    end: date,
    *,
    calendar_market: str | None = None,
) -> PeriodRows:
    return prepare_returns(series, frequency, start, end, calendar_market=calendar_market).rows


def convert_periods(rows: PeriodRows, fx: FxLevelSeries) -> PeriodRows:
    levels = {p.date: p.value for p in fx.points}
    return {
        day: (a, b, (1 + value) * levels[b] / levels[a] - 1)
        for day, (a, b, value) in rows.items()
        if a in levels and b in levels
    }
