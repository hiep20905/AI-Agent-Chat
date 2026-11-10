import hashlib
import json
from collections.abc import Callable
from typing import Any

from ...semantic_query.services.contracts import safe_connection_identity
from ..exceptions import GlossaryValidationError

MAX_NAME_LENGTH = 255
MAX_DEFINITION_LENGTH = 10_000
MAX_SYNONYMS = 50
MAX_REASON_LENGTH = 512
LINK_KINDS = {"dataset", "column", "metric"}
CONTENT_FIELDS = {"canonical_name", "definition", "synonyms", "steward_id", "links"}


def normalized_name(value: str) -> str:
    return " ".join(value.split()).casefold()


def _required_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not (result := " ".join(value.split())):
        raise GlossaryValidationError(f"{field} must be a non-empty string.")
    if len(result) > limit:
        raise GlossaryValidationError(f"{field} is too long.")
    return result


def expected_revision_of(payload: Any) -> int:
    """The revision number the client last saw, which a change must still match."""
    expected_revision = payload.get("expected_revision") if isinstance(payload, dict) else None
    if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
        raise GlossaryValidationError("expected_revision is required.")
    return expected_revision


def review_reason(payload: Any) -> str | None:
    """The optional note sent with an approval or a revocation, trimmed; blank means none."""
    reason = payload.get("reason") if isinstance(payload, dict) else None
    if reason is None:
        return None
    if not isinstance(reason, str) or len(reason) > MAX_REASON_LENGTH:
        raise GlossaryValidationError("Invalid review reason.")
    return reason.strip() or None


def group_id_of(payload: Any) -> int | None:
    """The group a request puts a term in; null takes the term out of every group."""
    group_id = payload.get("group_id") if isinstance(payload, dict) else None
    if group_id is not None and (isinstance(group_id, bool) or not isinstance(group_id, int)):
        raise GlossaryValidationError("Nhóm thuật ngữ không hợp lệ.")
    return group_id


def normalize_group_payload(payload: Any) -> dict[str, Any]:
    """A group's name, trimmed and unique without regard to case or spacing, and its optional description."""
    if not isinstance(payload, dict) or set(payload) - {"name", "description"}:
        raise GlossaryValidationError("Nhóm thuật ngữ chỉ có tên và mô tả.")
    name = payload.get("name")
    if not isinstance(name, str) or not (name := " ".join(name.split())):
        raise GlossaryValidationError("Nhập tên nhóm thuật ngữ.")
    if len(name) > MAX_NAME_LENGTH:
        raise GlossaryValidationError("Tên nhóm thuật ngữ quá dài.")
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise GlossaryValidationError("Mô tả nhóm thuật ngữ không hợp lệ.")
    description = (description or "").strip() or None
    if description is not None and len(description) > MAX_DEFINITION_LENGTH:
        raise GlossaryValidationError("Mô tả nhóm thuật ngữ quá dài.")
    return {"name": name, "normalized_name": normalized_name(name), "description": description}


def normalize_glossary_payload(payload: Any) -> dict[str, Any]:
    """Reject unknown fields and canonicalize text before persistence."""
    if not isinstance(payload, dict) or set(payload) - CONTENT_FIELDS - {"expected_revision", "group_id"}:
        raise GlossaryValidationError("Unsupported glossary fields.")
    name = _required_text(payload.get("canonical_name"), "canonical_name", MAX_NAME_LENGTH)
    definition = _required_text(payload.get("definition"), "definition", MAX_DEFINITION_LENGTH)
    synonyms = payload.get("synonyms", [])
    if not isinstance(synonyms, list) or len(synonyms) > MAX_SYNONYMS:
        raise GlossaryValidationError("synonyms must be a bounded array.")
    name_key = normalized_name(name)
    clean_synonyms: list[str] = []
    seen = {name_key}
    for synonym in synonyms:
        clean = _required_text(synonym, "synonym", MAX_NAME_LENGTH)
        synonym_key = normalized_name(clean)
        if synonym_key in seen:
            if synonym_key == name_key:
                raise GlossaryValidationError("A synonym cannot duplicate the canonical name.")
            continue
        seen.add(synonym_key)
        clean_synonyms.append(clean)
    links = payload.get("links")
    if not isinstance(links, list) or not links:
        raise GlossaryValidationError("At least one glossary link is required.")
    for link in links:
        if not isinstance(link, dict) or set(link) - {"kind", "dataset_id", "column_name", "metric_id"}:
            raise GlossaryValidationError("Invalid glossary link.")
        dataset_id = link.get("dataset_id")
        if link.get("kind") not in LINK_KINDS or isinstance(dataset_id, bool) or not isinstance(dataset_id, int):
            raise GlossaryValidationError("Invalid glossary link.")
    steward_id = payload.get("steward_id")
    if steward_id is not None and (isinstance(steward_id, bool) or not isinstance(steward_id, int)):
        raise GlossaryValidationError("steward_id must be a user id.")
    return {
        "canonical_name": name,
        "normalized_name": name_key,
        "definition": definition,
        "synonyms": clean_synonyms,
        "steward_id": steward_id,
        "links": links,
    }


