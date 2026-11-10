from collections.abc import Mapping
from typing import Any

from ..exceptions import DataDictionaryValidationError


def checked_pii_flag(value: Any, field: str) -> bool:
    """A PII mark sent for a dataset or column, which must be true or false."""
    if not isinstance(value, bool):
        raise DataDictionaryValidationError(f"{field} phải là true hoặc false.")
    return value


def mark_catalog_pii(
    catalog_datasets: list[dict[str, Any]],
    dataset_flags: Mapping[int, bool],
    column_flags: Mapping[tuple[int, str], bool],
) -> None:
    """Add whether each catalog dataset and dimension is marked as holding personal data (PII)."""
    for dataset in catalog_datasets:
        dataset["contains_pii"] = bool(dataset_flags.get(dataset["id"], False))
        for dimension in dataset.get("dimensions", []):
            dimension["contains_pii"] = bool(column_flags.get((dataset["id"], dimension["name"]), False))
