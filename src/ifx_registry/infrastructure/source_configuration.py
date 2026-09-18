"""Strict YAML configuration for the installed Registry source catalog."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path
from typing import cast

import yaml

from ifx_registry.domain.catalog import SourceDescriptor
from ifx_registry.domain.errors import SourceConfigurationError
from ifx_registry.infrastructure.catalog import InMemorySourceCatalog
from ifx_registry.infrastructure.source_factory import BuiltInSourceFactory

DEFAULT_SOURCE_CONFIGURATION = Path(__file__).resolve().parent.parent / "config" / "sources.yaml"

_ROOT_KEYS = frozenset({"schema_version", "sources"})
_SOURCE_KEYS = frozenset({"adapter", "enabled", "display_name", "description"})


class YamlSourceCatalogLoader:
    """Build the installed-source catalog from a strict, data-only YAML file."""

    def __init__(self, factory: BuiltInSourceFactory):
        self._factory = factory

    def load(
        self,
        path: Path = DEFAULT_SOURCE_CONFIGURATION,
        *,
        excluded_adapters: Collection[str] = (),
    ) -> InMemorySourceCatalog:
        configuration_path = Path(path)
        root = self._read_root(configuration_path)
        self._reject_unknown_keys(root, _ROOT_KEYS, "source configuration")
        schema_version = root.get("schema_version")
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != 1
        ):
            raise SourceConfigurationError(
                f"{configuration_path}: schema_version must be the integer 1"
            )

        raw_sources = root.get("sources")
        if not isinstance(raw_sources, list):
            raise SourceConfigurationError(f"{configuration_path}: sources must be a list")

        entries = []
        configured_adapters: set[str] = set()
        for index, raw_source in enumerate(raw_sources):
            location = f"{configuration_path}: sources[{index}]"
            source = self._as_mapping(raw_source, location)
            self._reject_unknown_keys(source, _SOURCE_KEYS, location)
            adapter_name = self._required_text(source, "adapter", location)
            if adapter_name in configured_adapters:
                raise SourceConfigurationError(
                    f"{location}: adapter {adapter_name!r} is configured more than once"
                )
            configured_adapters.add(adapter_name)
            if not self._factory.is_registered(adapter_name):
                available = ", ".join(self._factory.adapter_names) or "none"
                raise SourceConfigurationError(
                    f"{location}: unknown adapter {adapter_name!r}; available adapters: {available}"
                )
            if adapter_name in excluded_adapters:
                continue

            enabled = source.get("enabled", True)
            if not isinstance(enabled, bool):
                raise SourceConfigurationError(f"{location}: enabled must be true or false")
            display_name = self._required_text(source, "display_name", location)
            description = self._required_text(source, "description", location)
            if not enabled:
                continue

            configured = self._factory.create(adapter_name)
            descriptor = SourceDescriptor(
                dataset=configured.adapter.dataset,
                display_name=display_name,
                description=description,
                expected_file_count=configured.expected_file_count,
                homepage=configured.adapter.homepage,
                upstream_urls=configured.adapter.upstream_urls,
                version_check_description=configured.adapter.version_check_description,
                version_evidence_urls=configured.adapter.version_evidence_urls,
            )
            entries.append((descriptor, configured.adapter))

        return InMemorySourceCatalog(entries)

    @staticmethod
    def _read_root(path: Path) -> Mapping[str, object]:
        try:
            raw = cast(object, yaml.safe_load(path.read_text(encoding="utf-8")))
        except OSError as error:
            raise SourceConfigurationError(
                f"Could not read source configuration {path}: {error}"
            ) from error
        except yaml.YAMLError as error:
            raise SourceConfigurationError(
                f"Could not parse source configuration {path}: {error}"
            ) from error
        return YamlSourceCatalogLoader._as_mapping(raw, str(path))

    @staticmethod
    def _as_mapping(value: object, location: str) -> Mapping[str, object]:
        if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
            raise SourceConfigurationError(f"{location}: expected a mapping with string keys")
        return cast(Mapping[str, object], value)

    @staticmethod
    def _reject_unknown_keys(
        value: Mapping[str, object],
        allowed: frozenset[str],
        location: str,
    ) -> None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise SourceConfigurationError(f"{location}: unknown field(s): {', '.join(unknown)}")

    @staticmethod
    def _required_text(
        value: Mapping[str, object],
        field: str,
        location: str,
    ) -> str:
        raw = value.get(field)
        if not isinstance(raw, str) or not raw.strip():
            raise SourceConfigurationError(f"{location}: {field} must be non-blank text")
        return raw.strip()
