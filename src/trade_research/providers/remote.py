"""Optional bounded remote adapters with canonical opaque source references."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from types import ModuleType
from typing import Any, cast
from urllib.parse import quote

import httpx

from trade_research.domain import InstrumentId
from trade_research.providers.contracts import (
    MAX_HTTP_BYTES,
    MAX_PRICE_POINTS,
    OptionalProviderDependencyError,
    PricePoint,
    ProviderConfigurationError,
    ProviderContractError,
)

HttpGet = Callable[[str, Mapping[str, str]], str]

_RETRY_LIMIT = 3
_RETRY_BACKOFF_BASE = 0.5
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

_YAHOO_SUFFIXES = {
    "US": "",
    "NASDAQ": "",
    "NYSE": "",
    "AMEX": "",
    "OTC": "",
    "ETF": "",
    "UK": ".L",
    "LSE": ".L",
    "AIM": ".L",
    "EU": ".PA",
    "EURONEXT": ".PA",
    "INDEX": "",
    "XETRA": ".DE",
    "BME": ".MC",
    "BORSA_ITALIANA": ".MI",
    "SIX": ".SW",
    "SSE": ".SS",
    "SZSE": ".SZ",
    "BJSE": ".BJ",
}

_BLOOMBERG_SUFFIXES = {
    "US": " US Equity",
    "NASDAQ": " US Equity",
    "NYSE": " US Equity",
    "AMEX": " US Equity",
    "OTC": " US Equity",
    "ETF": " US Equity",
    "UK": " LN Equity",
    "LSE": " LN Equity",
    "AIM": " LN Equity",
    "EU": " EB Equity",
    "EURONEXT": " NA Equity",
    "INDEX": " Index",
    "XETRA": " GY Equity",
    "BME": " SM Equity",
    "BORSA_ITALIANA": " IM Equity",
    "SIX": " SW Equity",
    "SSE": " CH Equity",
    "SZSE": " CH Equity",
    "BJSE": " CH Equity",
}


def resolve_provider_symbol(provider: str, instrument: InstrumentId) -> str:
    """Map a validated market to one provider's bounded symbol convention."""

    normalized_provider = provider.strip().lower()
    if normalized_provider == "ccxt":
        if instrument.market != "CRYPTO":
            raise ProviderConfigurationError("CCXT accepts only CRYPTO instruments")
        return instrument.symbol
    suffixes = {"yahoo": _YAHOO_SUFFIXES, "bloomberg": _BLOOMBERG_SUFFIXES}.get(normalized_provider)
    if suffixes is None:
        raise ProviderConfigurationError(f"unknown market-symbol provider '{provider}'")
    try:
        suffix = suffixes[instrument.market]
    except KeyError as error:
        raise ProviderConfigurationError(
            f"{normalized_provider} does not support market '{instrument.market}'"
        ) from error
    symbol = f"{instrument.symbol}{suffix}"
    return symbol if normalized_provider in {"yahoo", "bloomberg"} else symbol.lower()


def _http_get(url: str, headers: Mapping[str, str]) -> str:
    """Fetch a URL with retry and exponential backoff for transient failures.

    Retries on timeouts, HTTP 429 (rate-limit), and HTTP 5xx errors up to
    _RETRY_LIMIT attempts with exponential backoff starting at _RETRY_BACKOFF_BASE
    seconds. Raises ProviderConfigurationError after all retries are exhausted.
    """
    merged = {"User-Agent": "trade-research/0.1.0", **dict(headers)}
    last_error: Exception | None = None
    for attempt in range(_RETRY_LIMIT):
        try:
            response = httpx.get(url, headers=merged, timeout=10.0, follow_redirects=True)
            if response.status_code in _RETRYABLE_STATUSES:
                raise ProviderConfigurationError(
                    f"remote provider returned HTTP {response.status_code}"
                )
            response.raise_for_status()
            if len(response.content) > MAX_HTTP_BYTES:
                raise ProviderContractError("remote provider exceeded the byte limit")
            try:
                return response.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ProviderContractError("remote provider returned invalid UTF-8") from error
        except (httpx.TimeoutException, httpx.ConnectError) as error:
            last_error = error
        except ProviderConfigurationError:
            raise
        if attempt < _RETRY_LIMIT - 1:
            time.sleep(_RETRY_BACKOFF_BASE * (2**attempt))
    raise ProviderConfigurationError("remote provider request failed after retries") from last_error


