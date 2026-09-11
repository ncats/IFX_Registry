"""Errors expressed in Registry domain language."""


class RegistryError(Exception):
    """Base class for expected Registry failures."""


class RegistryClientConfigurationError(RegistryError):
    """Raised when an injected client lacks a requested capability."""


class SourceConfigurationError(RegistryError):
    """Raised when the installed-source configuration is invalid."""


class InvalidDatasetIdError(RegistryError, ValueError):
    """Raised when a source or dataset name is not a safe Registry identifier."""


class InvalidSnapshotIdError(RegistryError, ValueError):
    """Raised when a pinned source:dataset:version reference is invalid."""


class SourceContractError(RegistryError):
    """Raised when a source implementation violates the source port contract."""


class VersionMismatchError(SourceContractError):
    """Raised when a fetch returns a version other than the requested version."""


class SourceAcquisitionError(RegistryError):
    """Raised when an upstream source cannot be queried or downloaded safely."""


class SourceValidationError(RegistryError):
    """Raised when downloaded source files violate source-specific invariants."""


class UnknownSourceError(RegistryError, LookupError):
    """Raised when a caller requests a source that is not installed."""


class AcquisitionJobNotFoundError(RegistryError, LookupError):
    """Raised when an acquisition job does not exist."""


class AcquisitionAlreadyRunningError(RegistryError):
    """Raised when the same source version already has an active acquisition."""


class DerivedBuildAlreadyRunningError(RegistryError):
    """Raised when a derived dataset already has an active build."""


class DerivedBuildJobNotFoundError(RegistryError, LookupError):
    """Raised when a derived build job does not exist."""


class UnknownDerivedRecipeError(RegistryError, LookupError):
    """Raised when no reusable build recipe is installed for a dataset."""


class InvalidDerivedBuildError(RegistryError, ValueError):
    """Raised when selected inputs do not satisfy a recipe contract."""


class SourceVersionNotApprovedError(RegistryError):
    """Raised when acquisition was not preceded by a matching recent check."""


class OperationalStateUnavailableError(RegistryError):
    """Raised when temporary job or version-check state cannot be read or written."""


class SnapshotAlreadyExistsError(RegistryError):
    """Raised when publication would replace an immutable published snapshot."""


class InvalidPublicationError(RegistryError, ValueError):
    """Raised when caller-provided snapshot files or provenance are invalid."""


class SnapshotNotFoundError(RegistryError, LookupError):
    """Raised when an exact snapshot is not present in Registry storage."""


class RegistryUnavailableError(RegistryError):
    """Raised when the authoritative Registry storage cannot be reached."""


class CatalogConsistencyError(RegistryError):
    """Raised when a published manifest violates the Registry contract."""


class MaterializationError(RegistryError):
    """Raised when a pinned snapshot cannot be verified into a local cache."""


class MultipleDatasetFilesError(RegistryError, ValueError):
    """Raised when a caller must select one file from a multi-file dataset."""


class DatasetFileNotFoundError(RegistryError, FileNotFoundError):
    """Raised when a named file is not declared by a pinned dataset."""
