from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from ..dependencies import (
    glossary_term_audit_event_repository,
    glossary_term_group_repository,
    glossary_term_repository,
    glossary_term_revision_repository,
)
from ..exceptions import GlossaryConflictError, GlossaryTermGroupNotFoundError, GlossaryTermNotFoundError
from ..models import GlossaryTerm, GlossaryTermAuditEvent, GlossaryTermGroup
from .validation import MAX_REASON_LENGTH, normalize_group_payload

GROUP_NAME_TAKEN = "Đã có nhóm thuật ngữ cùng tên."
UNGROUPED = "Chưa phân nhóm"


def require_group(group_id: int) -> GlossaryTermGroup:
    group = glossary_term_group_repository().get(group_id)
    if group is None:
        raise GlossaryTermGroupNotFoundError()
    return group


def create_group(payload: Any) -> GlossaryTermGroup:
    values = normalize_group_payload(payload)
    repository = glossary_term_group_repository()
    if repository.by_normalized_name(values["normalized_name"]) is not None:
        raise GlossaryConflictError(GROUP_NAME_TAKEN)
    group = GlossaryTermGroup(**values)
    try:
        repository.add(group)
    except IntegrityError as error:
        raise GlossaryConflictError(GROUP_NAME_TAKEN) from error
    return group


def update_group(group_id: int, payload: Any) -> GlossaryTermGroup:
    """Rename or describe a group; its terms stay in it and none of them needs a new review."""
    group = require_group(group_id)
    values = normalize_group_payload(payload)
    same_name = glossary_term_group_repository().by_normalized_name(values["normalized_name"])
    if same_name is not None and same_name.id != group.id:
        raise GlossaryConflictError(GROUP_NAME_TAKEN)
    try:
        glossary_term_group_repository().update(group, values)
    except IntegrityError as error:
        raise GlossaryConflictError(GROUP_NAME_TAKEN) from error
    return group


def delete_group(group_id: int, actor_id: int) -> list[int]:
    """Delete a group and leave its terms ungrouped, each with a move in its audit trail; returns the moved term IDs.

    A group only organizes terms, so deleting it changes no term's meaning or approval.
    """
    group = require_group(group_id)
    terms = glossary_term_repository().in_group(group.id)
    for term in terms:
        place_term(term, None, actor_id)
    glossary_term_group_repository().delete(group)
    return [term.id for term in terms]


def move_term(term_id: int, group_id: int | None, actor_id: int) -> GlossaryTerm:
    term = glossary_term_repository().lock(term_id)
    if term is None:
        raise GlossaryTermNotFoundError()
    place_term(term, None if group_id is None else require_group(group_id), actor_id)
    return term


def place_term(term: GlossaryTerm, group: GlossaryTermGroup | None, actor_id: int) -> None:
    """Put a term in a group, or in none, and record the move on its current revision.

    The group is not part of a revision, so the move needs no review and the approved revision keeps serving.
    """
    previous = term.group
    if (previous.id if previous else None) == (group.id if group else None):
        return
    term.group = group
    term.group_id = group.id if group else None
    revision = glossary_term_revision_repository().current_for(term.id)
    if revision is None:
        return
    reason = f"{previous.name if previous else UNGROUPED} → {group.name if group else UNGROUPED}"
    glossary_term_audit_event_repository().add(
        GlossaryTermAuditEvent(
            term_id=term.id,
            revision_id=revision.id,
            event_type="moved",
            actor_id=actor_id,
            reason=reason[:MAX_REASON_LENGTH],
            revision_number=revision.revision_number,
            occurred_at=datetime.now(timezone.utc),
        ),
        flush=False,
    )


def group_reference(group: GlossaryTermGroup | None) -> dict[str, Any] | None:
    return {"id": group.id, "name": group.name} if group else None
