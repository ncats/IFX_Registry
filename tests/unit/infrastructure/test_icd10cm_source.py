"""CDC ICD-10-CM release selection and tabular ZIP validation."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from ifx_registry import FetchRequest, SourceValidationError, VersionProbeRequest
from ifx_registry.domain.models import SourceVersion
from ifx_registry.infrastructure.http import (
    DownloadedResource,
    HttpGateway,
    HttpMetadata,
    HttpText,
)
from ifx_registry.infrastructure.sources.icd10cm import (
    CDC_BASE,
    CdcIcd10CmSource,
    annual_zip_url,
    latest_fiscal_year,
)


class Gateway(HttpGateway):
    def __init__(self, payload: Path):
        self.payload = payload
        self.root = '<a href="/pub/ICD10CM/2026/">2026</a><a href="/pub/ICD10CM/2027/">2027</a>'
        self.listing = (
            '<a href="/pub/Health_Statistics/NCHS/Publications/ICD10CM/2027/'
            'icd10cm-table-and-index-2027.zip">ZIP</a>'
        )

    def get_text(self, url: str, *, timeout: float) -> HttpText:
        assert url in {CDC_BASE, f"{CDC_BASE}2027/"}
        return HttpText(self.root if url == CDC_BASE else self.listing, HttpMetadata(url, {}))

    def head(self, url: str, *, timeout: float) -> HttpMetadata:
        assert url.endswith("icd10cm-table-and-index-2027.zip")
        return HttpMetadata(url, {"ETag": '"test"'})

    def download(self, url: str, destination: Path, *, timeout: float) -> DownloadedResource:
        destination.write_bytes(self.payload.read_bytes())
        return DownloadedResource(destination, self.head(url, timeout=timeout))


def _zip(path: Path, *, year: str = "2027", count: int = 40_000) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "icd10cm-table-and-index-2027/icd10cm-tabular_-2027.xml",
            "<ICD10CM.tabular><version>" + year + "</version>" + "<diag/>" * count
            + "</ICD10CM.tabular>",
        )


def test_icd10cm_discovers_newest_year_and_validates_tabular_xml(tmp_path: Path) -> None:
    payload = tmp_path / "source.zip"
    _zip(payload)
    source = CdcIcd10CmSource(Gateway(payload))
    assert source.discover_latest(VersionProbeRequest()).value == "2027"

    snapshot = source.fetch(FetchRequest(tmp_path / "output", SourceVersion("2027")))
    assert snapshot.snapshot_id == "cdc:icd10cm:2027"
    assert snapshot.files[0].relative_path.as_posix() == "icd10cm-table-and-index.zip"
    assert snapshot.metadata["diagnosis_codes"] == 40_000


def test_icd10cm_rejects_same_year_update_and_wrong_xml_year(tmp_path: Path) -> None:
    with pytest.raises(SourceValidationError, match="separate Registry version"):
        latest_fiscal_year(
            '<a href="/pub/2027/">2027</a><a href="/pub/2027-update/">update</a>'
        )
    payload = tmp_path / "wrong.zip"
    _zip(payload, year="2026")
    with pytest.raises(SourceValidationError, match="declares '2026'"):
        CdcIcd10CmSource(Gateway(payload)).fetch(
            FetchRequest(tmp_path / "output", SourceVersion("2027"))
        )


def test_icd10cm_accepts_cdc_legacy_zip_name() -> None:
    listing = (
        '<a href="/pub/Health_Statistics/NCHS/Publications/ICD10CM/2026/'
        'icd10cm-table%20and%20index-2026.zip">ZIP</a>'
    )
    assert annual_zip_url(listing, 2026).endswith(
        "/2026/icd10cm-table%20and%20index-2026.zip"
    )
