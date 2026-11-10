from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from ..exceptions import (
    ExpertRuleConflictError,
    ExpertRuleGroupNotFoundError,
    ExpertRuleNotFoundError,
    ExpertRuleValidationError,
)
from .expert_rule import MAX_NAME, MAX_REASON, MAX_TEXT, _text

GROUP_NAME_TAKEN = "Đã có nhóm rule cùng tên."
UNGROUPED = "Chưa phân nhóm"


def rule_group_id(payload: Any) -> int | None:
    """The group a request puts a rule in; null takes the rule out of every group."""
    group_id = payload.get("group_id") if isinstance(payload, dict) else None
    if group_id is not None and (isinstance(group_id, bool) or not isinstance(group_id, int)):
        raise ExpertRuleValidationError("Nhóm rule không hợp lệ.")
    return group_id


def normalize_group_payload(payload: Any) -> dict[str, Any]:
    """A group's name and optional description, held to the same prose rules as a rule's own text."""
    if not isinstance(payload, dict) or set(payload) - {"name", "description"}:
        raise ExpertRuleValidationError("Nhóm rule chỉ có tên và mô tả.")
    if not isinstance(payload.get("name"), str) or not payload["name"].strip():
        raise ExpertRuleValidationError("Nhập tên nhóm rule.")
    name = _text(payload["name"], "name", MAX_NAME)
    description = payload.get("description")
    if description is not None and not isinstance(description, str):
        raise ExpertRuleValidationError("Mô tả nhóm rule không hợp lệ.")
    description = _text(description, "description", MAX_TEXT) if description and description.strip() else None
    return {"name": name, "normalized_name": name.casefold(), "description": description}


def require_rule_group(session: Any, group_id: int) -> Any:
    from ..repositories.expert_rule_group import ExpertRuleGroupRepository

    group = ExpertRuleGroupRepository(session).get(group_id)
    if group is None:
        raise ExpertRuleGroupNotFoundError()
    return group


def create_rule_group(session: Any, payload: Any) -> Any:
    from ..models import ExpertRuleGroup
    from ..repositories.expert_rule_group import ExpertRuleGroupRepository

    values = normalize_group_payload(payload)
    repository = ExpertRuleGroupRepository(session)
    if repository.by_normalized_name(values["normalized_name"]) is not None:
        raise ExpertRuleConflictError(GROUP_NAME_TAKEN)
    group = ExpertRuleGroup(**values)
    try:
        repository.add(group)
    except IntegrityError as error:
        raise ExpertRuleConflictError(GROUP_NAME_TAKEN) from error
    return group


def update_rule_group(session: Any, group_id: int, payload: Any) -> Any:
    """Rename or describe a group; its rules stay in it and none of them needs a new review."""
    from ..repositories.expert_rule_group import ExpertRuleGroupRepository

    group = require_rule_group(session, group_id)
    values = normalize_group_payload(payload)
    repository = ExpertRuleGroupRepository(session)
    same_name = repository.by_normalized_name(values["normalized_name"])
    if same_name is not None and same_name.id != group.id:
        raise ExpertRuleConflictError(GROUP_NAME_TAKEN)
    try:
        repository.update(group, values)
    except IntegrityError as error:
        raise ExpertRuleConflictError(GROUP_NAME_TAKEN) from error
    return group


def delete_rule_group(session: Any, group_id: int, actor_id: int) -> list[int]:
    """Delete a group and leave its rules ungrouped, each with a move in its audit trail; returns the moved rule IDs.

    A group only organizes rules, so deleting it changes no rule's content or approval.
    """
    from ..repositories.expert_rule import ExpertRuleRepository
    from ..repositories.expert_rule_group import ExpertRuleGroupRepository

    group = require_rule_group(session, group_id)
    rules = ExpertRuleRepository(session).in_group(group.id)
    for rule in rules:
        place_rule(session, rule, None, actor_id)
    ExpertRuleGroupRepository(session).delete(group)
    return [rule.id for rule in rules]


def move_rule(session: Any, rule_id: int, group_id: int | None, actor_id: int) -> Any:
    from ..repositories.expert_rule import ExpertRuleRepository

    rule = ExpertRuleRepository(session).lock(rule_id)
    if rule is None:
        raise ExpertRuleNotFoundError()
    place_rule(session, rule, None if group_id is None else require_rule_group(session, group_id), actor_id)
    return rule


def place_rule(session: Any, rule: Any, group: Any | None, actor_id: int) -> None:
    """Put a rule in a group, or in none, and record the move on its current revision.

    The group is not part of a revision and the lifecycle version stays, so a pending approval is not invalidated.
    """
    from ..models import ExpertRuleAuditEvent
    from ..repositories.expert_rule_audit_event import ExpertRuleAuditEventRepository
    from ..repositories.expert_rule_revision import ExpertRuleRevisionRepository

    previous = rule.group
    if (previous.id if previous else None) == (group.id if group else None):
        return
    rule.group = group
    rule.group_id = group.id if group else None
    revision = ExpertRuleRevisionRepository(session).current_for(rule.id)
    if revision is None:
        return
    reason = f"{previous.name if previous else UNGROUPED} → {group.name if group else UNGROUPED}"
    ExpertRuleAuditEventRepository(session).add(
        ExpertRuleAuditEvent(
            rule_id=rule.id,
            revision_id=revision.id,
            event_type="moved",
            actor_id=actor_id,
            reason=reason[:MAX_REASON],
            revision_number=revision.revision_number,
            lifecycle_version=revision.lifecycle_version,
            occurred_at=datetime.now(timezone.utc),
        ),
        flush=False,
    )


def group_reference(group: Any | None) -> dict[str, Any] | None:
    return {"id": group.id, "name": group.name} if group else None
