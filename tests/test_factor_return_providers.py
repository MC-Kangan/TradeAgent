import json
from datetime import date
from unittest.mock import patch

import httpx
import pytest

from trade_research.domain import InstrumentId
from trade_research.providers import ProviderContractError
from trade_research.providers.factor_returns import YahooReturnProvider, returns_from_levels
from trade_research.providers.remote import _http_get


def payload():
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "currency": "USD",
                        "exchangeTimezoneName": "America/New_York",
                        "symbol": "ACME",
                        "instrumentType": "EQUITY",
                    },
                    "timestamp": [1704205800, 1704292200, 1704378600],
                    "indicators": {
                        "quote": [{"close": [100, 100, 100]}],
                        "adjclose": [{"adjclose": [100, 101, 102]}],
                    },
                }
            ]
        }
    }


@pytest.mark.parametrize("instrument_type", ["EQUITY", "ETF"])
def test_yahoo_uses_adjusted_prices_and_local_session_dates(instrument_type):
    data = payload()
    data["chart"]["result"][0]["meta"]["instrumentType"] = instrument_type
    provider = YahooReturnProvider(http_get=lambda *_: json.dumps(data))
    series = provider.return_history(
        InstrumentId(symbol="ACME", market="US"), date(2024, 1, 1), date(2024, 1, 10)
    )
    assert series.points[0].value == pytest.approx(0.01)
    assert series.points[0].start_date == date(2024, 1, 2)
    assert series.return_basis == "adjusted_close_return"
    assert series.vendor_field == "ADJ_CLOSE"


def test_yahoo_never_falls_back_to_unadjusted_prices():
    data = payload()
    del data["chart"]["result"][0]["indicators"]["adjclose"]
    with pytest.raises(ProviderContractError):
        YahooReturnProvider(http_get=lambda *_: json.dumps(data)).return_history(
            InstrumentId(symbol="ACME", market="US"), date(2024, 1, 1), date(2024, 1, 10)
        )


def test_missing_level_breaks_return_chain():
    points = returns_from_levels(
        [
            (date(2024, 1, 2), 100.0),
            (date(2024, 1, 3), None),
            (date(2024, 1, 4), 102.0),
            (date(2024, 1, 5), 103.0),
        ]
    )
    assert len(points) == 1
    assert points[0].start_date == date(2024, 1, 4)


def test_retryable_status_retries_before_success():
    req = httpx.Request("GET", "https://example.com")
    with (
        patch(
            "trade_research.providers.remote.httpx.get",
            side_effect=[
                httpx.Response(429, request=req),
                httpx.Response(200, text="{}", request=req),
            ],
        ) as get,
        patch("trade_research.providers.remote.time.sleep"),
    ):
        assert _http_get(str(req.url), {}) == "{}"
        assert get.call_count == 2


def test_bloomberg_equity_px_last_requires_adjusted_close_semantics():
    from pydantic import ValidationError

    from trade_research.providers.factor_returns import BloombergReturnMapping

    values = dict(
        instrument=InstrumentId(symbol="ACME", market="US"),
        security="ACME US Equity",
        field="PX_LAST",
        currency="USD",
    )
    mapping = BloombergReturnMapping(**values, return_basis="adjusted_close_return")
    assert mapping.field == "PX_LAST"
    with pytest.raises(ValidationError):
        BloombergReturnMapping(**values, return_basis="gross_total_return")
    index = BloombergReturnMapping(
        instrument=InstrumentId(symbol="MSCI-EU-GTR-EUR", market="INDEX"),
        security="MXEU Index",
        field="PX_LAST",
        currency="EUR",
        return_basis="gross_total_return",
    )
    assert index.return_basis == "gross_total_return"


