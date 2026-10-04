"""Source adapters normalize historical levels to bounded, dated total returns.

No vendor payload or input return series is included in factor reports.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator

from trade_research.domain import InstrumentId
from trade_research.domain.models import DomainModel, FactorReturnPoint, FactorReturnSeries
from trade_research.providers.contracts import (
    MAX_HTTP_BYTES,
    OptionalProviderDependencyError,
    ProviderConfigurationError,
    ProviderContractError,
)
from trade_research.providers.remote import (
    HttpGet,
    _bloomberg_get,
    _bloomberg_has,
    _bloomberg_rows,
    _bloomberg_scalar,
    _http_get,
    resolve_provider_symbol,
)


def returns_from_levels(
    levels: Sequence[tuple[date, float | None]],
) -> tuple[FactorReturnPoint, ...]:
    """Use adjacent original observations only; never bridge missing prices."""
    if len(levels) > 4097:
        raise ProviderContractError("factor history exceeded its point limit")
    points: list[FactorReturnPoint] = []
    previous: tuple[date, float | None] | None = None
    for day, value in levels:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ProviderContractError("total-return levels must be positive and finite")
        if previous is not None:
            prev_day, prev_value = previous
            if day <= prev_day:
                raise ProviderContractError("factor history dates must be unique and ordered")
            if value is not None and prev_value is not None:
                points.append(
                    FactorReturnPoint(
                        start_date=prev_day, end_date=day, value=value / prev_value - 1
                    )
                )
        previous = (day, value)
    return tuple(points)


class InlineReturnProvider:
    """Exact supplied histories, with no network fallback."""

    def __init__(self, series: tuple[FactorReturnSeries, ...]) -> None:
        self._series = {item.instrument: item for item in series}

    def return_history(
        self, instrument: InstrumentId, start: date, end: date
    ) -> FactorReturnSeries:
        if instrument not in self._series:
            raise ProviderConfigurationError("a required factor return series was not supplied")
        return self._series[instrument]


class YahooReturnProvider:
    """Daily dividend-adjusted close returns, separate from execution OHLCV."""

    def __init__(self, http_get: HttpGet = _http_get) -> None:
        self._http_get = http_get

    def return_history(
        self, instrument: InstrumentId, start: date, end: date
    ) -> FactorReturnSeries:
        if instrument.market in {"EU", "EURONEXT", "CRYPTO", "PORTFOLIO"}:
            raise ProviderConfigurationError(
                "factor returns require a precise supported equity venue"
            )
        symbol = resolve_provider_symbol("yahoo", instrument)
        begin = int(
            datetime.combine(start - timedelta(days=7), datetime.min.time(), tzinfo=UTC).timestamp()
        )
        stop = int(
            datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=UTC).timestamp()
        )
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol, safe='')}"
            f"?period1={begin}&period2={stop}&interval=1d"
        )
        raw = self._http_get(url, {"Accept": "application/json"})
        if len(raw.encode()) > MAX_HTTP_BYTES:
            raise ProviderContractError("Yahoo factor response exceeded the byte limit")
        try:
            result = json.loads(raw)["chart"]["result"][0]
            meta = result["meta"]
            if meta["symbol"].upper() != symbol.upper():
                raise ProviderContractError("Yahoo returned the wrong security")
            if meta.get("instrumentType") not in {"EQUITY", "ETF"}:
                raise ProviderContractError(
                    "Yahoo factor returns require an equity or ETF; "
                    "index conventions are unverified"
                )
            timezone = ZoneInfo(meta["exchangeTimezoneName"])
            currency = "GBP" if meta["currency"] in {"GBp", "GBX"} else meta["currency"]
            timestamps = result["timestamp"]
            adjusted = result["indicators"]["adjclose"][0]["adjclose"]
            if not isinstance(timestamps, list) or not isinstance(adjusted, list):
                raise ProviderContractError("Yahoo returned malformed arrays")
            levels = [
                (datetime.fromtimestamp(float(t), timezone).date(), None if v is None else float(v))
                for t, v in zip(timestamps, adjusted, strict=True)
            ]
            points = returns_from_levels(levels)
            return FactorReturnSeries(
                instrument=instrument,
                currency=currency,
                return_basis="adjusted_close_return",
                source="yahoo",
                vendor_field="ADJ_CLOSE",
                retrieved_at=datetime.now(UTC),
                points=tuple(p for p in points if start <= p.start_date and p.end_date <= end),
            )
        except (KeyError, IndexError, TypeError, ValueError, OverflowError) as error:
            raise ProviderContractError(
                "Yahoo returned invalid adjusted-close history or metadata"
            ) from error


class BloombergLevelMapping(DomainModel):
    """Explicit reference-data identity; no ticker inference or executable formulas."""

    security: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9./_-]{0,40} (?:[A-Z]{2} Equity|Index|Comdty)$",
    )
    field: Literal["PX_LAST", "TOTAL_RETURN_INDEX_GROSS_DVDS"]


class BloombergReturnMapping(DomainModel):
    """Administrator-verified security/field identity, never inferred from a suffix."""

    instrument: InstrumentId
    security: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9./_-]{0,40} (?:[A-Z]{2} Equity|Index)$",
    )
    field: Literal["PX_LAST", "TOTAL_RETURN_INDEX_GROSS_DVDS"]
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    return_basis: Literal["gross_total_return", "net_total_return"]

    @model_validator(mode="after")
    def validate_field(self) -> BloombergReturnMapping:
        if self.field == "PX_LAST" and self.instrument.market != "INDEX":
            raise ValueError("PX_LAST is allowed only for explicitly mapped total-return indexes")
        if (
            self.field == "TOTAL_RETURN_INDEX_GROSS_DVDS"
            and self.return_basis != "gross_total_return"
        ):
            raise ValueError("gross-dividend field requires gross total returns")
        return self


class BloombergReturnProvider:
    """Historical reference-data adapter with exclusive request ownership.

    A firm integration may supply a dedicated, started/authorized blpapi session
    and identity. Otherwise a host/port session is opened per call (Desktop or
    an already authorized service configuration). B-PIPE authentication is not
    guessed. Supplied sessions are caller-owned; their use outside this adapter
    must not consume this adapter's events.
    """

    def __init__(
        self,
        mappings: tuple[BloombergReturnMapping, ...],
        *,
        host: str = "localhost",
        port: int = 8194,
        session: Any = None,
        identity: Any = None,
    ) -> None:
        if len({item.instrument for item in mappings}) != len(mappings):
            raise ValueError("Bloomberg mappings must be unique")
        self._mappings = {item.instrument: item for item in mappings}
        self._host, self._port = host, port
        self._session, self._identity = session, identity
        self._lock = threading.Lock()

    def return_history(
        self, instrument: InstrumentId, start: date, end: date
    ) -> FactorReturnSeries:
        mapping = self._mappings.get(instrument)
        if mapping is None:
            raise ProviderConfigurationError(
                "a verified Bloomberg total-return mapping is required"
            )
        levels = self.level_history(mapping, start, end)
        points = returns_from_levels(levels)
        return FactorReturnSeries(
            instrument=instrument,
            source="bloomberg",
            currency=mapping.currency,
            return_basis=mapping.return_basis,
            vendor_field=mapping.field,
            retrieved_at=datetime.now(UTC),
            points=tuple(p for p in points if start <= p.start_date and p.end_date <= end),
        )

    def level_history(
        self, mapping: BloombergReturnMapping | BloombergLevelMapping, start: date, end: date
    ) -> list[tuple[date, float | None]]:
        """Read daily levels without assigning return semantics to a price field."""
        try:
            import blpapi
        except ImportError as error:
            raise OptionalProviderDependencyError(
                "Bloomberg requires the optional blpapi dependency"
            ) from error
        # Separate from the old OHLCV adapter: no shared event-stream race.
        with self._lock:
            session = self._session
            owned = session is None
            if owned:
                options = blpapi.SessionOptions()
                options.setServerHost(self._host)
                options.setServerPort(self._port)
                session = blpapi.Session(options)
            correlation = None
            complete = False
            try:
                if owned and not session.start():
                    raise ProviderConfigurationError("Bloomberg session could not start")
                if not session.openService("//blp/refdata"):
                    raise ProviderConfigurationError(
                        "Bloomberg historical reference service unavailable"
                    )
                request = session.getService("//blp/refdata").createRequest("HistoricalDataRequest")
                request.append("securities", mapping.security)
                request.append("fields", mapping.field)
                request.set("startDate", (start - timedelta(days=7)).strftime("%Y%m%d"))
                request.set("endDate", end.strftime("%Y%m%d"))
                request.set("periodicitySelection", "DAILY")
                correlation = session.sendRequest(request, identity=self._identity)
                levels: list[tuple[date, float | None]] = []
                deadline = time.monotonic() + 30
                while not complete:
                    if time.monotonic() >= deadline:
                        raise ProviderConfigurationError("Bloomberg historical request timed out")
                    event = session.nextEvent(timeout=1000)
                    if event.eventType() == blpapi.Event.REQUEST_STATUS:
                        if any(correlation in message.correlationIds() for message in event):
                            raise ProviderConfigurationError("Bloomberg historical request failed")
                        continue
                    if event.eventType() not in (
                        blpapi.Event.RESPONSE,
                        blpapi.Event.PARTIAL_RESPONSE,
                    ):
                        continue
                    matched = False
                    for message in event:
                        if correlation not in message.correlationIds():
                            continue
                        matched = True
                        if _bloomberg_has(message, "responseError"):
                            raise ProviderConfigurationError(
                                "Bloomberg historical request was rejected"
                            )
                        security = _bloomberg_get(message, "securityData")
                        if security is None or _bloomberg_has(security, "securityError"):
                            raise ProviderConfigurationError("Bloomberg security is unavailable")
                        if (
                            str(_bloomberg_scalar(_bloomberg_get(security, "security")))
                            != mapping.security
                        ):
                            raise ProviderContractError("Bloomberg returned the wrong security")
                        if _bloomberg_rows(_bloomberg_get(security, "fieldExceptions")):
                            raise ProviderConfigurationError(
                                "Bloomberg total-return field is unavailable"
                            )
                        for row in _bloomberg_rows(_bloomberg_get(security, "fieldData")):
                            day = date.fromisoformat(
                                str(_bloomberg_scalar(_bloomberg_get(row, "date")))
                            )
                            value = _bloomberg_scalar(_bloomberg_get(row, mapping.field))
                            if value is not None and (
                                isinstance(value, bool) or not isinstance(value, str | int | float)
                            ):
                                raise ProviderContractError("Bloomberg returned an invalid level")
                            levels.append((day, None if value is None else float(value)))
                            if len(levels) > 4097:
                                raise ProviderContractError(
                                    "Bloomberg exceeded the factor history limit"
                                )
                    complete = matched and event.eventType() == blpapi.Event.RESPONSE
                if any(b[0] <= a[0] for a, b in zip(levels[:-1], levels[1:], strict=True)):
                    raise ProviderContractError("Bloomberg dates must be ordered and unique")
                if any(value is not None and not math.isfinite(value) for _, value in levels):
                    raise ProviderContractError("Bloomberg levels must be finite")
                return levels
            except (ValueError, TypeError, OverflowError) as error:
                raise ProviderContractError("Bloomberg returned invalid historical data") from error
            finally:
                if owned:
                    session.stop()
                elif correlation is not None and not complete:
                    try:
                        session.cancel(correlation)
                    except Exception:
                        pass  # Preserve the original failure if cleanup also fails.
