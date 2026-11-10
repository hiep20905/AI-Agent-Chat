from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from flask_login import current_user

from ...data_dictionary.dependencies import dataset_dictionary_repository, dataset_grain_repository
from ..dependencies import metric_contract_event_repository, metric_contract_repository, metric_definition_repository
from ..exceptions import MetricContractValidationError
from ..models import MetricContract, MetricContractEvent, MetricDefinition
from .limits import METRIC_REVIEW_REASON_MAX_LENGTH
from .query_defaults import QUERY_DEFAULT_FIELDS, MetricQueryDefaults, stored_query_defaults


def metric_contract_for(dataset_id: int, metric_id: int) -> MetricContract | None:
    return metric_contract_repository().for_dataset_metric(dataset_id, metric_id)


def metric_definition_for(dataset_id: int, metric_id: int) -> MetricDefinition | None:
    return metric_definition_repository().for_dataset_metric(dataset_id, metric_id)


def metric_query_defaults_on(dataset_ids: Iterable[int]) -> dict[tuple[int, int], MetricQueryDefaults]:
    """The query defaults of every metric of these datasets that has some, by (dataset ID, metric ID)."""
    dataset_ids = sorted(set(dataset_ids))
    definitions = metric_definition_repository().for_datasets(dataset_ids) if dataset_ids else []
    declared = [item for item in definitions if any(getattr(item, field, None) for field in QUERY_DEFAULT_FIELDS)]
    if not declared:
        return {}
    declared_ids = sorted({item.dataset_id for item in declared})
    dictionaries = dataset_dictionary_repository().list_by_ids(declared_ids)
    row_kind_columns = {item.id: item.row_kind_column for item in dictionaries}
    grains: dict[int, dict[str | None, str]] = {}
    for grain in dataset_grain_repository().list_for_datasets(declared_ids):
        grains.setdefault(grain.dataset_id, {})[grain.row_kind_value] = grain.grain
    loaded = {}
    for item in declared:
        defaults = stored_query_defaults(item, row_kind_columns.get(item.dataset_id), grains.get(item.dataset_id, {}))
        if defaults is not None:
            loaded[(item.dataset_id, item.metric_id)] = defaults
    return loaded


def metric_query_defaults_for(dataset_id: int, metric_id: int) -> MetricQueryDefaults | None:
    """One metric's grain, time anchor and default filters, read with its dataset's row kinds; None when unset."""
    return metric_query_defaults_on([dataset_id]).get((dataset_id, metric_id))


def review_reason(body: Any) -> str | None:
    """The optional note or reason a reviewer sends with a metric decision, trimmed; blank means none."""
    reason = body.get("reason") if isinstance(body, dict) else None
    if reason is None:
        return None
    if not isinstance(reason, str) or len(reason) > METRIC_REVIEW_REASON_MAX_LENGTH:
        limit = METRIC_REVIEW_REASON_MAX_LENGTH
        raise MetricContractValidationError(f"Lý do phải là văn bản tối đa {limit} ký tự.")
    return reason.strip() or None


def record_metric_event(
    dataset_id: int, metric_id: int, action: str, reason: str | None = None, contract: Any = None
) -> None:
    """Append one review event. Callers hash the metric first: adding a row can autoflush the session."""
    event = MetricContractEvent(
        dataset_id=dataset_id,
        metric_id=metric_id,
        action=action,
        actor_id=current_user.id,
        reason=reason,
        expression=contract.expression if contract is not None else None,
        expression_hash=contract.expression_hash if contract is not None else None,
        created_at=datetime.now(tz=timezone.utc),
    )
    metric_contract_event_repository().add(event, flush=False)
