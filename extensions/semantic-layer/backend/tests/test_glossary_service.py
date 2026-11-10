import ast
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from catalog_source import MODULES_PATH, compile_into
from sqlalchemy import JSON, Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

from text_to_bi.superset_semantic_layer.modules.glossary.exceptions import (
    GlossaryConflictError,
    GlossarySelfApprovalError,
    GlossaryTermGroupNotFoundError,
    GlossaryTermNotFoundError,
    GlossaryValidationError,
)
from text_to_bi.superset_semantic_layer.modules.glossary.services import validation
from text_to_bi.superset_semantic_layer.modules.glossary.services.validation import (
    expected_revision_of,
    glossary_link_fingerprint,
    normalize_glossary_payload,
    review_reason,
    validated_links,
)

SERVICE_PATH = MODULES_PATH / "glossary" / "services" / "glossary.py"
GROUP_SERVICE_PATH = MODULES_PATH / "glossary" / "services" / "group.py"
REPOSITORY_PATHS = [
    MODULES_PATH / "glossary" / "repositories" / name for name in ("glossary_term.py", "glossary_term_revision.py")
]
VALIDATION_HELPERS = (
    "content_hash",
    "normalize_glossary_payload",
    "validated_links",
    "group_id_of",
    "normalize_group_payload",
    "MAX_REASON_LENGTH",
)


def _payload(**overrides):
    return {
        "canonical_name": "Revenue",
        "definition": "Recognized revenue.",
        "synonyms": [],
        "steward_id": None,
        "links": [{"dataset_id": 7, "kind": "dataset"}],
        **overrides,
    }


def test_normalize_payload_trims_casefolds_and_deduplicates_synonyms():
    payload = normalize_glossary_payload(
        _payload(canonical_name="  Net Revenue ", synonyms=[" net sales ", "NET SALES", "revenue net"])
    )

    assert payload["canonical_name"] == "Net Revenue"
    assert payload["normalized_name"] == "net revenue"
    assert payload["synonyms"] == ["net sales", "revenue net"]


@pytest.mark.parametrize(
    "payload",
    [
        _payload(canonical_name=""),
        _payload(synonyms=[" revenue "]),
        _payload(links=[]),
        _payload(steward_id=True),
        # The removed provenance fields are refused rather than silently dropped.
        _payload(provenance="manual"),
        _payload(owner_id=1),
        _payload(source_reference="postgresql://admin:secret@db/internal"),
    ],
)
def test_normalize_payload_rejects_invalid_or_unknown_values(payload):
    with pytest.raises(GlossaryValidationError):
        normalize_glossary_payload(payload)


def test_the_revision_token_and_review_reason_are_read_from_the_request():
    assert expected_revision_of({"expected_revision": 4}) == 4
    for payload in (None, {}, {"expected_revision": True}, {"expected_revision": "4"}):
        with pytest.raises(GlossaryValidationError, match="expected_revision is required"):
            expected_revision_of(payload)
    assert review_reason({"reason": "  Định nghĩa sai.  "}) == "Định nghĩa sai."
    assert review_reason({"reason": "   "}) is None and review_reason(None) is None
    with pytest.raises(GlossaryValidationError):
        review_reason({"reason": "x" * 513})


def test_fingerprint_is_credential_free_and_stales_when_live_object_changes():
    dataset = SimpleNamespace(
        id=7,
        catalog="analytics",
        schema="public",
        table_name="daily_weather",
        sql=None,
        database=SimpleNamespace(id=2, database_name="warehouse", sqlalchemy_uri="postgresql://user:secret@host/db"),
        columns=[SimpleNamespace(column_name="temperature", is_active=True, expression=None)],
        metrics=[SimpleNamespace(id=19, metric_name="avg_temperature", expression="AVG(temperature)")],
    )
    fingerprint = glossary_link_fingerprint(dataset, "metric", dataset.metrics[0])

    assert "secret" not in fingerprint
    dataset.metrics[0].expression = "AVG(temperature_f)"
    assert glossary_link_fingerprint(dataset, "metric", dataset.metrics[0]) != fingerprint


