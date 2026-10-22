"""CDC ICD-10-CM annual tabular ZIP, versioned by fiscal year."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import (
    DownloadedSourceFile,
    HttpFileSpec,
    HttpSnapshotSource,
    SourceValidationResult,
    require_download,
)
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy

CDC_BASE = "https://ftp.cdc.gov/pub/Health_Statistics/NCHS/Publications/ICD10CM/"
ICD10CM_MEMBER = "icd10cm-table-and-index.zip"


def latest_fiscal_year(text: str) -> int:
    years = {
        int(value)
        for value in re.findall(
            r'href="[^"]*/(20\d{2})/"', text, flags=re.IGNORECASE
        )
    }
    if not years:
        raise SourceValidationError("CDC ICD-10-CM index has no fiscal-year directories")
    year = max(years)
    if re.search(rf'href="[^"]*/{year}-update/"', text, flags=re.IGNORECASE):
        raise SourceValidationError(
            f"CDC has a {year} ICD-10-CM update; its separate Registry "
            "version needs an explicit naming decision"
        )
    return year


def annual_zip_url(text: str, year: int) -> str:
    matches = []
    for path in re.findall(r'href="([^"]+\.zip)"', text, flags=re.IGNORECASE):
        filename = unquote(PurePosixPath(path).name)
        if re.fullmatch(
            rf"icd10cm-table[ -]and[ -]index-{year}\.zip",
            filename,
            flags=re.IGNORECASE,
        ):
            matches.append(urljoin(CDC_BASE, path))
    if len(matches) != 1:
        raise SourceValidationError(
            f"CDC ICD-10-CM {year} directory must contain one annual table-and-index ZIP; "
            f"found {len(matches)}"
        )
    return matches[0]


class CdcIcd10CmVersionStrategy(SourceVersionStrategy):
    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return (CDC_BASE,)

    @property
    def description(self) -> str:
        return "Selects CDC's newest fiscal-year ICD-10-CM table-and-index ZIP."

    def discover(self, http: HttpGateway, request: VersionProbeRequest) -> SourceVersion:
        root = http.get_text(CDC_BASE, timeout=request.timeout.total_seconds())
        year = latest_fiscal_year(root.text)
        year_url = f"{CDC_BASE}{year}/"
        listing = http.get_text(year_url, timeout=request.timeout.total_seconds())
        file_url = annual_zip_url(listing.text, year)
        metadata = http.head(file_url, timeout=request.timeout.total_seconds())
        return SourceVersion(
            str(year),
            evidence={
                "method": "cdc_fiscal_year_directory",
                "listing_url": root.metadata.final_url,
                "year_listing_url": listing.metadata.final_url,
                "file_url": file_url,
                "last_modified": metadata.header("Last-Modified"),
                "etag": metadata.header("ETag"),
                "content_length": metadata.header("Content-Length"),
            },
        )


class CdcIcd10CmSource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway):
        super().__init__(http)
        self._strategy = CdcIcd10CmVersionStrategy()

    @property
    def dataset(self) -> DatasetId:
        return DatasetId("cdc", "icd10cm")

    @property
    def homepage(self) -> str:
        return "https://www.cdc.gov/nchs/icd/icd-10-cm/files.html"

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return ()

    @property
    def expected_file_count(self) -> int:
        return 1

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return (CDC_BASE,)

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        url = version.evidence.get("file_url")
        if not isinstance(url, str) or not url.startswith(CDC_BASE):
            raise SourceValidationError("CDC ICD-10-CM version lacks a verified annual ZIP URL")
        return (HttpFileSpec(url, ICD10CM_MEMBER),)

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._strategy

    def validate_downloads(
        self, version: SourceVersion, downloads: tuple[DownloadedSourceFile, ...]
    ) -> SourceValidationResult:
        resource = require_download(downloads, ICD10CM_MEMBER).resource
        for header, key in (
            ("Last-Modified", "last_modified"),
            ("ETag", "etag"),
            ("Content-Length", "content_length"),
        ):
            expected = version.evidence.get(key)
            if expected and resource.metadata.header(header) != expected:
                raise SourceValidationError(f"CDC ICD-10-CM {header} changed during download")
        try:
            with zipfile.ZipFile(resource.path) as archive:
                bad_member = archive.testzip()
                if bad_member is not None:
                    raise SourceValidationError(f"CDC ICD-10-CM ZIP has corrupt member {bad_member}")
                tabular = [
                    name for name in archive.namelist()
                    if re.search(r"icd10cm-tabular.*\.xml$", name, flags=re.IGNORECASE)
                ]
                if len(tabular) != 1:
                    raise SourceValidationError(
                        f"CDC ICD-10-CM ZIP has {len(tabular)} tabular XML files"
                    )
                codes = 0
                declared_year = None
                with archive.open(tabular[0]) as handle:
                    for _, element in ET.iterparse(handle, events=("end",)):
                        if element.tag == "version" and declared_year is None:
                            declared_year = (element.text or "").strip()
                        if element.tag == "diag":
                            codes += 1
                        element.clear()
        except (OSError, zipfile.BadZipFile, ET.ParseError) as error:
            raise SourceValidationError(f"Could not read CDC ICD-10-CM ZIP: {error}") from error
        if declared_year != version.value:
            raise SourceValidationError(
                f"CDC ICD-10-CM tabular XML declares {declared_year!r}, "
                f"expected {version.value}"
            )
        if codes < 40_000:
            raise SourceValidationError(f"CDC ICD-10-CM tabular XML has too few codes: {codes}")
        return SourceValidationResult(
            version, metadata={"tabular_xml": tabular[0], "diagnosis_codes": codes}
        )

    def validate_confirmation(
        self, version: SourceVersion, confirmed_version: SourceVersion
    ) -> None:
        super().validate_confirmation(version, confirmed_version)
        for key in ("file_url", "last_modified", "etag", "content_length"):
            if version.evidence.get(key) != confirmed_version.evidence.get(key):
                raise SourceValidationError(f"CDC ICD-10-CM {key} changed during acquisition")