@pytest.mark.parametrize("problem", [None, "wrong_security", "field_error"])
def test_bloomberg_correlated_historical_response(monkeypatch, problem):
    import sys
    from types import SimpleNamespace

    from trade_research.providers import ProviderConfigurationError
    from trade_research.providers.factor_returns import (
        BloombergReturnMapping,
        BloombergReturnProvider,
    )

    instrument = InstrumentId(symbol="ACME", market="US")
    mapping = BloombergReturnMapping(
        instrument=instrument,
        security="ACME US Equity",
        field="TOTAL_RETURN_INDEX_GROSS_DVDS",
        currency="USD",
        return_basis="gross_total_return",
    )

    class Request:
        def __init__(self):
            self.values = {}

        def append(self, key, value):
            self.values[key] = value

        def set(self, key, value):
            self.values[key] = value

    class Message(dict):
        def correlationIds(self):
            return (session.correlation,)

    class Event(list):
        def eventType(self):
            return 1

    class Session:
        stopped = False

        def openService(self, name):
            return True

        def getService(self, name):
            return self

        def createRequest(self, name):
            self.request = Request()
            return self.request

        def sendRequest(self, request, identity):
            self.identity, self.correlation = identity, object()
            return self.correlation

        def cancel(self, correlation):
            assert correlation is self.correlation

        def nextEvent(self, timeout):
            return Event(
                [
                    Message(
                        securityData={
                            "security": "WRONG US Equity"
                            if problem == "wrong_security"
                            else mapping.security,
                            "fieldExceptions": [{"error": "unavailable"}]
                            if problem == "field_error"
                            else [],
                            "fieldData": [
                                {"date": "2024-01-02", mapping.field: 100},
                                {"date": "2024-01-03", mapping.field: 102},
                            ],
                        }
                    )
                ]
            )

        def stop(self):
            self.stopped = True

    session = Session()
    identity = object()
    monkeypatch.setitem(
        sys.modules,
        "blpapi",
        SimpleNamespace(
            CorrelationId=object,
            Event=SimpleNamespace(RESPONSE=1, PARTIAL_RESPONSE=2, REQUEST_STATUS=3),
        ),
    )
    provider = BloombergReturnProvider((mapping,), session=session, identity=identity)
    if problem:
        with pytest.raises((ProviderConfigurationError, ProviderContractError)):
            provider.return_history(instrument, date(2024, 1, 1), date(2024, 1, 5))
    else:
        series = provider.return_history(instrument, date(2024, 1, 1), date(2024, 1, 5))
        assert series.points[0].value == pytest.approx(0.02)
        assert series.currency == "USD"
        assert session.identity is identity
        assert session.request.values["fields"] == mapping.field
    assert not session.stopped


def test_bloomberg_equity_px_last_sets_explicit_corporate_action_adjustments(monkeypatch):
    import sys
    from types import SimpleNamespace

    from trade_research.providers.factor_returns import (
        BloombergReturnMapping,
        BloombergReturnProvider,
    )

    instrument = InstrumentId(symbol="ACME", market="US")
    mapping = BloombergReturnMapping(
        instrument=instrument,
        security="ACME US Equity",
        field="PX_LAST",
        currency="USD",
        return_basis="adjusted_close_return",
    )

    class Request:
        def __init__(self):
            self.values = {}

        def append(self, key, value):
            self.values[key] = value

        def set(self, key, value):
            self.values[key] = value

    class Message(dict):
        def correlationIds(self):
            return (session.correlation,)

    class Event(list):
        def eventType(self):
            return 1

    class Session:
        def openService(self, _name):
            return True

        def getService(self, _name):
            return self

        def createRequest(self, _name):
            self.request = Request()
            return self.request

        def sendRequest(self, _request, identity):
            self.correlation = object()
            return self.correlation

        def nextEvent(self, timeout):
            return Event(
                [
                    Message(
                        securityData={
                            "security": mapping.security,
                            "fieldData": [
                                {"date": "2024-01-02", "PX_LAST": 100},
                                {"date": "2024-01-03", "PX_LAST": 101},
                            ],
                        }
                    )
                ]
            )

    session = Session()
    monkeypatch.setitem(
        sys.modules,
        "blpapi",
        SimpleNamespace(Event=SimpleNamespace(RESPONSE=1, PARTIAL_RESPONSE=2, REQUEST_STATUS=3)),
    )
    BloombergReturnProvider((mapping,), session=session).return_history(
        instrument, date(2024, 1, 1), date(2024, 1, 5)
    )

    assert session.request.values["adjustmentFollowDPDF"] is False
    assert session.request.values["adjustmentNormal"] is True
    assert session.request.values["adjustmentAbnormal"] is True
    assert session.request.values["adjustmentSplit"] is True