def _weather_dataset():
    column = SimpleNamespace(id=86, column_name="quan_sat_id", is_active=True, expression=None)
    metric = SimpleNamespace(id=19, metric_name="observation_count", expression="COUNT(*)")
    return SimpleNamespace(id=11, columns=[column], metrics=[metric])


def test_validated_links_populates_only_the_matching_target_field():
    dataset = _weather_dataset()
    links = validated_links(
        [
            {"kind": "column", "dataset_id": 11, "column_name": "quan_sat_id"},
            {"kind": "metric", "dataset_id": 11, "metric_id": 19},
        ],
        lambda dataset_id: dataset if dataset_id == 11 else None,
        lambda _dataset, _metric: True,
    )

    assert (links[0]["column_name"], links[0]["metric_id"]) == ("quan_sat_id", None)
    assert (links[1]["column_name"], links[1]["metric_id"]) == (None, 19)


@pytest.mark.parametrize(
    "link",
    [
        {"kind": "column", "dataset_id": 11, "column_name": "missing"},
        {"kind": "metric", "dataset_id": 11, "metric_id": 999},
        {"kind": "dataset", "dataset_id": 99},
    ],
)
def test_validated_links_rejects_missing_live_targets(link):
    dataset = _weather_dataset()
    with pytest.raises(GlossaryValidationError, match="Invalid glossary link"):
        validated_links([link], lambda dataset_id: dataset if dataset_id == 11 else None, lambda *_: True)


def test_a_term_may_link_only_approved_metrics():
    dataset = _weather_dataset()
    link = {"kind": "metric", "dataset_id": 11, "metric_id": 19}

    with pytest.raises(GlossaryValidationError, match="approved metrics"):
        validated_links([link], lambda _: dataset, lambda _dataset, _metric: False)
    assert validated_links([link], lambda _: dataset, lambda _dataset, _metric: True)


def test_lifecycle_mutations_lock_the_term_and_its_current_revision():
    for path in REPOSITORY_PATHS:
        assert "with_for_update()" in path.read_text(encoding="utf-8")
    source = SERVICE_PATH.read_text(encoding="utf-8")
    locked = next(
        node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "_locked_current"
    )
    assert ".lock(" in ast.unparse(locked) and "lock_current(" in ast.unparse(locked)


