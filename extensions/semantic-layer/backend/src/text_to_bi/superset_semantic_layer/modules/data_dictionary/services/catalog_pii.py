from typing import Any

from ..dependencies import column_dictionary_repository, dataset_dictionary_repository
from .pii import mark_catalog_pii


def label_catalog_pii(catalog_datasets: list[dict[str, Any]]) -> None:
    """Add the PII marks the data dictionary gives each catalog dataset and dimension, so a consumer sees them."""
    dataset_ids = [dataset["id"] for dataset in catalog_datasets]
    if not dataset_ids:
        return
    dataset_flags = dataset_dictionary_repository().pii_flags(dataset_ids)
    column_flags = {
        (entry.dataset_id, entry.column_name): bool(entry.contains_pii)
        for entry in column_dictionary_repository().list_for_datasets(dataset_ids)
    }
    mark_catalog_pii(catalog_datasets, dataset_flags, column_flags)