def _bounded_payload(payload: str) -> str:
    if len(payload.encode("utf-8")) > MAX_HTTP_BYTES:
        raise ProviderContractError("remote provider exceeded the byte limit")
    return payload


class YahooPriceProvider:
    """Yahoo chart endpoint adapter with a fixed symbol-only request shape.

    Returns **adjusted close** prices from the Yahoo Finance v8 chart API.
    The ``close`` values are adjusted for splits and dividends.  Open, high,
    low, and volume are **unadjusted** as reported by the exchange.
    """

    _CHART_WINDOWS = frozenset(
        {
            ("1y", "1d"),
            ("10y", "1d"),
            ("60d", "30m"),
        }
    )

    def __init__(
        self,
        http_get: HttpGet = _http_get,
        *,
        range_: str = "1y",
        interval: str = "1d",
    ) -> None:
        if (range_, interval) not in self._CHART_WINDOWS:
            raise ValueError("unsupported Yahoo chart window")
        self._http_get = http_get
        self._range = range_
        self._interval = interval

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        symbol = resolve_provider_symbol("yahoo", instrument)
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{quote(symbol, safe='')}?range={self._range}&interval={self._interval}"
        )
        payload_text = _bounded_payload(self._http_get(url, {"Accept": "application/json"}))
        try:
            payload = json.loads(payload_text)
            result = payload["chart"]["result"][0]
            quote_data = result["indicators"]["quote"][0]
            timestamps = result["timestamp"]
            series = (
                quote_data["open"],
                quote_data["high"],
                quote_data["low"],
                quote_data["close"],
                quote_data["volume"],
            )
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise ProviderContractError("Yahoo returned a malformed response") from error
        if not isinstance(timestamps, list) or any(not isinstance(item, list) for item in series):
            raise ProviderContractError("Yahoo returned a malformed price series")
        if len(timestamps) > MAX_PRICE_POINTS:
            raise ProviderContractError("Yahoo exceeded the point limit")
        if any(len(item) != len(timestamps) for item in series):
            raise ProviderContractError("Yahoo returned mismatched price arrays")
        snapshot = _canonical_reference(payload)
        return tuple(
            PricePoint(
                instrument=instrument,
                observed_at=datetime.fromtimestamp(float(timestamp), tz=UTC),
                close=float(close),
                source="yahoo",
                provenance={
                    "provider_kind": "yahoo",
                    "vendor_field": "OHLCV",
                    "snapshot_ref": snapshot,
                    "reference": _canonical_reference(
                        [timestamp, open_value, high, low, close, volume]
                    ),
                },
                open=_optional_float(open_value),
                high=_optional_float(high),
                low=_optional_float(low),
                volume=_optional_float(volume),
            )
            for timestamp, open_value, high, low, close, volume in zip(
                timestamps, *series, strict=True
            )
            if close is not None
        )