def _sqlite_service(users=(7, 8)):
    """The real service, repositories and dependencies on disposable SQLite tables; Superset is never imported."""
    base = declarative_base()

    class GlossaryTermGroup(base):
        __tablename__ = "glossary_term_group"
        id = Column(Integer, primary_key=True)
        name = Column(String, nullable=False)
        normalized_name = Column(String, nullable=False, unique=True)
        description = Column(String)

    class GlossaryTerm(base):
        __tablename__ = "glossary_term"
        id = Column(Integer, primary_key=True)
        canonical_name = Column(String, nullable=False)
        normalized_name = Column(String, nullable=False, unique=True)
        steward_id = Column(Integer)
        group_id = Column(Integer, ForeignKey("glossary_term_group.id"))
        group = relationship(GlossaryTermGroup)

    class GlossaryTermRevision(base):
        __tablename__ = "glossary_term_revision"
        __table_args__ = (UniqueConstraint("term_id", "revision_number"),)
        id = Column(Integer, primary_key=True)
        term_id = Column(Integer, ForeignKey("glossary_term.id"), nullable=False)
        revision_number = Column(Integer, nullable=False)
        definition = Column(String, nullable=False)
        synonyms = Column(JSON, nullable=False)
        content_hash = Column(String, nullable=False)
        status = Column(String, nullable=False)
        is_current = Column(Boolean, nullable=False)
        approver_id = Column(Integer)
        approved_at = Column(DateTime)
        revoker_id = Column(Integer)
        revoked_at = Column(DateTime)
        term = relationship(GlossaryTerm)

    class GlossaryTermLink(base):
        __tablename__ = "glossary_term_link"
        id = Column(Integer, primary_key=True)
        revision_id = Column(Integer, nullable=False)
        kind = Column(String, nullable=False)
        link_key = Column(String, nullable=False)
        dataset_id = Column(Integer, nullable=False)
        column_name = Column(String)
        metric_id = Column(Integer)
        fingerprint = Column(String, nullable=False)

    class GlossaryTermAuditEvent(base):
        __tablename__ = "glossary_term_audit_event"
        id = Column(Integer, primary_key=True)
        term_id = Column(Integer, nullable=False)
        revision_id = Column(Integer, nullable=False)
        event_type = Column(String, nullable=False)
        actor_id = Column(Integer)
        reason = Column(String)
        revision_number = Column(Integer, nullable=False)
        occurred_at = Column(DateTime, nullable=False)

    engine = create_engine("sqlite:///:memory:")
    base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    dataset = SimpleNamespace(id=7, table_name="daily_weather", columns=[], metrics=[])
    namespace = {
        "db": SimpleNamespace(session=session),
        "GlossaryTerm": GlossaryTerm,
        "GlossaryTermGroup": GlossaryTermGroup,
        "GlossaryTermRevision": GlossaryTermRevision,
        "GlossaryTermLink": GlossaryTermLink,
        "GlossaryTermAuditEvent": GlossaryTermAuditEvent,
        "datetime": datetime,
        "timezone": timezone,
        "IntegrityError": IntegrityError,
        "security_manager": SimpleNamespace(get_user_by_id=lambda user_id: object() if user_id in users else None),
        "get_dataset": lambda dataset_id: dataset if dataset_id == 7 else None,
        "glossary_metric_approved": lambda _dataset, _metric: True,
        **{name: getattr(validation, name) for name in VALIDATION_HELPERS},
    }
    service = [
        node
        for path in (GROUP_SERVICE_PATH, SERVICE_PATH)
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.Assign))
    ]
    compile_into(namespace, *service)
    return SimpleNamespace(**namespace), session


def test_an_edit_of_an_approved_term_keeps_the_approved_revision_serving_until_its_draft_is_approved():
    service, session = _sqlite_service()
    revisions = service.glossary_term_revision_repository()
    first = service.create_draft(_payload(steward_id=8), 7)
    session.commit()
    term_id = first.term_id
    assert revisions.serving_for(term_id) is None

    service.approve_revision(term_id, 1, 8, None, True)
    session.commit()
    draft = service.update_draft(term_id, _payload(definition="Revenue net of returns."), 1, 7)
    session.commit()

    assert (draft.revision_number, draft.status, draft.is_current) == (2, "draft", True)
    assert revisions.serving_for(term_id).revision_number == 1
    assert [revision.revision_number for revision in revisions.serving()] == [1]

    service.approve_revision(term_id, 2, 8, None, True)
    session.commit()
    assert revisions.serving_for(term_id).revision_number == 2
    assert [revision.revision_number for revision in revisions.serving()] == [2]


def test_revoking_while_an_edit_waits_stops_the_serving_revision_and_keeps_the_draft():
    service, session = _sqlite_service()
    revisions = service.glossary_term_revision_repository()
    term_id = service.create_draft(_payload(), 7).term_id
    service.approve_revision(term_id, 1, 8, None, False)
    service.update_draft(term_id, _payload(definition="Revenue net of returns."), 1, 7)
    session.commit()

    with pytest.raises(GlossaryValidationError, match="reason"):
        service.revoke_revision(term_id, 2, 8, None)
    current = service.revoke_revision(term_id, 2, 8, "Định nghĩa sai.")
    session.commit()

    assert (current.revision_number, current.status) == (2, "draft")
    assert revisions.serving_for(term_id) is None and revisions.serving() == []
    events = service.glossary_term_audit_event_repository().for_term(term_id)
    assert [(event.event_type, event.revision_number) for event in events] == [
        ("created", 1),
        ("approved", 1),
        ("updated", 2),
        ("revoked", 1),
    ]
    with pytest.raises(GlossaryValidationError, match="approved revision"):
        service.revoke_revision(term_id, 2, 8, "Lần nữa.")


