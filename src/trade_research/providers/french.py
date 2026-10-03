"""Bounded downloads of native Kenneth French five-factor and momentum data.

No research-framework dependency, credential handling, arbitrary URL or disk cache.
"""

from __future__ import annotations

import calendar
import csv
import hashlib
import io
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime

import httpx

from trade_research.domain.factors import FRENCH_DEFINITIONS
from trade_research.domain.models import (
    FactorFrequency,
    FactorRegion,
    ResearchFactorPanel,
    ResearchFactorPoint,
)
from trade_research.providers.contracts import ProviderConfigurationError, ProviderContractError

BASE = "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
FILES = {
    ("US", "daily"): (
        "F-F_Research_Data_5_Factors_2x3_daily_CSV.zip",
        "F-F_Momentum_Factor_daily_CSV.zip",
    ),
    ("US", "monthly"): ("F-F_Research_Data_5_Factors_2x3_CSV.zip", "F-F_Momentum_Factor_CSV.zip"),
    ("Europe", "daily"): ("Europe_5_Factors_Daily_CSV.zip", "Europe_Mom_Factor_Daily_CSV.zip"),
    ("Europe", "monthly"): ("Europe_5_Factors_CSV.zip", "Europe_Mom_Factor_CSV.zip"),
}
MAX_BYTES = 8 * 1024 * 1024


def download_zip(url: str) -> bytes:
    try:
        with httpx.stream("GET", url, timeout=30, follow_redirects=False) as response:
            response.raise_for_status()
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > MAX_BYTES:
                    raise ProviderContractError("French download exceeded byte limit")
        return bytes(body)
    except httpx.HTTPError as error:
        raise ProviderConfigurationError("Kenneth French download unavailable") from error


def unzip_csv(payload: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            entries = archive.infolist()
            if len(entries) != 1 or entries[0].file_size > MAX_BYTES:
                raise ValueError("unexpected archive size")
            return archive.read(entries[0]).decode("utf-8-sig")
    except (ValueError, UnicodeError, zipfile.BadZipFile, RuntimeError) as error:
        raise ProviderContractError("Invalid French archive") from error


def parse_french_csv(
    text: str,
    frequency: FactorFrequency,
    columns: tuple[str, ...],
) -> dict[date, tuple[float, ...]]:
    """Read the requested native frequency, reject schema drift and skip missing sentinels."""
    import math

    result: dict[date, tuple[float, ...]] = {}
    seen: set[date] = set()
    previous: date | None = None
    header: list[str] | None = None
    width = 8 if frequency == "daily" else 6
    for row in csv.reader(io.StringIO(text)):
        cells = [cell.strip() for cell in row]
        if not cells:
            continue
        if cells[0] == "" and set(columns).issubset(cells[1:]):
            header = cells
            continue
        key = cells[0]
        if not key.isdigit() or len(key) != width:
            continue
        if header is None or len(cells) != len(header):
            raise ValueError("French columns do not match the expected schema")
        year, month = int(key[:4]), int(key[4:6])
        day = date(year, month, int(key[6:]) if width == 8 else calendar.monthrange(year, month)[1])
        if day in seen or (seen and previous is not None and day < previous):
            raise ValueError("French dates must be unique and ordered")
        seen.add(day)
        previous = day
        values = tuple(float(cells[header.index(column)]) for column in columns)
        if not all(math.isfinite(value) for value in values):
            raise ValueError("French values must be finite")
        if any(value in {-99.99, -999.0} for value in values):
            continue
        result[day] = tuple(value / 100 for value in values)
    if header is None or not seen:
        raise ValueError("No native French observations found")
    return result


class FrenchFactorProvider:
    def __init__(self, download: Callable[[str], bytes] = download_zip) -> None:
        self._download = download

    def research_factors(
        self,
        region: FactorRegion,
        frequency: FactorFrequency,
        start: date,
        end: date,
    ) -> ResearchFactorPanel:
        names = FILES[(region, frequency)]
        payloads = [self._download(BASE + name) for name in names]
        try:
            factors = parse_french_csv(
                unzip_csv(payloads[0]), frequency, ("Mkt-RF", "SMB", "HML", "RMW", "CMA", "RF")
            )
            momentum = parse_french_csv(
                unzip_csv(payloads[1]), frequency, ("Mom" if region == "US" else "WML",)
            )
            points = tuple(
                ResearchFactorPoint(
                    date=day,
                    values=dict(
                        zip(
                            (d.id for d in FRENCH_DEFINITIONS),
                            (*factors[day][:5], momentum[day][0]),
                            strict=True,
                        )
                    ),
                    risk_free=factors[day][5],
                )
                for day in sorted(factors.keys() & momentum.keys())
                if start <= day <= end
            )
            return ResearchFactorPanel(
                region=region,
                currency="USD",
                calendar="US" if region == "US" else "weekdays",
                definitions=FRENCH_DEFINITIONS,
                frequency=frequency,
                source="kenneth_french",
                retrieved_at=datetime.now(UTC),
                reference="sha256:" + hashlib.sha256(b"\0".join(payloads)).hexdigest(),
                points=points,
            )
        except ValueError as error:
            raise ProviderContractError("Invalid French factor observations") from error


class InlineResearchFactorProvider:
    def __init__(self, panel: ResearchFactorPanel) -> None:
        self.panel = panel

    def research_factors(
        self,
        region: FactorRegion,
        frequency: FactorFrequency,
        start: date,
        end: date,
    ) -> ResearchFactorPanel:
        return self.panel