def test_bloomberg_fx_uses_px_last_through_the_shared_historical_adapter(monkeypatch):
    from datetime import UTC, datetime

    from trade_research.providers.factor_fx import BloombergFxProvider
    from trade_research.providers.factor_returns import BloombergReturnProvider

    seen = []

    def levels(self, mapping, start, end):
        seen.append((mapping.security, mapping.field, start, end))
        return [(date(2024, 1, 2), 1.09), (date(2024, 1, 3), 1.10)]

    monkeypatch.setattr(BloombergReturnProvider, "level_history", levels)
    series = BloombergFxProvider(BloombergReturnProvider(())).fx_history(
        "EUR", date(2024, 1, 2), date(2024, 1, 3), "USD"
    )

    assert series.source == "bloomberg"
    assert series.base_currency == "EUR"
    assert series.quote_currency == "USD"
    assert [point.value for point in series.points] == [1.09, 1.10]
    assert seen == [("EURUSD Curncy", "PX_LAST", date(2024, 1, 2), date(2024, 1, 3))]
    assert series.retrieved_at <= datetime.now(UTC)


@pytest.mark.parametrize("instrument_type", ["INDEX", "CURRENCY", None])
def test_yahoo_rejects_unverified_return_conventions(instrument_type):
    data = payload()
    data["chart"]["result"][0]["meta"]["instrumentType"] = instrument_type
    with pytest.raises(ProviderContractError):
        YahooReturnProvider(http_get=lambda *_: json.dumps(data)).return_history(
            InstrumentId(symbol="ACME", market="US"), date(2024, 1, 1), date(2024, 1, 10)
        )


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("scenario", ["partial", "timeout", "request_failure", "service_failure"])
def test_bloomberg_request_lifecycle(monkeypatch, owned, scenario):
    import sys
    from types import SimpleNamespace

    import trade_research.providers.factor_returns as adapters
    from trade_research.providers import ProviderConfigurationError

    instrument = InstrumentId(symbol="ACME", market="US")
    mapping = adapters.BloombergReturnMapping(
        instrument=instrument,
        security="ACME US Equity",
        field="TOTAL_RETURN_INDEX_GROSS_DVDS",
        currency="USD",
        return_basis="gross_total_return",
    )

    class Message(dict):
        def __init__(self, correlation, rows):
            super().__init__(securityData={"security": mapping.security, "fieldData": rows})
            self.correlation = correlation

        def correlationIds(self):
            return (self.correlation,)

    class Event(list):
        def __init__(self, kind, messages=()):
            super().__init__(messages)
            self.kind = kind

        def eventType(self):
            return self.kind

    class Request:
        def append(self, *_):
            pass

        def set(self, *_):
            pass

    class Session:
        def __init__(self):
            self.started = self.stopped = self.cancelled = False
            self.events = []
            self.correlation = object()

        def start(self):
            self.started = True
            return True

        def stop(self):
            self.stopped = True

        def cancel(self, correlation):
            assert correlation is self.correlation
            self.cancelled = True

        def openService(self, _):
            return scenario != "service_failure"

        def getService(self, _):
            return self

        def createRequest(self, _):
            return Request()

        def sendRequest(self, request, identity):
            self.events = [
                Event(1, [Message(object(), [{"date": "2024-01-01", mapping.field: 999}])]),
                Event(2, [Message(self.correlation, [{"date": "2024-01-02", mapping.field: 100}])]),
                Event(1, [Message(self.correlation, [{"date": "2024-01-03", mapping.field: 102}])]),
            ]
            if scenario == "request_failure":
                self.events = [Event(3, [Message(self.correlation, [])])]
            return self.correlation

        def nextEvent(self, timeout):
            assert timeout == 1000
            return self.events.pop(0)

    session = Session()
    options = SimpleNamespace(setServerHost=lambda _: None, setServerPort=lambda _: None)
    monkeypatch.setitem(
        sys.modules,
        "blpapi",
        SimpleNamespace(
            SessionOptions=lambda: options,
            Session=lambda _: session,
            Event=SimpleNamespace(RESPONSE=1, PARTIAL_RESPONSE=2, REQUEST_STATUS=3),
        ),
    )
    ticks = iter([0, 31] if scenario == "timeout" else range(10))
    monkeypatch.setattr(adapters.time, "monotonic", lambda: next(ticks))
    provider = adapters.BloombergReturnProvider((mapping,), session=None if owned else session)
    if scenario == "partial":
        series = provider.return_history(instrument, date(2024, 1, 1), date(2024, 1, 5))
        assert len(series.points) == 1
        assert series.points[0].value == pytest.approx(0.02)
        assert series.points[0].start_date == date(2024, 1, 2)
    else:
        with pytest.raises(ProviderConfigurationError):
            provider.return_history(instrument, date(2024, 1, 1), date(2024, 1, 5))
    assert session.started is owned
    assert session.stopped is owned
    assert session.cancelled is (not owned and scenario in {"timeout", "request_failure"})