class CcxtPriceProvider:
    """Optional crypto adapter, loaded only when CCXT is installed and configured."""

    def __init__(self, exchange_id: str) -> None:
        self._exchange_id = exchange_id

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        symbol = resolve_provider_symbol("ccxt", instrument)
        try:
            import ccxt  # type: ignore[import-not-found]
        except ImportError as error:
            raise OptionalProviderDependencyError(
                "CCXT provider requires the optional 'ccxt' dependency. "
                "Install it with: pip install ccxt"
            ) from error
        try:
            exchange_type = getattr(ccxt, self._exchange_id)
        except AttributeError as error:
            raise ProviderConfigurationError(
                f"unknown CCXT exchange '{self._exchange_id}'"
            ) from error
        candles = exchange_type({"enableRateLimit": True}).fetch_ohlcv(
            symbol, timeframe="1d", limit=MAX_PRICE_POINTS + 1
        )
        if not isinstance(candles, list) or len(candles) > MAX_PRICE_POINTS:
            raise ProviderContractError("CCXT exceeded the point limit")
        snapshot = _canonical_reference(candles)
        try:
            return tuple(
                PricePoint(
                    instrument=instrument,
                    observed_at=datetime.fromtimestamp(float(candle[0]) / 1000, tz=UTC),
                    close=float(candle[4]),
                    source="ccxt",
                    provenance={
                        "provider_kind": "ccxt",
                        "vendor_field": "OHLCV",
                        "snapshot_ref": snapshot,
                        "reference": _canonical_reference(candle),
                    },
                    open=float(candle[1]),
                    high=float(candle[2]),
                    low=float(candle[3]),
                    volume=float(candle[5]),
                )
                for candle in candles
            )
        except (IndexError, TypeError, ValueError) as error:
            raise ProviderContractError("CCXT returned malformed candles") from error