def test_a_revoked_term_is_revised_by_a_new_draft_and_served_again_after_approval():
    service, session = _sqlite_service()
    revisions = service.glossary_term_revision_repository()
    term_id = service.create_draft(_payload(), 7).term_id
    service.approve_revision(term_id, 1, 8, None, False)
    service.revoke_revision(term_id, 1, 8, "Sai.")
    session.commit()

    service.update_draft(term_id, _payload(definition="Corrected."), 1, 7)
    service.approve_revision(term_id, 2, 8, None, False)
    session.commit()
    assert revisions.serving_for(term_id).definition == "Corrected."


def test_stale_revisions_names_and_stewards_are_refused():
    service, session = _sqlite_service()
    term_id = service.create_draft(_payload(), 7).term_id
    session.commit()

    with pytest.raises(GlossaryConflictError, match="changed"):
        service.update_draft(term_id, _payload(), 2, 7)
    with pytest.raises(GlossaryConflictError, match="changed"):
        service.approve_revision(term_id, 3, 8, None, False)
    with pytest.raises(GlossaryConflictError, match="already exists"):
        service.create_draft(_payload(canonical_name=" revenue "), 7)
    with pytest.raises(GlossaryValidationError, match="canonical name cannot be changed"):
        service.update_draft(term_id, _payload(canonical_name="Sales"), 1, 7)
    with pytest.raises(GlossaryValidationError, match="Unknown steward_id"):
        service.update_draft(term_id, _payload(steward_id=99), 1, 7)
    with pytest.raises(GlossaryTermNotFoundError):
        service.approve_revision(404, 1, 8, None, False)


def test_the_editor_names_the_steward():
    service, session = _sqlite_service()
    term_id = service.create_draft(_payload(), 7).term_id
    service.update_draft(term_id, _payload(steward_id=8), 1, 7)
    session.commit()
    assert service.glossary_term_repository().get(term_id).steward_id == 8


@pytest.mark.parametrize("require_second_reviewer", [False, True])
def test_the_author_may_approve_their_own_draft_unless_a_second_reviewer_is_required(require_second_reviewer):
    service, session = _sqlite_service()
    term_id = service.create_draft(_payload(), 7).term_id
    session.commit()

    if require_second_reviewer:
        with pytest.raises(GlossarySelfApprovalError):
            service.approve_revision(term_id, 1, 7, None, True)
        assert service.glossary_term_revision_repository().current_for(term_id).status == "draft"
    else:
        assert service.approve_revision(term_id, 1, 7, None, False).approver_id == 7


def test_only_the_current_draft_can_be_approved():
    service, session = _sqlite_service()
    term_id = service.create_draft(_payload(), 7).term_id
    service.approve_revision(term_id, 1, 8, None, False)
    session.commit()
    with pytest.raises(GlossaryValidationError, match="Only the current draft"):
        service.approve_revision(term_id, 1, 8, None, False)


def test_a_group_has_a_trimmed_name_unique_without_case_and_an_optional_description():
    assert validation.normalize_group_payload({"name": "  Nhiệt   độ ", "description": "  "}) == {
        "name": "Nhiệt độ",
        "normalized_name": "nhiệt độ",
        "description": None,
    }
    for payload in (None, {}, {"name": "  "}, {"name": "x" * 256}, {"name": "A", "color": "red"}):
        with pytest.raises(GlossaryValidationError):
            validation.normalize_group_payload(payload)
    assert validation.group_id_of({"group_id": None}) is None and validation.group_id_of({"group_id": 3}) == 3
    with pytest.raises(GlossaryValidationError, match="Nhóm thuật ngữ không hợp lệ"):
        validation.group_id_of({"group_id": True})


