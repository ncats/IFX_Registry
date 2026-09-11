"""Reusable source adapter for stable HTTP files versioned by Last-Modified."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from email.utils import parsedate_to_datetime

from ifx_registry.application.contracts import VersionProbeRequest
from ifx_registry.domain.errors import SourceValidationError
from ifx_registry.domain.models import DatasetId, SourceVersion
from ifx_registry.infrastructure.http import HttpGateway
from ifx_registry.infrastructure.sources.http_snapshot import HttpFileSpec, HttpSnapshotSource
from ifx_registry.infrastructure.sources.version_strategies import SourceVersionStrategy


@dataclass(frozen=True, slots=True)
class LastModifiedSourceDefinition:
    dataset: DatasetId
    files: tuple[HttpFileSpec, ...]
    homepage: str
    version_description: str


@dataclass(frozen=True, slots=True)
class MaxLastModifiedVersionStrategy(SourceVersionStrategy):
    urls: tuple[str, ...]
    description: str

    @property
    def evidence_urls(self) -> tuple[str, ...]:
        return self.urls

    def discover(
        self,
        http: HttpGateway,
        request: VersionProbeRequest,
    ) -> SourceVersion:
        evidence: list[dict[str, str]] = []
        version_dates: list[date] = []
        for url in self.urls:
            metadata = http.head(url, timeout=request.timeout.total_seconds())
            raw_date = metadata.header("Last-Modified")
            if raw_date:
                parsed_date = _parse_http_date(raw_date)
                version_dates.append(parsed_date)
                evidence.append(
                    {
                        "url": metadata.final_url,
                        "last_modified": raw_date,
                    }
                )
        if not version_dates:
            raise SourceValidationError(
                "Could not determine a version because no source file supplied a "
                "valid Last-Modified header"
            )
        version_date = max(version_dates)
        return SourceVersion(
            value=version_date.isoformat(),
            version_date=version_date,
            evidence={
                "method": (
                    "single_file_last_modified"
                    if len(self.urls) == 1
                    else "multi_file_max_last_modified"
                ),
                "files": evidence,
            },
        )


class LastModifiedHttpSource(HttpSnapshotSource):
    def __init__(self, http: HttpGateway, definition: LastModifiedSourceDefinition):
        super().__init__(http)
        self._definition = definition
        self._version_strategy = MaxLastModifiedVersionStrategy(
            tuple(file.url for file in definition.files),
            definition.version_description,
        )

    @property
    def dataset(self) -> DatasetId:
        return self._definition.dataset

    @property
    def file_specs(self) -> tuple[HttpFileSpec, ...]:
        return self._definition.files

    @property
    def homepage(self) -> str:
        return self._definition.homepage

    @property
    def version_strategy(self) -> SourceVersionStrategy:
        return self._version_strategy


def _parse_http_date(value: str) -> date:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError) as error:
        raise SourceValidationError(f"Could not parse Last-Modified header {value!r}") from error
    if parsed is None:
        raise SourceValidationError(f"Could not parse Last-Modified header {value!r}")
    return parsed.date()


LAST_MODIFIED_SOURCES: dict[str, LastModifiedSourceDefinition] = {
    "hcop_human_all_sixteen_column": LastModifiedSourceDefinition(
        DatasetId("hcop", "human_all_sixteen_column"),
        (
            HttpFileSpec(
                "https://storage.googleapis.com/public-download-files/hcop/human_all_hcop_sixteen_column.txt.gz",
                "human_all_hcop_sixteen_column.txt.gz",
            ),
        ),
        "https://www.genenames.org/tools/hcop/",
        "Uses the source file's Last-Modified date as its version.",
    ),
    "mgi_hmd_human_phenotype": LastModifiedSourceDefinition(
        DatasetId("mgi", "hmd_human_phenotype"),
        (
            HttpFileSpec(
                "https://www.informatics.jax.org/downloads/reports/HMD_HumanPhenotype.rpt",
                "HMD_HumanPhenotype.rpt",
            ),
        ),
        "https://www.informatics.jax.org/",
        "Uses the report's Last-Modified date as its version.",
    ),
    "impc_genotype_phenotype_assertions": LastModifiedSourceDefinition(
        DatasetId("impc", "genotype_phenotype_assertions"),
        (
            HttpFileSpec(
                "https://ftp.ebi.ac.uk/pub/databases/impc/all-data-releases/latest/results/genotype-phenotype-assertions-IMPC.csv.gz",
                "genotype-phenotype-assertions-IMPC.csv.gz",
            ),
        ),
        "https://www.mousephenotype.org/",
        "Uses the latest release file's Last-Modified date as its version.",
    ),
    "pubtator_gene2pubtator3": LastModifiedSourceDefinition(
        DatasetId("pubtator", "gene2pubtator3"),
        (
            HttpFileSpec(
                "https://ftp.ncbi.nlm.nih.gov/pub/lu/PubTator3/gene2pubtator3.gz",
                "gene2pubtator3.gz",
            ),
        ),
        "https://www.ncbi.nlm.nih.gov/research/pubtator/",
        "Uses the gene2pubtator3 file's Last-Modified date as its version.",
    ),
    "ncbi_publications": LastModifiedSourceDefinition(
        DatasetId("ncbi", "publications"),
        (
            HttpFileSpec("https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene2pubmed.gz", "gene2pubmed.gz"),
            HttpFileSpec(
                "https://ftp.ncbi.nlm.nih.gov/gene/GeneRIF/generifs_basic.gz", "generifs_basic.gz"
            ),
        ),
        "https://www.ncbi.nlm.nih.gov/gene/",
        "Uses the newest Last-Modified date across the Gene-to-PubMed and GeneRIF files.",
    ),
    "ncbi_gene_summary": LastModifiedSourceDefinition(
        DatasetId("ncbi", "gene_summary"),
        (
            HttpFileSpec(
                "https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene_summary.gz", "gene_summary.gz"
            ),
        ),
        "https://www.ncbi.nlm.nih.gov/gene/",
        "Uses the gene summary file's Last-Modified date as its version.",
    ),
    "jensenlab_tissues": LastModifiedSourceDefinition(
        DatasetId("jensenlab", "tissues"),
        (
            HttpFileSpec(
                "https://download.jensenlab.org/human_tissue_integrated_full.tsv",
                "human_tissue_integrated_full.tsv",
            ),
        ),
        "https://jensenlab.org/resources/proteomics/",
        "Uses the tissue file's Last-Modified date as its version.",
    ),
    "jensenlab_diseases": LastModifiedSourceDefinition(
        DatasetId("jensenlab", "diseases"),
        tuple(
            HttpFileSpec(f"https://download.jensenlab.org/{name}", name)
            for name in (
                "human_disease_knowledge_filtered.tsv",
                "human_disease_experiments_filtered.tsv",
                "human_disease_textmining_filtered.tsv",
            )
        ),
        "https://jensenlab.org/resources/proteomics/",
        "Uses the newest Last-Modified date across the three disease evidence files.",
    ),
    "jensenlab_protein_counts": LastModifiedSourceDefinition(
        DatasetId("jensenlab", "protein_counts"),
        (HttpFileSpec("https://download.jensenlab.org/protein_counts.tsv", "protein_counts.tsv"),),
        "https://jensenlab.org/resources/proteomics/",
        "Uses the protein counts file's Last-Modified date as its version.",
    ),
    "jensenlab_tinx": LastModifiedSourceDefinition(
        DatasetId("jensenlab", "tinx"),
        (
            HttpFileSpec(
                "https://download.jensenlab.org/human_textmining_mentions.tsv",
                "human_textmining_mentions.tsv.gz",
                gzip_download=True,
            ),
            HttpFileSpec(
                "https://download.jensenlab.org/disease_textmining_mentions.tsv",
                "disease_textmining_mentions.tsv.gz",
                gzip_download=True,
            ),
        ),
        "https://jensenlab.org/resources/proteomics/",
        "Uses the newest Last-Modified date across the two text-mining files.",
    ),
    "expasy_enzyme": LastModifiedSourceDefinition(
        DatasetId("expasy", "enzyme"),
        (
            HttpFileSpec("https://ftp.expasy.org/databases/enzyme/enzclass.txt", "enzclass.txt"),
            HttpFileSpec("https://ftp.expasy.org/databases/enzyme/enzyme.dat", "enzyme.dat"),
        ),
        "https://enzyme.expasy.org/",
        "Uses the newest Last-Modified date across the enzyme class and detail files.",
    ),
    "iuphar_ligands_interactions": LastModifiedSourceDefinition(
        DatasetId("iuphar", "ligands_interactions"),
        (
            HttpFileSpec("https://www.guidetopharmacology.org/DATA/ligands.csv", "ligands.csv"),
            HttpFileSpec(
                "https://www.guidetopharmacology.org/DATA/interactions.csv", "interactions.csv"
            ),
        ),
        "https://www.guidetopharmacology.org/",
        "Uses the newest Last-Modified date across the ligand and interaction files.",
    ),
    "uberon_ontology": LastModifiedSourceDefinition(
        DatasetId("uberon", "ontology"),
        (HttpFileSpec("http://purl.obolibrary.org/obo/uberon.obo", "uberon.obo"),),
        "https://obofoundry.org/ontology/uberon.html",
        "Uses the ontology file's Last-Modified date as its version.",
    ),
    "go_ontology": LastModifiedSourceDefinition(
        DatasetId("go", "ontology"),
        (HttpFileSpec("https://current.geneontology.org/ontology/go-basic.json", "go-basic.json"),),
        "https://geneontology.org/",
        "Uses the ontology file's Last-Modified date as its version.",
    ),
    "go_goa_human_go": LastModifiedSourceDefinition(
        DatasetId("go", "goa_human_go"),
        (
            HttpFileSpec(
                "https://current.geneontology.org/annotations/goa_human.gaf.gz", "goa_human.gaf.gz"
            ),
        ),
        "https://geneontology.org/",
        "Uses the GO-hosted human annotation file's Last-Modified date as its version.",
    ),
    "go_goa_human_uniprot": LastModifiedSourceDefinition(
        DatasetId("go", "goa_human_uniprot"),
        (
            HttpFileSpec(
                "https://ftp.ebi.ac.uk/pub/databases/GO/goa/HUMAN/goa_human.gaf.gz",
                "goa_human.gaf.gz",
            ),
        ),
        "https://geneontology.org/",
        "Uses the UniProt-hosted human annotation file's Last-Modified date as its version.",
    ),
    "mondo_ontology": LastModifiedSourceDefinition(
        DatasetId("mondo", "ontology"),
        (HttpFileSpec("https://purl.obolibrary.org/obo/mondo.json", "mondo.json"),),
        "https://mondo.monarchinitiative.org/",
        "Uses the ontology file's Last-Modified date as its version.",
    ),
    "chebi_three_star_sdf": LastModifiedSourceDefinition(
        DatasetId("chebi", "three_star_sdf"),
        (
            HttpFileSpec(
                "https://ftp.ebi.ac.uk/pub/databases/chebi/SDF/chebi_3_stars.sdf.gz",
                "chebi_3_stars.sdf.gz",
            ),
        ),
        "https://www.ebi.ac.uk/chebi/",
        "Uses the three-star structure file's Last-Modified date as its version.",
    ),
}