class BloombergPriceProvider:
    """Optional BPIPE historical OHLCV adapter with lazy blpapi loading."""

    def __init__(self, host: str = "localhost", port: int = 8194) -> None:
        self._host = host
        self._port = port
        self._session: Any | None = None
        self._service: Any | None = None
        self._blpapi: ModuleType | None = None
        self._lock = threading.Lock()

    def price_history(self, instrument: InstrumentId) -> tuple[PricePoint, ...]:
        ticker = resolve_provider_symbol("bloomberg", instrument)
        session, service, blpapi = self._ensure_session()

        today = datetime.now(UTC).date()
        start = today.replace(year=today.year - 1)
        request = service.createRequest("HistoricalDataRequest")
        for field in ("PX_OPEN", "PX_HIGH", "PX_LOW", "PX_LAST", "PX_VOLUME"):
            request.append("fields", field)
        request.append("securities", ticker)
        request.set("startDate", start.strftime("%Y%m%d"))
        request.set("endDate", today.strftime("%Y%m%d"))
        request.set("periodicitySelection", "DAILY")
        session.sendRequest(request)

        raw_rows: list[dict[str, object]] = []
        while True:
            event = session.nextEvent(timeout=30000)
            event_type = event.eventType()
            if event_type in (blpapi.Event.RESPONSE, blpapi.Event.PARTIAL_RESPONSE):
                for message in event:
                    _bloomberg_process_message(message, raw_rows)
            if event_type == blpapi.Event.RESPONSE:
                break
            if event_type == blpapi.Event.TIMEOUT:
                raise ProviderConfigurationError(
                    "Bloomberg BPIPE request timed out waiting for data"
                )

        if len(raw_rows) > MAX_PRICE_POINTS:
            raise ProviderContractError("Bloomberg exceeded the point limit")

        snapshot = _canonical_reference(raw_rows)
        try:
            return tuple(
                PricePoint(
                    instrument=instrument,
                    observed_at=_bloomberg_row_date(row["date"]),
                    close=float(cast(float | int | str, row["close"])),
                    source="bloomberg",
                    provenance={
                        "provider_kind": "bloomberg",
                        "vendor_field": "OHLCV",
                        "snapshot_ref": snapshot,
                        "reference": _canonical_reference(row),
                    },
                    open=_optional_float(row.get("open")),
                    high=_optional_float(row.get("high")),
                    low=_optional_float(row.get("low")),
                    volume=_optional_float(row.get("volume")),
                )
                for row in raw_rows
                if row.get("close") is not None
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProviderContractError("Bloomberg returned malformed price rows") from error

    def _ensure_session(self) -> tuple[Any, Any, ModuleType]:
        with self._lock:
            if self._session is not None and self._service is not None and self._blpapi is not None:
                return self._session, self._service, self._blpapi
            try:
                import blpapi
            except ImportError as error:
                raise OptionalProviderDependencyError(
                    "Bloomberg provider requires the optional 'blpapi' dependency."
                ) from error
            options = blpapi.SessionOptions()
            options.setServerHost(self._host)
            options.setServerPort(self._port)
            session = blpapi.Session(options)
            if not session.start():
                raise ProviderConfigurationError("Bloomberg BPIPE session failed to start")
            if not session.openService("//blp/refdata"):
                session.stop()
                raise ProviderConfigurationError(
                    "Bloomberg BPIPE could not open //blp/refdata service"
                )
            service = session.getService("//blp/refdata")
            self._session = session
            self._service = service
            self._blpapi = blpapi
            return session, service, blpapi

    def __del__(self) -> None:
        session = self._session
        if session is not None:
            try:
                session.stop()
            except Exception:
                pass


def _bloomberg_process_message(message: object, raw_rows: list[dict[str, object]]) -> None:
    if _bloomberg_has(message, "responseError"):
        raise ProviderConfigurationError("Bloomberg returned an error response")
    security_data = _bloomberg_get(message, "securityData")
    if security_data is None:
        return
    if _bloomberg_has(security_data, "securityError"):
        raise ProviderConfigurationError("Bloomberg returned an error for the security")
    field_data = _bloomberg_get(security_data, "fieldData")
    for row in _bloomberg_rows(field_data):
        date_value = _bloomberg_get(row, "date")
        close = _bloomberg_get(row, "PX_LAST")
        if date_value is None or close is None:
            continue
        raw_rows.append(
            {
                "date": str(_bloomberg_scalar(date_value)),
                "open": _bloomberg_scalar(_bloomberg_get(row, "PX_OPEN")),
                "high": _bloomberg_scalar(_bloomberg_get(row, "PX_HIGH")),
                "low": _bloomberg_scalar(_bloomberg_get(row, "PX_LOW")),
                "close": _bloomberg_scalar(close),
                "volume": _bloomberg_scalar(_bloomberg_get(row, "PX_VOLUME")),
            }
        )


def _bloomberg_has(value: object, name: str) -> bool:
    if isinstance(value, Mapping):
        return name in value
    has_element = getattr(value, "hasElement", None)
    return bool(callable(has_element) and has_element(name))


def _bloomberg_get(value: object, name: str) -> object | None:
    if isinstance(value, Mapping):
        return value.get(name)
    get_element = getattr(value, "getElement", None)
    if callable(get_element) and _bloomberg_has(value, name):
        return cast(object, get_element(name))
    get_as_string = getattr(value, "getElementAsString", None)
    if callable(get_as_string) and _bloomberg_has(value, name):
        return cast(object, get_as_string(name))
    return None


def _bloomberg_rows(field_data: object) -> tuple[object, ...]:
    if field_data is None:
        return ()
    if isinstance(field_data, list | tuple):
        return tuple(field_data)
    values = getattr(field_data, "values", None)
    if callable(values):
        return tuple(values())
    num_values = getattr(field_data, "numValues", None)
    get_value = getattr(field_data, "getValueAsElement", None)
    if callable(num_values) and callable(get_value):
        return tuple(get_value(index) for index in range(int(num_values())))
    if isinstance(field_data, Iterable):
        return tuple(field_data)
    return ()


def _bloomberg_scalar(value: object) -> object:
    if value is None or isinstance(value, str | int | float):
        return value
    get_value = getattr(value, "getValue", None)
    if callable(get_value):
        return get_value()
    return str(value)


def _bloomberg_row_date(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return datetime.strptime(str(value), "%Y-%m-%d").replace(tzinfo=UTC)


def _canonical_reference(value: object) -> str:
    if isinstance(value, str):
        encoded = value.encode()
    else:
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _optional_float(value: object) -> float | None:
    return None if value is None else float(cast(float | int | str, value))


def _row_float(row: Mapping[str, str], field: str) -> float | None:
    value = row.get(field)
    if value is None or value in ("", "N/D"):
        return None
    return float(value)
