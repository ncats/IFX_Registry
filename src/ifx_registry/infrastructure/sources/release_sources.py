"""HTTP sources whose releases are declared by listings or release metadata."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from email.utils import parsedate_to_datetime

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import HttpFileSpec, HttpSnapshotSource
from ifx_registry.infrastructure.sources.version_strategies import (
    FixedVersionStrategy,
    ParsedTextVersionStrategy,
    SourceVersionStrategy,
)

FilePlanner = Callable[[SourceVersion], tuple[HttpFileSpec, ...]]


@dataclass(frozen=True, slots=True)
class ReleaseSourceDefinition:
    dataset: DatasetId
    homepage: str
    strategy: SourceVersionStrategy
    files_for_version: FilePlanner
    expected_file_count: int
    declared_upstream_urls: tuple[str, ...]


class ReleaseHttpSource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway, definition: ReleaseSourceDefinition):
        super().__init__(http)
        self._definition = definition

    @property
    def dataset(self) -> DatasetId:
        return self._definition.dataset

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return ()

    def file_specs_for(self, version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        return self._definition.files_for_version(version)

    @property
    def expected_file_count(self) -> int:
        return self._definition.expected_file_count

    @property
    def upstream_urls(self) -> tuple[str, ...]:
        return self._definition.declared_upstream_urls

    @property
    def homepage(self) -> str:
        return self._definition.homepage

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._definition.strategy


@dataclass(frozen=True, slots=True)
class ParsedReleaseWithFileDates(SourceVersionStrategy):
    """Combine a release parsed from text with dates from its resolved files."""

    text_strategy: ParsedTextVersionStrategy
    files_for_version: FilePlanner

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return self.text_strategy.evidence_urls

    @property
    def description(self) -> str:
        return self.text_strategy.description

    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        release = self.text_strategy.discover(http, request)
        file_evidence: list[dict[str, str]] = []
        dates: list[date] = []
        for file in self.files_for_version(release):
            metadata = http.head(file.url, timeout=request.timeout.total_seconds())
            raw_date = metadata.header("Last-Modified")
            if raw_date:
                dates.append(_http_date(raw_date))
                file_evidence.append({"url": metadata.final_url, "last_modified": raw_date})
        return SourceVersion(
            value=release.value,
            version_date=max(dates) if dates else None,
            discovered_at=release.discovered_at,
            evidence={**release.evidence, "files": file_evidence},
        )


def _http_date(value: str) -> date:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as error:
        raise SourceValidationError(f"Could not parse Last-Modified {value!r}") from error
    if parsed is None:
        raise SourceValidationError(f"Could not parse Last-Modified {value!r}")
    return parsed.date()


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def _latest_regex_version(text: str, pattern: str, label: str) -> SourceVersion:
    matches = {_version_tuple(value) for value in re.findall(pattern, text, re.IGNORECASE)}
    if not matches:
        raise SourceValidationError(f"Could not find the latest {label} release")
    return SourceVersion(".".join(str(part) for part in max(matches)))


def _pathwaycommons_version(text: str) -> SourceVersion:
    match = re.search(r"PC version (\d+) (\d+ \w+ \d+)", text)
    if not match:
        raise SourceValidationError("Could not parse Pathway Commons release metadata")
    version_date = datetime.strptime(match.group(2), "%d %b %Y").date()
    return SourceVersion(
        match.group(1),
        version_date=version_date,
        evidence={"matched_text": match.group(0)},
    )


def _rhea_version(text: str) -> SourceVersion:
    values = dict(
        line.split("=", 1)
        for raw in text.splitlines()
        if (line := raw.strip()) and not line.startswith("#") and "=" in line
    )
    version = values.get("rhea.release.number")
    raw_date = values.get("rhea.release.date")
    if not version or not raw_date:
        raise SourceValidationError("Could not parse the Rhea release properties")
    return SourceVersion(
        version.strip(),
        version_date=date.fromisoformat(raw_date.strip()),
        evidence={"rhea_release_date": raw_date.strip()},
    )


def _dated_listing_version(text: str, pattern: str, label: str) -> SourceVersion:
    match = re.search(pattern, text)
    if not match:
        raise SourceValidationError(f"Could not find the current {label} file")
    year, month, day = match.groups()[-3:]
    release_date = date(int(year), int(month), int(day))
    return SourceVersion(
        release_date.isoformat(),
        version_date=release_date,
        evidence={"file_name": match.group(0)},
    )


def _pfocr_version(text: str) -> SourceVersion:
    gene = re.search(r"pfocr-(\d{4})(\d{2})(\d{2})-gmt-Homo_sapiens\.gmt", text)
    chemical = re.search(
        r"pfocr-(\d{4})(\d{2})(\d{2})-chemical-gmt-Homo_sapiens\.gmt",
        text,
    )
    if gene is None or chemical is None or gene.groups() != chemical.groups():
        raise SourceValidationError(
            "Could not find matching current PFOCR gene and chemical releases"
        )
    release_date = date(*(int(part) for part in gene.groups()))
    return SourceVersion(
        release_date.isoformat(),
        version_date=release_date,
        evidence={
            "gene_file_name": gene.group(0),
            "chemical_file_name": chemical.group(0),
        },
    )


def _hpa_version(text: str) -> SourceVersion:
    match = re.search(r"version ([\d.]+)", text, re.IGNORECASE)
    if match is None:
        raise SourceValidationError("Could not find the Human Protein Atlas version")
    return SourceVersion(match.group(1).rstrip("."))


def _latest_directory_version(text: str, pattern: str, label: str) -> SourceVersion:
    versions = sorted(set(re.findall(pattern, text)))
    if not versions:
        raise SourceValidationError(f"Could not find a dated {label} release directory")
    value = versions[-1]
    compact_date = value if "-" not in value else value.replace("-", "")
    version_date = datetime.strptime(compact_date, "%Y%m%d").date()
    return SourceVersion(value, version_date=version_date)


def _static_files(*files: HttpFileSpec) -> FilePlanner:
    def plan(version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        del version
        return files

    return plan


def _text_strategy(
    url: str,
    parser: Callable[[str], SourceVersion],
    method: str,
    description: str,
) -> ParsedTextVersionStrategy:
    return ParsedTextVersionStrategy(url, parser, method, description)


BIOPLEX_URL = "https://bioplex.hms.harvard.edu/"
BIOPLEX_FILES = (
    HttpFileSpec(
        "https://bioplex.hms.harvard.edu/data/BioPlex_293T_Network_10K_Dec_2019.tsv",
        "BioPlex_293T_Network_10K_Dec_2019.tsv",
    ),
    HttpFileSpec(
        "https://bioplex.hms.harvard.edu/data/BioPlex_HCT116_Network_5.5K_Dec_2019.tsv",
        "BioPlex_HCT116_Network_5.5K_Dec_2019.tsv",
    ),
)

STRING_INDEX = "https://stringdb-downloads.org/download/"


def _string_files(version: SourceVersion) -> tuple[HttpFileSpec, ...]:
    name = f"9606.protein.links.v{version.value}.txt.gz"
    return (HttpFileSpec(f"{STRING_INDEX}protein.links.v{version.value}/{name}", name),)


PANTHER_INDEX = (
    "https://data.pantherdb.org/ftp/sequence_classifications/current_release/"
    "PANTHER_Sequence_Classification_files/"
)


def _panther_files(version: SourceVersion) -> tuple[HttpFileSpec, ...]:
    value = version.value
    return (
        HttpFileSpec(
            f"https://data.pantherdb.org/PANTHER{value}/ontology/Protein_Class_{value}",
            f"Protein_Class_{value}",
        ),
        HttpFileSpec(
            f"https://data.pantherdb.org/PANTHER{value}/ontology/Protein_class_relationship",
            "Protein_class_relationship",
        ),
        HttpFileSpec(f"{PANTHER_INDEX}PTHR{value}_human", f"PTHR{value}_human"),
    )


WIKIPATHWAYS_GMT_INDEX = "https://data.wikipathways.org/current/gmt/"
WIKIPATHWAYS_RDF_INDEX = "https://data.wikipathways.org/current/rdf/"
PFOCR_INDEX = "https://data.wikipathways.org/pfocr/current/"


def _listed_file(
    base_url: str,
    evidence_key: str,
    stable_name: str,
) -> FilePlanner:
    def plan(version: SourceVersion) -> tuple[HttpFileSpec, ...]:
        file_name = str(version.evidence[evidence_key])
        return (HttpFileSpec(f"{base_url}{file_name}", stable_name),)

    return plan


def _pfocr_files(version: SourceVersion) -> tuple[HttpFileSpec, ...]:
    return (
        HttpFileSpec(
            f"{PFOCR_INDEX}{version.evidence['gene_file_name']}",
            "pfocr_gene_gmt.gmt",
        ),
        HttpFileSpec(
            f"{PFOCR_INDEX}{version.evidence['chemical_file_name']}",
            "pfocr_chemical_gmt.gmt",
        ),
    )


PATHWAYCOMMONS_METADATA = "https://download.baderlab.org/PathwayCommons/PC2/v14/datasources.txt"
PATHWAYCOMMONS_FILES = _static_files(
    HttpFileSpec(
        "https://download.baderlab.org/PathwayCommons/PC2/v14/pc-hgnc.gmt.gz",
        "pc-hgnc.gmt.gz",
    )
)
RHEA_PROPERTIES = "https://ftp.expasy.org/databases/rhea/rhea-release.properties"
RHEA_FILES = _static_files(
    HttpFileSpec(RHEA_PROPERTIES, "rhea-release.properties"),
    HttpFileSpec("https://ftp.expasy.org/databases/rhea/rdf/rhea.rdf.gz", "rhea.rdf.gz"),
    HttpFileSpec(
        "https://ftp.expasy.org/databases/rhea/tsv/rhea2uniprot_sprot.tsv",
        "rhea2uniprot_sprot.tsv",
    ),
    HttpFileSpec(
        "https://ftp.expasy.org/databases/rhea/tsv/rhea2uniprot_trembl.tsv.gz",
        "rhea2uniprot_trembl.tsv.gz",
    ),
    HttpFileSpec("https://ftp.expasy.org/databases/rhea/tsv/rhea2ec.tsv", "rhea2ec.tsv"),
    HttpFileSpec(
        "https://ftp.expasy.org/databases/rhea/tsv/rhea-directions.tsv",
        "rhea-directions.tsv",
    ),
)

GTEX_FILES = _static_files(
    HttpFileSpec(
        "https://storage.googleapis.com/adult-gtex/bulk-gex/v11/rna-seq/"
        "GTEx_Analysis_2025-08-22_v11_RNASeQCv2.4.3_gene_tpm.gct.gz",
        "GTEx_Analysis_2025_08_22_v11_RNASeQCv2.4.3_gene_tpm.gct.gz",
    ),
    HttpFileSpec(
        "https://storage.googleapis.com/adult-gtex/annotations/v11/metadata-files/"
        "GTEx_Analysis_v11_Annotations_SampleAttributesDS.txt",
        "GTEx_Analysis_v11_Annotations_SampleAttributesDS.txt",
    ),
    HttpFileSpec(
        "https://storage.googleapis.com/adult-gtex/annotations/v11/metadata-files/"
        "GTEx_Analysis_v11_Annotations_SubjectPhenotypesDS.txt",
        "GTEx_Analysis_v11_Annotations_SubjectPhenotypesDS.txt",
    ),
)
HPA_ABOUT = "https://www.proteinatlas.org/about/download"
HPA_FILES = _static_files(
    HttpFileSpec(
        "https://www.proteinatlas.org/download/tsv/normal_ihc_data.tsv.zip",
        "normal_ihc_data.tsv.zip",
    ),
    HttpFileSpec(
        "https://www.proteinatlas.org/download/tsv/rna_tissue_hpa.tsv.zip",
        "rna_tissue_hpa.tsv.zip",
    ),
)
TIGA_INDEX = "https://unmtid-dbs.net/download/TIGA/"
TIGA_FILES = _static_files(
    HttpFileSpec(
        "https://unmtid-dbs.net/download/TIGA/latest/tiga_gene-trait_stats.tsv",
        "tiga_gene-trait_stats.tsv",
    ),
    HttpFileSpec(
        "https://unmtid-dbs.net/download/TIGA/latest/tiga_gene-trait_provenance.tsv",
        "tiga_gene-trait_provenance.tsv",
    ),
)
SURECHEMBL_INDEX = "https://ftp.ebi.ac.uk/pub/databases/chembl/SureChEMBL/bulk_data/"
SURECHEMBL_NAMES = (
    "patents.parquet",
    "biomedical_entities.parquet",
    "biomedical_locations.parquet",
    "biomedical_types.parquet",
    "fields.parquet",
)


def _surechembl_files(version: SourceVersion) -> tuple[HttpFileSpec, ...]:
    return tuple(
        HttpFileSpec(f"{SURECHEMBL_INDEX}{version.value}/{name}", name) for name in SURECHEMBL_NAMES
    )


def _with_dates(
    strategy: ParsedTextVersionStrategy,
    files: FilePlanner,
) -> ParsedReleaseWithFileDates:
    return ParsedReleaseWithFileDates(strategy, files)


BIOPLEX_PLAN = _static_files(*BIOPLEX_FILES)
BIOPLEX_STRATEGY = _with_dates(
    _text_strategy(
        BIOPLEX_URL,
        lambda text: _latest_regex_version(text, r"BioPlex\s+(\d+(?:\.\d+)+)", "BioPlex"),
        "bioplex_homepage_release",
        "Reads the newest BioPlex release number from the source website.",
    ),
    BIOPLEX_PLAN,
)
STRING_STRATEGY = _with_dates(
    _text_strategy(
        STRING_INDEX,
        lambda text: _latest_regex_version(text, r"protein\.links\.v(\d+(?:\.\d+)*)/", "STRING"),
        "string_download_listing",
        "Reads the newest protein-links release from the STRING download listing.",
    ),
    _string_files,
)
PANTHER_STRATEGY = _with_dates(
    _text_strategy(
        PANTHER_INDEX,
        lambda text: _latest_regex_version(text, r"PTHR(\d+(?:\.\d+)*)_human", "PANTHER"),
        "panther_release_listing",
        "Reads the newest human PANTHER release from the current-release listing.",
    ),
    _panther_files,
)

RELEASE_SOURCES: dict[str, ReleaseSourceDefinition] = {
    "bioplex_ppi": ReleaseSourceDefinition(
        DatasetId("bioplex", "ppi"),
        BIOPLEX_URL,
        BIOPLEX_STRATEGY,
        BIOPLEX_PLAN,
        2,
        (BIOPLEX_URL, *(file.url for file in BIOPLEX_FILES)),
    ),
    "string_protein_links_human": ReleaseSourceDefinition(
        DatasetId("string", "protein_links_human"),
        "https://string-db.org/",
        STRING_STRATEGY,
        _string_files,
        1,
        (STRING_INDEX,),
    ),
    "panther_protein_classes": ReleaseSourceDefinition(
        DatasetId("panther", "protein_classes"),
        "https://pantherdb.org/",
        PANTHER_STRATEGY,
        _panther_files,
        3,
        (PANTHER_INDEX,),
    ),
    "pathwaycommons_pc_hgnc": ReleaseSourceDefinition(
        DatasetId("pathwaycommons", "pc_hgnc"),
        "https://www.pathwaycommons.org/",
        _text_strategy(
            PATHWAYCOMMONS_METADATA,
            _pathwaycommons_version,
            "pathwaycommons_datasources_txt",
            "Reads the release number and date from Pathway Commons metadata.",
        ),
        PATHWAYCOMMONS_FILES,
        1,
        (PATHWAYCOMMONS_METADATA,),
    ),
    "rhea_reaction_bundle": ReleaseSourceDefinition(
        DatasetId("rhea", "reaction_bundle"),
        "https://www.rhea-db.org/",
        _text_strategy(
            RHEA_PROPERTIES,
            _rhea_version,
            "rhea_release_properties",
            "Reads the release number and date from Rhea's release properties.",
        ),
        RHEA_FILES,
        6,
        (RHEA_PROPERTIES,),
    ),
    "wikipathways_human_gmt": ReleaseSourceDefinition(
        DatasetId("wikipathways", "human_gmt"),
        "https://www.wikipathways.org/",
        _text_strategy(
            WIKIPATHWAYS_GMT_INDEX,
            lambda text: _dated_listing_version(
                text,
                r"wikipathways-(\d{4})(\d{2})(\d{2})-gmt-Homo_sapiens\.gmt",
                "WikiPathways human GMT",
            ),
            "wikipathways_filename_date",
            "Reads the release date from the current human GMT filename.",
        ),
        _listed_file(WIKIPATHWAYS_GMT_INDEX, "file_name", "wikipathways_human.gmt"),
        1,
        (WIKIPATHWAYS_GMT_INDEX,),
    ),
    "wikipathways_rdf_wp": ReleaseSourceDefinition(
        DatasetId("wikipathways", "rdf_wp"),
        "https://www.wikipathways.org/",
        _text_strategy(
            WIKIPATHWAYS_RDF_INDEX,
            lambda text: _dated_listing_version(
                text,
                r"wikipathways-(\d{4})(\d{2})(\d{2})-rdf-wp\.zip",
                "WikiPathways RDF",
            ),
            "wikipathways_rdf_filename_date",
            "Reads the release date from the current RDF archive filename.",
        ),
        _listed_file(WIKIPATHWAYS_RDF_INDEX, "file_name", "wikipathways_rdf_wp.zip"),
        1,
        (WIKIPATHWAYS_RDF_INDEX,),
    ),
    "pfocr_human_pathways": ReleaseSourceDefinition(
        DatasetId("pfocr", "human_pathways"),
        "https://wikipathways.github.io/pfocr-database/",
        _text_strategy(
            PFOCR_INDEX,
            _pfocr_version,
            "pfocr_filename_date",
            "Reads the shared release date from current gene and chemical GMT filenames.",
        ),
        _pfocr_files,
        2,
        (PFOCR_INDEX,),
    ),
    "gtex_expression_v11": ReleaseSourceDefinition(
        DatasetId("gtex", "expression_v11"),
        "https://gtexportal.org/",
        FixedVersionStrategy(
            SourceVersion("v11", version_date=date(2025, 8, 22)),
            "Uses the reviewed GTEx v11 release selected in source configuration.",
            ("https://gtexportal.org/",),
        ),
        GTEX_FILES,
        3,
        ("https://gtexportal.org/",),
    ),
    "hpa_tissue_expression": ReleaseSourceDefinition(
        DatasetId("hpa", "tissue_expression"),
        "https://www.proteinatlas.org/",
        _with_dates(
            _text_strategy(
                HPA_ABOUT,
                _hpa_version,
                "hpa_download_page_version",
                "Reads the release number from the Human Protein Atlas download page.",
            ),
            HPA_FILES,
        ),
        HPA_FILES,
        2,
        (HPA_ABOUT,),
    ),
    "tiga_gene_trait": ReleaseSourceDefinition(
        DatasetId("tiga", "gene_trait"),
        "https://unmtid-shinyapps.net/tiga/",
        _with_dates(
            _text_strategy(
                TIGA_INDEX,
                lambda text: _latest_directory_version(text, r'href="([0-9]{8})/"', "TIGA"),
                "tiga_release_directory",
                "Reads the newest dated release directory from the TIGA download site.",
            ),
            TIGA_FILES,
        ),
        TIGA_FILES,
        2,
        (TIGA_INDEX,),
    ),
    "surechembl_patent_discovery": ReleaseSourceDefinition(
        DatasetId("surechembl", "patent_discovery"),
        "https://chembl.gitbook.io/surechembl/",
        _text_strategy(
            SURECHEMBL_INDEX,
            lambda text: _latest_directory_version(
                text, r'href="([0-9]{4}-[0-9]{2}-[0-9]{2})/"', "SureChEMBL"
            ),
            "surechembl_release_directory",
            "Reads the newest dated bulk-data directory from SureChEMBL.",
        ),
        _surechembl_files,
        5,
        (SURECHEMBL_INDEX,),
    ),
}
