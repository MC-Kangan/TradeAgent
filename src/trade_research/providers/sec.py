"""Bounded SEC EDGAR adapters: CIK resolution and Company Facts fundamentals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, cast

from trade_research.domain import Evidence, InstrumentId, MetricKind, Observation, VendorField
from trade_research.providers.contracts import (
    ProviderConfigurationError,
    ProviderContractError,
)
from trade_research.providers.remote import _bounded_payload, _http_get

HttpGet = Callable[[str, Mapping[str, str]], str]

_SEC_COMPANY_TICKERS_URL: Final = "https://www.sec.gov/files/company_tickers.json"
_SEC_COMPANY_FACTS_URL: Final = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_DEFAULT_CACHE_MAX_AGE: Final = timedelta(hours=24)


class CikResolver:
    """Resolve ticker symbols to SEC CIK codes with local caching."""

    def __init__(
        self,
        user_agent: str,
        cache_dir: Path | str,
        overrides: Mapping[str, str] | None = None,
        max_cache_age: timedelta = _DEFAULT_CACHE_MAX_AGE,
        http_get: HttpGet = _http_get,
    ) -> None:
        if not user_agent:
            raise ProviderConfigurationError(
                "SEC CIK resolver requires a configured identifying user agent"
            )
        self._user_agent = user_agent
        self._cache_dir = Path(cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path = self._cache_dir / "sec_cik_map.json"
        self._overrides = dict(overrides) if overrides is not None else {}
        self._max_cache_age = max_cache_age
        self._http_get = http_get
        self._mapping: dict[str, str] | None = None
        self._mapping_hash: str | None = None

    def resolve(self, symbol: str) -> str:
        """Return the zero-padded 10-digit CIK for a ticker symbol."""
        normalized = symbol.upper().strip()
        if normalized in self._overrides:
            return self._overrides[normalized]
        mapping, _ = self._load_mapping()
        try:
            return mapping[normalized]
        except KeyError as error:
            raise ProviderConfigurationError(
                f"no SEC CIK found for symbol '{normalized}'"
            ) from error

    @property
    def mapping_hash(self) -> str | None:
        """Return the SHA-256 hash of the current mapping, if loaded."""
        if self._mapping_hash is not None:
            return self._mapping_hash
        _, mapping_hash = self._load_mapping()
        return mapping_hash

    def _load_mapping(self) -> tuple[dict[str, str], str]:
        if self._mapping is not None and self._mapping_hash is not None:
            return self._mapping, self._mapping_hash
        if self._cache_path.is_file():
            try:
                cached = json.loads(self._cache_path.read_text(encoding="utf-8"))
                retrieved_at = datetime.fromisoformat(cached["retrieved_at"])
                age = datetime.now(tz=UTC) - retrieved_at
                if age <= self._max_cache_age:
                    self._mapping = {k: v for k, v in cached["mapping"].items()}
                    self._mapping_hash = cached["content_hash"]
                    return self._mapping, self._mapping_hash
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        return self._download_mapping()

    def _download_mapping(self) -> tuple[dict[str, str], str]:
        text = self._http_get(
            _SEC_COMPANY_TICKERS_URL,
            {"Accept": "application/json", "User-Agent": self._user_agent},
        )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as error:
            raise ProviderContractError("SEC ticker mapping is not valid JSON") from error
        if not isinstance(payload, dict):
            raise ProviderContractError("SEC ticker mapping is malformed")
        mapping: dict[str, str] = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            cik = entry.get("cik_str")
            ticker = entry.get("ticker")
            if not isinstance(cik, int) or not isinstance(ticker, str):
                continue
            mapping[str(ticker).upper().strip()] = str(int(cik)).zfill(10)
        for ticker, cik in self._overrides.items():
            mapping[ticker.upper().strip()] = cik
        content_hash = f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"
        retrieved_at = datetime.now(tz=UTC).isoformat()
        self._cache_path.write_text(
            json.dumps(
                {
                    "retrieved_at": retrieved_at,
                    "content_hash": content_hash,
                    "mapping": mapping,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._mapping = mapping
        self._mapping_hash = content_hash
        return mapping, content_hash


class SecFilingsProvider:
    """SEC EDGAR filings adapter using a CikResolver for automatic CIK lookup."""

    def __init__(
        self,
        resolver: CikResolver,
        user_agent: str,
        http_get: HttpGet = _http_get,
    ) -> None:
        if not user_agent:
            raise ProviderConfigurationError(
                "SEC provider requires a configured identifying user agent"
            )
        self._resolver = resolver
        self._user_agent = user_agent
        self._http_get = http_get

    def filings(self, instrument: InstrumentId) -> tuple[Evidence, ...]:
        from trade_research.providers.contracts import MAX_FILING_ROWS

        cik = self._resolver.resolve(instrument.symbol)
        text = _bounded_payload(
            self._http_get(
                f"https://data.sec.gov/submissions/CIK{cik}.json",
                {"Accept": "application/json", "User-Agent": self._user_agent},
            )
        )
        try:
            payload = json.loads(text)
            recent = payload["filings"]["recent"]
            forms = recent["form"]
            dates = recent["filingDate"]
            accessions = recent["accessionNumber"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ProviderContractError("SEC returned a malformed response") from error
        if not all(isinstance(values, list) for values in (forms, dates, accessions)):
            raise ProviderContractError("SEC returned malformed filing metadata")
        count = min(len(forms), len(dates), len(accessions))
        if count > MAX_FILING_ROWS:
            count = MAX_FILING_ROWS
        now = datetime.now(tz=UTC)
        evidence: list[Evidence] = []
        for index in range(count):
            form = str(forms[index])[:16]
            filing_date = str(dates[index])[:10]
            accession = str(accessions[index])
            reference = f"sha256:{hashlib.sha256(accession.encode()).hexdigest()}"
            normalized = json.dumps(
                {
                    "filing_date": filing_date,
                    "form": form,
                    "reference": reference,
                    "accession_number": accession,
                    "is_amendment": "/A" in str(form),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            evidence.append(Evidence(source="sec", content=normalized, collected_at=now))
        return tuple(evidence)


_SEC_CONCEPT_MAP: Final[dict[str, tuple[str, str]]] = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": ("revenue", "REVENUE"),
    "Revenues": ("revenue", "REVENUE"),
    "NetIncomeLoss": ("net_income", "NET_INCOME"),
    "OperatingIncomeLoss": ("operating_income", "OPERATING_INCOME"),
    "StockholdersEquity": ("shareholders_equity", "SHAREHOLDERS_EQUITY"),
    "LongTermDebt": ("total_debt", "TOTAL_DEBT"),
    "ShortTermBorrowings": ("total_debt", "TOTAL_DEBT"),
    "NetCashProvidedByUsedInOperatingActivities": ("free_cash_flow", "FREE_CASH_FLOW"),
    "PaymentsToAcquirePropertyPlantAndEquipment": ("free_cash_flow", "FREE_CASH_FLOW"),
    "GrossProfit": ("gross_profit", "GROSS_PROFIT"),
}

_SEC_OPERATING_CASH_FLOW = "NetCashProvidedByUsedInOperatingActivities"
_SEC_CAPEX = "PaymentsToAcquirePropertyPlantAndEquipment"


class SecCompanyFactsProvider:
    """SEC Company Facts XBRL adapter returning fundamental Observation objects.

    Implements the FundamentalProvider protocol so it can be registered
    under CapabilityName.FUNDAMENTALS in the ProviderRegistry.
    """

    def __init__(
        self,
        resolver: CikResolver,
        user_agent: str,
        http_get: HttpGet = _http_get,
    ) -> None:
        if not user_agent:
            raise ProviderConfigurationError(
                "SEC Company Facts provider requires a configured identifying user agent"
            )
        self._resolver = resolver
        self._user_agent = user_agent
        self._http_get = http_get

    def fundamentals(self, instrument: InstrumentId) -> tuple[Observation, ...]:
        cik = self._resolver.resolve(instrument.symbol)
        url = _SEC_COMPANY_FACTS_URL.format(cik=cik)
        text = _bounded_payload(
            self._http_get(
                url,
                {"Accept": "application/json", "User-Agent": self._user_agent},
            )
        )
        try:
            payload = json.loads(text)
            facts = payload["facts"]["us-gaap"]
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ProviderContractError(
                "SEC Company Facts returned a malformed response"
            ) from error
        if not isinstance(facts, dict):
            raise ProviderContractError("SEC Company Facts us-gaap section is malformed")

        entity_name = payload.get("entityName", "")
        snapshot_text = json.dumps({"cik": cik, "entity": entity_name}, sort_keys=True)
        snapshot_ref = f"sha256:{hashlib.sha256(snapshot_text.encode()).hexdigest()}"
        now = datetime.now(tz=UTC)

        observations: list[Observation] = []
        fcf_operating: dict[str, float] = {}
        fcf_capex: dict[str, float] = {}

        for concept, (metric_name, vendor_field) in _SEC_CONCEPT_MAP.items():
            concept_data = facts.get(concept)
            if not isinstance(concept_data, dict):
                continue
            units = concept_data.get("units")
            if not isinstance(units, dict):
                continue
            usd_data = units.get("USD")
            if not isinstance(usd_data, list) or not usd_data:
                continue

            current, prior = _select_periods(usd_data)
            if current is None:
                continue

            if _is_fcf_capex_collector(concept):
                frame = _frame_key(current)
                if frame:
                    fcf_capex[frame] = float(cast("int | float", current["val"]))
                continue

            current_obs = _build_statement_observation(
                instrument=instrument,
                metric_name=metric_name,
                vendor_field=vendor_field,
                concept=concept,
                data=current,
                snapshot_ref=snapshot_ref,
                observed_at=now,
                period_role="current",
            )
            if current_obs is not None:
                observations.append(current_obs)

            if concept == _SEC_OPERATING_CASH_FLOW:
                frame = _frame_key(current)
                if frame:
                    fcf_operating[frame] = float(cast("int | float", current["val"]))

            if prior is not None:
                prior_obs = _build_statement_observation(
                    instrument=instrument,
                    metric_name=metric_name,
                    vendor_field=vendor_field,
                    concept=concept,
                    data=prior,
                    snapshot_ref=snapshot_ref,
                    observed_at=now,
                    period_role="prior",
                )
                if prior_obs is not None:
                    observations.append(prior_obs)

        _build_free_cash_flow(
            observations,
            instrument,
            snapshot_ref,
            now,
            fcf_operating,
            fcf_capex,
        )

        return tuple(observations)


def _is_fcf_capex_collector(concept: str) -> bool:
    return concept == _SEC_CAPEX


def _frame_key(data: dict[str, object]) -> str | None:
    frame = data.get("frame")
    if isinstance(frame, str) and frame:
        return frame
    fy = data.get("fy")
    fp = data.get("fp")
    if isinstance(fy, int) and isinstance(fp, str):
        return f"FY{fy}{fp}"
    return None


def _select_periods(
    usd_data: list[dict[str, object]],
) -> tuple[dict[str, object] | None, dict[str, object] | None]:
    """Select current (most recent annual) and prior-year periods from SEC data."""

    annual = [
        d
        for d in usd_data
        if isinstance(d.get("fp"), str)
        and d.get("fp") in ("FY", "Q4")
        and isinstance(d.get("fy"), int)
        and isinstance(d.get("val"), int | float)
        and isinstance(d.get("form"), str)
    ]
    if not annual:
        return None, None

    annual.sort(
        key=lambda d: (int(cast("int", d["fy"])), str(d.get("filed", ""))),
        reverse=True,
    )
    current = annual[0]
    current_fy = int(cast("int", current["fy"]))
    prior = next(
        (
            d
            for d in annual
            if int(cast("int", d["fy"])) == current_fy - 1
            and d.get("fp") == current.get("fp")
        ),
        None,
    )
    return current, prior


def _build_statement_observation(
    *,
    instrument: InstrumentId,
    metric_name: str,
    vendor_field: str,
    concept: str,
    data: dict[str, object],
    snapshot_ref: str,
    observed_at: datetime,
    period_role: str,
) -> Observation | None:
    val = data.get("val")
    if not isinstance(val, int | float) or isinstance(val, bool):
        return None
    fy = data.get("fy")
    fp = data.get("fp")
    end_date = data.get("end")

    if not isinstance(fy, int) or not isinstance(fp, str):
        return None

    period_end = str(end_date)[:10] if isinstance(end_date, str) else ""
    period_type = "annual" if fp in ("FY", "Q4") else "quarterly"
    period_ref = (
        "sha256:"
        + hashlib.sha256(
            f"{concept}:{fy}:{fp}:{period_end}".encode()
        ).hexdigest()
    )
    accession = str(data.get("accn", "")) if data.get("accn") else ""
    reference = (
        f"sha256:{hashlib.sha256(f'{concept}:{fy}:{fp}:{accession}'.encode()).hexdigest()}"
        if accession
        else period_ref
    )

    try:
        metric = MetricKind(metric_name)
    except ValueError:
        return None
    try:
        vf = VendorField(vendor_field)
    except ValueError:
        return None

    provenance: dict[str, object] = {
        "provider_kind": "sec",
        "vendor_field": vf.value,
        "snapshot_ref": snapshot_ref,
        "period_role": period_role,
        "period_end": period_end,
        "period_type": period_type,
        "period_ref": period_ref,
        "currency": "USD",
        "reference": reference,
    }

    return Observation(
        instrument=instrument,
        metric=metric,
        value=float(val),
        source="sec",
        observed_at=observed_at,
        provenance=provenance,
    )


def _build_free_cash_flow(
    observations: list[Observation],
    instrument: InstrumentId,
    snapshot_ref: str,
    observed_at: datetime,
    fcf_operating: dict[str, float],
    fcf_capex: dict[str, float],
) -> None:
    """Derive free cash flow from operating cash flow minus capex for matching periods."""

    for frame, op_value in fcf_operating.items():
        capex_value = fcf_capex.get(frame)
        if capex_value is None:
            continue
        fcf = op_value - abs(capex_value)
        period_ref_val = f"sha256:{hashlib.sha256(f'fcf:{frame}'.encode()).hexdigest()}"
        try:
            metric = MetricKind("free_cash_flow")
        except ValueError:
            continue
        try:
            vf = VendorField("FREE_CASH_FLOW")
        except ValueError:
            continue
        observations.append(
            Observation(
                instrument=instrument,
                metric=metric,
                value=round(fcf, 2),
                source="sec",
                observed_at=observed_at,
                provenance={
                    "provider_kind": "sec",
                    "vendor_field": vf.value,
                    "snapshot_ref": snapshot_ref,
                    "period_role": "current",
                    "period_end": "",
                    "period_type": "annual",
                    "period_ref": period_ref_val,
                    "currency": "USD",
                    "reference": period_ref_val,
                },
            )
        )