def glossary_link_fingerprint(dataset: Any, kind: str, target: Any | None = None) -> str:
    """Bind a link to live safe metadata, excluding connection credentials."""
    if kind not in LINK_KINDS:
        raise GlossaryValidationError("Unsupported glossary link kind.")
    payload = {
        "kind": kind,
        "dataset_id": getattr(dataset, "id", None),
        "catalog": getattr(dataset, "catalog", None),
        "schema": getattr(dataset, "schema", None),
        "table": getattr(dataset, "table_name", None),
        "sql": getattr(dataset, "sql", None),
        "database": safe_connection_identity(getattr(dataset, "database", None)),
    }
    if kind == "column":
        payload["column"] = getattr(target, "column_name", None)
        payload["active"] = getattr(target, "is_active", None)
        payload["expression"] = getattr(target, "expression", None)
    elif kind == "metric":
        payload["metric_id"] = getattr(target, "id", None)
        payload["metric_name"] = getattr(target, "metric_name", None)
        payload["expression"] = getattr(target, "expression", None)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def content_hash(values: dict[str, Any]) -> str:
    """A revision's content, so an expert rule built on a term notices when the term means something else."""
    content = {key: values.get(key) for key in ("canonical_name", "definition", "synonyms", "links")}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def validated_links(
    links: list[dict[str, Any]],
    dataset_resolver: Callable[[int], Any],
    metric_is_approved: Callable[[Any, Any], bool],
) -> list[dict[str, Any]]:
    """The links bound to live objects: a physical active column, or a metric whose contract is approved."""
    result = []
    seen = set()
    for raw in links:
        dataset = dataset_resolver(raw["dataset_id"])
        if dataset is None:
            raise GlossaryValidationError("Invalid glossary link.")
        kind = raw["kind"]
        target = None
        if kind == "column":
            name = raw.get("column_name")
            target = next((column for column in dataset.columns if getattr(column, "column_name", None) == name), None)
            if target is None or getattr(target, "is_active", None) is not True or getattr(target, "expression", None):
                raise GlossaryValidationError("Invalid glossary link.")
        elif kind == "metric":
            metric_id = raw.get("metric_id")
            target = next((metric for metric in dataset.metrics if getattr(metric, "id", None) == metric_id), None)
            if target is None:
                raise GlossaryValidationError("Invalid glossary link.")
            if not metric_is_approved(dataset, target):
                raise GlossaryValidationError("A glossary term may link only approved metrics.")
        elif raw.get("column_name") is not None or raw.get("metric_id") is not None:
            raise GlossaryValidationError("Invalid glossary link.")
        values = {
            "kind": kind,
            "dataset_id": dataset.id,
            "column_name": getattr(target, "column_name", None),
            "metric_id": getattr(target, "id", None) if kind == "metric" else None,
            "link_key": f"{kind}:{dataset.id}:{getattr(target, 'column_name', '')}:{getattr(target, 'id', '')}",
            "fingerprint": glossary_link_fingerprint(dataset, kind, target),
        }
        key = (values["kind"], values["dataset_id"], values["column_name"], values["metric_id"])
        if key in seen:
            raise GlossaryValidationError("Duplicate glossary link.")
        seen.add(key)
        result.append(values)
    return result