def test_groups_are_created_renamed_and_refuse_a_name_another_group_has():
    service, session = _sqlite_service()
    heat = service.create_group({"name": "Nhiệt độ", "description": "Các cách gọi nhiệt độ"})
    rain = service.create_group({"name": "Mưa"})
    session.commit()

    with pytest.raises(GlossaryConflictError, match="cùng tên"):
        service.create_group({"name": " nhiệt  ĐỘ "})
    with pytest.raises(GlossaryConflictError, match="cùng tên"):
        service.update_group(rain.id, {"name": "Nhiệt độ"})
    session.rollback()
    renamed = service.update_group(heat.id, {"name": "Nhiệt độ không khí", "description": None})
    session.commit()
    assert (renamed.name, renamed.description) == ("Nhiệt độ không khí", None)
    with pytest.raises(GlossaryTermGroupNotFoundError):
        service.update_group(404, {"name": "Khác"})


def test_moving_an_approved_term_keeps_it_serving_without_a_new_revision_and_audits_the_move():
    service, session = _sqlite_service()
    heat = service.create_group({"name": "Nhiệt độ"})
    rain = service.create_group({"name": "Mưa"})
    term_id = service.create_draft(_payload(group_id=heat.id), 7).term_id
    service.approve_revision(term_id, 1, 8, None, False)
    session.commit()
    assert service.glossary_term_repository().get(term_id).group_id == heat.id

    service.move_term(term_id, rain.id, 8)
    service.move_term(term_id, rain.id, 8)
    service.move_term(term_id, None, 8)
    session.commit()

    revisions = service.glossary_term_revision_repository()
    assert [revision.revision_number for revision in revisions.for_term(term_id)] == [1]
    assert revisions.serving_for(term_id).status == "approved"
    events = service.glossary_term_audit_event_repository().for_term(term_id)
    assert [(event.event_type, event.reason) for event in events] == [
        ("created", None),
        ("approved", None),
        ("moved", "Nhiệt độ → Mưa"),
        ("moved", "Mưa → Chưa phân nhóm"),
    ]
    with pytest.raises(GlossaryTermGroupNotFoundError):
        service.move_term(term_id, 404, 8)
    with pytest.raises(GlossaryTermNotFoundError):
        service.move_term(404, rain.id, 8)


def test_an_edit_moves_the_term_at_once_while_its_draft_waits_and_keeps_the_group_when_it_names_none():
    service, session = _sqlite_service()
    heat = service.create_group({"name": "Nhiệt độ"})
    term_id = service.create_draft(_payload(), 7).term_id
    service.approve_revision(term_id, 1, 8, None, False)
    service.update_draft(term_id, _payload(definition="Revenue net of returns.", group_id=heat.id), 1, 7)
    session.commit()
    assert service.glossary_term_repository().get(term_id).group_id == heat.id
    assert service.glossary_term_revision_repository().serving_for(term_id).revision_number == 1

    service.update_draft(term_id, _payload(definition="Revenue after returns."), 2, 7)
    session.commit()
    assert service.glossary_term_repository().get(term_id).group_id == heat.id
    with pytest.raises(GlossaryTermGroupNotFoundError):
        service.create_draft(_payload(canonical_name="Sales", group_id=404), 7)


def test_deleting_a_group_leaves_its_terms_ungrouped_and_approved():
    service, session = _sqlite_service()
    heat = service.create_group({"name": "Nhiệt độ"})
    first = service.create_draft(_payload(group_id=heat.id), 7).term_id
    second = service.create_draft(_payload(canonical_name="Sales", group_id=heat.id), 7).term_id
    service.approve_revision(first, 1, 8, None, False)
    session.commit()

    assert service.delete_group(heat.id, 8) == [first, second]
    session.commit()

    terms = service.glossary_term_repository()
    assert terms.get(first).group_id is None and terms.get(second).group_id is None
    assert service.glossary_term_group_repository().by_name() == []
    assert service.glossary_term_revision_repository().serving_for(first).revision_number == 1
    events = service.glossary_term_audit_event_repository().for_term(second)
    assert (events[-1].event_type, events[-1].reason) == ("moved", "Nhiệt độ → Chưa phân nhóm")
    with pytest.raises(GlossaryTermGroupNotFoundError):
        service.delete_group(heat.id, 8)
