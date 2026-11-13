import json
import re
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from queue import Queue
from typing import Any, Literal

from pydantic import BaseModel, Field

from ....common.utils.type_formatter import iso_datetime
from ...semantic_layer.exceptions import SemanticLayerRefusedError
from ...semantic_layer.services.catalog import SemanticCatalog
from ...semantic_layer.services.data_model import DataModelSources
from ...semantic_layer.services.metrics import catalog_metric
from ..exceptions import QueryPlanError
from . import prompts
from .formula import answers_anything, doubtful_result, told_formula
from .query import combined_result, plan_query, result_for_answer
from .triage import TRIAGE_STATUSES, QuestionTriage, SubQuestion, blocked_reply, checked_triage

MAX_HISTORY_TURNS = 3
MAX_HISTORY_MESSAGE_LENGTH = 2000
MISSING_KINDS = ("glossary", "metric", "expert_rule")
WEEKDAYS = ("thứ Hai", "thứ Ba", "thứ Tư", "thứ Năm", "thứ Sáu", "thứ Bảy", "Chủ nhật")
# Where a sequential sub-question uses what an earlier one found: {{q1}} is the first row of sub-question 1.
PLACEHOLDER = re.compile(r"\{\{\s*q(\d+)\s*\}\}")
PART_FAILED_MESSAGE = "Không trả lời được phần này do lỗi khi xử lý."
PART_SKIPPED_MESSAGE = "Phần trước không có kết quả để dùng tiếp, nên phần này chưa trả lời được."

Event = tuple[str, dict[str, Any]]
# Runs one approved metric: (dataset_id, metric_id, {dimensions, filters}) -> the Semantic Layer's answer.
MetricRunner = Callable[[int, int, dict[str, Any]], dict[str, Any]]


class MetricReference(BaseModel):
    dataset_id: int
    metric_id: int


class QueryFilter(BaseModel):
    column: str
    operator: Literal["==", "!=", "<", "<=", ">", ">=", "IN", "NOT IN"]
    value: int | float | str | list[int | float | str]
    source: Literal["time", "place", "user_definition", "question", "expert_rule"]


class QueryOrder(BaseModel):
    metric_id: int
    direction: Literal["desc", "asc"] = "desc"


class ComparedPeriod(BaseModel):
    start: str = Field(description="YYYY-MM-DD")
    end: str = Field(description="YYYY-MM-DD")
    label: str = Field(default="", description="Tên ngắn của kỳ đem so, ví dụ \"tuần trước\".")


class QueryPlan(BaseModel):
    dataset_id: int
    metric_ids: list[int]
    dimensions: list[str] = Field(default_factory=list)
    filters: list[QueryFilter] = Field(default_factory=list)
    order_by: QueryOrder | None = None
    limit: int | None = None
    comparison: ComparedPeriod | None = None
    time_grain: Literal["hour", "day", "week", "month", "quarter", "year"] | None = None
    presentation: Literal["number", "table", "bar", "line"] = "table"
    scope: str = Field(default="", description="Phạm vi bằng lời, ví dụ \"Hà Nội, 19/10–25/10/2026\".")


class Understanding(BaseModel):
    standalone_question: str = Field(description="Câu hỏi đầy đủ, tự đứng được một mình.")
    term_ids: list[int] = Field(default_factory=list)
    user_definition_ids: list[int] = Field(default_factory=list)
    undefined_concepts: list[str] = Field(default_factory=list)
    metrics: list[MetricReference] = Field(default_factory=list)
    question_kind: Literal["qualitative", "quantitative"]
    qualitative_concepts: list[str] = Field(default_factory=list)
    rule_ids: list[int] = Field(default_factory=list)
    user_rule_ids: list[int] = Field(default_factory=list)
    query: QueryPlan | None = None
    reason: str = ""


class DefinitionOption(BaseModel):
    label: str = Field(description="Nhãn ngắn của lựa chọn, ví dụ \"Từ 39 km/h (cấp 6)\".")
    definition: str = Field(description="Định nghĩa đầy đủ nếu người dùng chọn lựa chọn này.")


class DefinitionDraft(BaseModel):
    kind: Literal["glossary", "expert_rule"]
    concept: str = Field(description="Khái niệm trong câu hỏi.")
    name: str
    question: str = Field(description="Câu hỏi ngắn gửi người dùng.")
    options: list[DefinitionOption] = Field(description="2 đến 4 cách hiểu, cách phổ biến nhất đứng đầu.")


class MetricProposal(BaseModel):
    name: str
    definition: str
    unit: str = ""


class MissingProposals(BaseModel):
    definitions: list[DefinitionDraft] = Field(default_factory=list)
    metric: MetricProposal | None = None


class MetricDraft(BaseModel):
    dataset_id: int | None = None
    metric_name: str = ""
    label: str = ""
    expression: str = ""
    definition: str = ""
    unit: str = ""
    query: QueryPlan | None = None
    reason: str = ""


class DatasetMetricDraft(BaseModel):
    metric_name: str
    label: str
    expression: str
    definition: str
    unit: str = ""


class DatasetDraft(BaseModel):
    name: str = ""
    label: str = ""
    description: str = ""
    sql: str = ""
    metric: DatasetMetricDraft | None = None
    reason: str = ""


@dataclass
class MetricBuilder:
    """The data model steps taken when no approved metric answers: read its aggregate datasets, save a built metric."""

    load_sources: Callable[[], DataModelSources]
    # Saves a metric draft; returns the metric in the Semantic Layer's shape and whether it still waits for approval.
    create_metric: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]]
    # Keeps a dataset drafted over base datasets for an administrator; returns the proposal.
    propose_dataset: Callable[[dict[str, Any]], dict[str, Any]] | None = None


@dataclass
class AgentOutcome:
    # How the question was classified before anything was looked up (docs/specs/taxonomy-cau-hoi.md).
    triage: dict[str, Any] | None = None
    rewritten_question: str | None = None
    glossary_terms: list[dict[str, Any]] = field(default_factory=list)
    # Business concepts the question uses that no approved term, metric or rule defines; only these make a term missing.
    undefined_concepts: list[str] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    expert_rules: list[dict[str, Any]] = field(default_factory=list)
    # The user's own definitions on this dashboard that the question used.
    user_definitions: list[dict[str, Any]] = field(default_factory=list)
    requires_expert_rule: bool = True
    suggestions: dict[str, dict[str, Any]] = field(default_factory=dict)
    clarifications: list[dict[str, Any]] = field(default_factory=list)
    # The table the metric query answered with, or why it could not run.
    result: dict[str, Any] | None = None
    query_error: str | None = None
    answer: str = ""
    # A question split into sub-questions: each one answered (`parts`), and what each showed, in order, including the
    # ones that failed or were skipped (`part_results`). The fields above then gather what the parts found.
    parts: list["AgentOutcome"] = field(default_factory=list)
    part_results: list[dict[str, Any]] = field(default_factory=list)

    @property
    def missing(self) -> list[str]:
        """What the semantic layer lacks for this question.

        A glossary term is missing only for a concept nothing defines: a question fully described by an approved
        metric, such as "nhiệt độ trung bình tháng này", needs no term of its own. A concept the user confirmed on this
        dashboard counts as the question's expert rule. A question answered without a query lacks nothing, and one split
        into sub-questions lacks what any of them lacks.
        """
        if not self.answered_with_data:
            return []
        if self.parts:
            return [kind for kind in MISSING_KINDS if any(kind in part.missing for part in self.parts)]
        has_user_rule = any(item["kind"] == "expert_rule" for item in self.user_definitions)
        gaps = {
            "glossary": bool(self.undefined_concepts),
            # A metric the agent built answers provisionally, but stays missing until an administrator approves it.
            "metric": all(metric.get("provisional") for metric in self.metrics),
            "expert_rule": self.requires_expert_rule and not self.expert_rules and not has_user_rule,
        }
        return [kind for kind in MISSING_KINDS if gaps[kind]]

    @property
    def answered_with_data(self) -> bool:
        return self.triage is None or self.triage["triage"] == "answerable"

    @property
    def status(self) -> str:
        if not self.answered_with_data:
            return TRIAGE_STATUSES[self.triage["triage"]]
        return "needs_review" if self.missing else "resolved"


def recent_history(history: Any) -> list[dict[str, str]]:
    """The last few user and assistant turns, trimmed; anything malformed from the browser is dropped."""
    if not isinstance(history, list):
        return []
    messages = [
        {"role": item["role"], "content": item["content"].strip()[:MAX_HISTORY_MESSAGE_LENGTH]}
        for item in history
        if isinstance(item, dict) and item.get("role") in ("user", "assistant")
        and isinstance(item.get("content"), str) and item["content"].strip()
    ]
    return messages[-MAX_HISTORY_TURNS * 2:]


def _data_block(label: str, value: Any) -> str:
    return f"{label}:\n```json\n{json.dumps(value, ensure_ascii=False, indent=1, default=str)}\n```"


def _described(items: list[dict[str, Any]], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    """Catalog items reduced to the fields a step needs to judge them."""
    return [{key: item.get(key) for key in fields if item.get(key) not in (None, "", [])} for item in items]


TERM_FIELDS = ("id", "name", "synonyms", "definition", "metrics", "columns", "dataset_ids")
USER_DEFINITION_FIELDS = ("id", "kind", "name", "concept", "definition", "conditions")
METRIC_FIELDS = ("dataset_id", "metric_id", "name", "definition", "unit", "glossary_term_ids", "query_defaults")
RULE_FIELDS = ("id", "name", "dataset_id", "conditions", "exceptions", "metric_ids", "glossary_term_ids")


def _known(ids: list[int], items_by_id: dict[int, dict[str, Any]]) -> list[int]:
    """The IDs the model returned that are real candidates, de-duplicated in the model's order."""
    return [item_id for item_id in dict.fromkeys(ids) if item_id in items_by_id]


def _tool_call(
    index: int, name: str, arguments: dict[str, Any], action: Callable[[], Any], shown: Callable[[Any], Any]
):
    """Run one step as a reported tool call; `shown` turns the step's return value into the result the UI displays."""
    call_id = f"call_{index}"
    yield "tool_start", {"id": call_id, "name": name, "arguments": arguments}
    started = time.monotonic()
    try:
        value = action()
    except Exception as error:
        yield "tool_end", {
            "id": call_id, "name": name, "status": "error", "error": str(error) or type(error).__name__,
            "duration_ms": round((time.monotonic() - started) * 1000),
        }
        raise
    yield "tool_end", {
        "id": call_id, "name": name, "status": "ok", "result": shown(value),
        "duration_ms": round((time.monotonic() - started) * 1000),
    }
    return value


def _ask(model: Any, schema: type[BaseModel], system_prompt: str, user_content: str) -> Any:
    return model.with_structured_output(schema).invoke([("system", system_prompt), ("human", user_content)])


MAX_OPTIONS = 4


def _clarifications(proposals: MissingProposals, missing: list[str]) -> list[dict[str, Any]]:
    """The questions the user is asked: only for a missing term or rule, named, with at least one usable option."""
    drafts: dict[tuple[str, str], dict[str, Any]] = {}
    for draft in proposals.definitions:
        name = draft.name.strip()
        options = [
            {"label": option.label.strip(), "definition": option.definition.strip()}
            for option in draft.options if option.label.strip() and option.definition.strip()
        ][:MAX_OPTIONS]
        if draft.kind in missing and name and options:
            drafts.setdefault((draft.kind, name.casefold()), {
                "kind": draft.kind, "concept": draft.concept.strip(), "name": name,
                "question": draft.question.strip() or f"Bạn hiểu “{draft.concept.strip() or name}” là gì?",
                "options": options,
            })
    return list(drafts.values())


def _suggestions(proposals: MissingProposals, missing: list[str]) -> dict[str, dict[str, Any]]:
    """The metric proposed to administrators, when a metric is missing and the model named one."""
    metric = proposals.metric
    return {"metric": metric.model_dump()} if "metric" in missing and metric and metric.name.strip() else {}


def _proposals_shown(proposals: MissingProposals, missing: list[str]) -> dict[str, Any]:
    return {"clarifications": _clarifications(proposals, missing), **_suggestions(proposals, missing)}


def _today_block(today: date) -> str:
    return _data_block("Hôm nay", {"today": iso_datetime(today), "weekday": WEEKDAYS[today.weekday()]})


def _understanding_shown(
    value: Understanding,
    terms_by_id: dict[int, dict[str, Any]],
    metrics_by_key: dict[tuple[int, int], dict[str, Any]],
    rules_by_id: dict[int, dict[str, Any]],
    definitions_by_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    qualitative = value.question_kind == "qualitative"
    rule_ids, user_rule_ids = (value.rule_ids, value.user_rule_ids) if qualitative else ([], [])
    return {
        "standalone_question": value.standalone_question,
        "glossary_terms": [terms_by_id[term_id]["name"] for term_id in _known(value.term_ids, terms_by_id)],
        "undefined_concepts": value.undefined_concepts,
        "metrics": [
            metrics_by_key[(item.dataset_id, item.metric_id)]["name"]
            for item in value.metrics if (item.dataset_id, item.metric_id) in metrics_by_key
        ],
        "question_kind": value.question_kind,
        "qualitative_concepts": value.qualitative_concepts,
        "expert_rules": [rules_by_id[rule_id]["name"] for rule_id in _known(rule_ids, rules_by_id)],
        "user_definitions": [
            definitions_by_id[item_id]["name"]
            for item_id in _known(value.user_definition_ids + user_rule_ids, definitions_by_id)
        ],
        "query": value.query.model_dump() if value.query else None,
        "reason": value.reason,
    }


def _run_metrics(planned: Any, run_metric: MetricRunner) -> dict[str, Any]:
    """Each metric is its own governed query, once per period; they share dimensions, so they all run side by side."""
    periods = [False, True] if planned.comparison else [False]
    calls = [(compared, metric) for compared in periods for metric in planned.metrics]
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        answers = list(pool.map(
            lambda call: run_metric(planned.dataset_id, call[1]["metric_id"], planned.request_body(compared=call[0])),
            calls,
        ))
    count = len(planned.metrics)
    return combined_result(planned, answers[:count], answers[count:] if planned.comparison else None)


def _stored_conditions(definitions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The user's definitions that carry stored conditions, in the shape the query planner adds them."""
    return [
        {"name": item["name"], "dataset_id": item["conditions"]["dataset_id"], "filters": item["conditions"]["filters"],
         "source": "user_definition"}
        for item in definitions if item.get("conditions")
    ]


def _query_shown(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "scope": result["scope"],
        "filters": [
            {key: item[key] for key in ("column", "operator", "shown_value", "source")} for item in result["filters"]
        ],
        "dimensions": [dimension["name"] for dimension in result["dimensions"]],
        "metrics": [metric["label"] for metric in result["metrics"]],
        "order": result["order"],
        "limit": result["limit"],
        "comparison": result["comparison"],
        "coverage": result["coverage"],
        "row_count": result["row_count"],
        "rows": result["rows"][:5],
    }


def _draft_shown(draft: MetricDraft) -> dict[str, Any]:
    if draft.dataset_id is None:
        return {"dataset_id": None, "reason": draft.reason}
    return {key: getattr(draft, key) for key in ("dataset_id", "label", "expression", "unit", "reason")}


def _built_plan(draft: MetricDraft, metric_id: int) -> dict[str, Any] | None:
    """The draft's query, aimed at the metric the Semantic Layer saved."""
    if draft.query is None:
        return None
    plan = {**draft.query.model_dump(), "dataset_id": draft.dataset_id, "metric_ids": [metric_id]}
    if plan.get("order_by"):
        plan["order_by"] = {**plan["order_by"], "metric_id": metric_id}
    return plan


def _build_metric(
    step: int,
    sources: DataModelSources,
    asked: str,
    found: dict[str, Any],
    today: date,
    model: Any,
    builder: MetricBuilder,
    run_metric: MetricRunner,
    rules: list[dict[str, Any]],
    outcome: AgentOutcome,
):
    """Step 3: build a metric on an aggregate dataset and answer with it, provisionally until it is approved.

    Returns the next step number and the metric used, or None when no aggregate dataset measures what was asked.
    """
    if not sources.aggregates:
        return step, None
    draft_input = "\n\n".join((
        _today_block(today),
        f"Câu hỏi: {asked}",
        _data_block("Nội dung nghiệp vụ đã xác định", found),
        _data_block("Dataset tổng hợp: cột, giá trị có nhãn và metric đã có", sources.aggregates),
    ))
    draft = yield from _tool_call(
        step, "draft_metric", {"question": asked},
        lambda: _ask(model, MetricDraft, prompts.DRAFT_METRIC, draft_input), _draft_shown,
    )
    step += 1
    dataset = next((item for item in sources.aggregates if item["id"] == draft.dataset_id), None)
    if dataset is None or not draft.expression.strip():
        return step, None
    body = {
        "dataset_id": dataset["id"], "metric_name": draft.metric_name, "label": draft.label,
        "expression": draft.expression, "definition": draft.definition, "unit": draft.unit or None, "question": asked,
    }
    drafted = {"name": draft.label, "definition": draft.definition, "unit": draft.unit}
    try:
        created, provisional = yield from _tool_call(
            step, "create_metric", {key: body[key] for key in ("dataset_id", "label", "expression")},
            lambda: builder.create_metric(body),
            lambda value: {"metric": value[0]["metric_name"], "provisional": value[1]},
        )
        step += 1
        metric = catalog_metric(created, dataset.get("name"), provisional)
        outcome.metrics = [
            {key: metric[key] for key in ("dataset_id", "metric_id", "name", "provisional")},
        ]
        planned = plan_query(_built_plan(draft, metric["metric_id"]), [dataset], [metric], rules)
        outcome.result = yield from _tool_call(
            step, "query_metrics", {"plan": _built_plan(draft, metric["metric_id"])},
            lambda: _run_metrics(planned, run_metric), _query_shown,
        )
        outcome.result["provisional"] = provisional
        if provisional:
            # The asker sees how the figure was computed, and is warned when it looks like a wrong formula.
            expression = created.get("expression") or draft.expression
            outcome.result["formula"] = {
                "expression": expression, "parts": told_formula(expression, dataset.get("columns", [])),
            }
            outcome.result["doubt"] = _confirmed_doubt(outcome.result, metric, run_metric)
        yield "result", outcome.result
    except (QueryPlanError, SemanticLayerRefusedError) as error:
        outcome.query_error = str(error)
    if not outcome.metrics:
        # The Semantic Layer refused the metric: an administrator gets it as a proposal instead.
        outcome.suggestions = {"metric": drafted}
        return step + 1, drafted
    return step + 1, metric


def _confirmed_doubt(result: dict[str, Any], metric: dict[str, Any], run_metric: MetricRunner) -> str | None:
    """Doubt a provisional figure of nothing or zero only when the metric gives nothing over all the data either."""
    doubt = doubtful_result(result)
    if doubt is None:
        return None
    try:
        whole = run_metric(metric["dataset_id"], metric["metric_id"], {"dimensions": [], "filters": []})
    except SemanticLayerRefusedError:
        return None
    return None if answers_anything(whole, metric["metric_name"]) else doubt


def _propose_dataset(
    step: int,
    sources: DataModelSources,
    asked: str,
    found: dict[str, Any],
    today: date,
    model: Any,
    builder: MetricBuilder,
    outcome: AgentOutcome,
):
    """Step 4: no aggregate dataset measures it, so draft a dataset over base datasets for an administrator to create.

    Nothing runs: the asker is told a dataset was proposed. Returns the next step number.
    """
    if builder.propose_dataset is None:
        return step
    draft_input = "\n\n".join((
        _today_block(today),
        f"Câu hỏi: {asked}",
        _data_block("Nội dung nghiệp vụ đã xác định", found),
        _data_block("Dataset cơ sở: cột và khoá liên kết", sources.bases),
    ))
    draft = yield from _tool_call(
        step, "draft_dataset", {"question": asked},
        lambda: _ask(model, DatasetDraft, prompts.DRAFT_DATASET, draft_input),
        lambda value: value.model_dump(),
    )
    step += 1
    if not draft.sql.strip() or draft.metric is None:
        return step
    body = {**draft.model_dump(exclude={"reason"}), "question": asked}
    try:
        proposal = yield from _tool_call(
            step, "propose_dataset", {"name": draft.name, "label": draft.label},
            lambda: builder.propose_dataset(body),
            lambda value: {key: value.get(key) for key in ("id", "name", "status")},
        )
    except SemanticLayerRefusedError as error:
        outcome.query_error = str(error)
        return step + 1
    outcome.suggestions = {"dataset": {
        "proposal_id": proposal.get("id"), "label": proposal.get("label") or draft.label,
        "description": draft.description, "metric": draft.metric.label,
    }}
    return step + 1


def _conversation(history: list[dict[str, str]]) -> str:
    return "\n".join(f"{item['role']}: {item['content']}" for item in history) or "(chưa có)"


def _triage_input(question: str, history: list[dict[str, str]], dashboard: str | None) -> str:
    return "\n\n".join((
        f"Dashboard: {dashboard or '(không rõ tên)'}",
        f"Lịch sử hội thoại:\n{_conversation(history)}",
        f"Câu hỏi mới nhất: {question}",
    ))


def _streamed_answer(model: Any, messages: list[tuple[str, str]], outcome: AgentOutcome) -> Iterator[Event]:
    for chunk in model.stream(messages):
        text = chunk.content if isinstance(chunk.content, str) else ""
        if text:
            outcome.answer += text
            yield "token", {"text": text}


def _answer_without_query(
    triage: QuestionTriage,
    question: str,
    history: list[dict[str, str]],
    dashboard: str | None,
    model: Any,
    load_catalog: Callable[[], SemanticCatalog],
    outcome: AgentOutcome,
) -> Iterator[Event]:
    """Answer a question no metric query answers: refuse a blocked one, describe the data, or reply in a sentence.

    A blocked question gets a fixed reply and reaches no model; only a question about the data reads the catalog.
    """
    outcome.requires_expert_rule = False
    if triage.triage == "blocked":
        outcome.answer = blocked_reply(triage)
        yield "token", {"text": outcome.answer}
        return
    if triage.triage == "metadata":
        catalog = yield from _tool_call(2, "load_semantic_catalog", {}, load_catalog, SemanticCatalog.summary)
        context = {
            "dashboard": dashboard,
            "conversation": _conversation(history),
            "question": question,
            "metrics": _described(catalog.metrics, ("name", "definition", "unit", "query_defaults")),
            "glossary_terms": _described(catalog.glossary_terms, ("name", "synonyms", "definition")),
            "datasets": _described(catalog.datasets, ("name", "time_ranges")),
        }
        messages = [("system", prompts.DATA_DESCRIPTION), ("human", _data_block("Câu hỏi và catalog", context))]
    else:
        context = {"triage": triage.triage, "dashboard": dashboard, "question": question}
        messages = [("system", prompts.DECLINED_REPLY), ("human", _data_block("Câu hỏi", context))]
    yield from _streamed_answer(model, messages, outcome)


def _answer_question(
    first_step: int,
    question: str,
    history: list[dict[str, str]],
    catalog: SemanticCatalog,
    model: Any,
    user_definitions: Sequence[dict[str, Any]],
    run_metric: MetricRunner | None,
    today: date,
    builder: MetricBuilder | None,
    outcome: AgentOutcome,
):
    """Understand one single-intent question against the catalog, then answer it with approved metrics or say what is
    missing. Its steps are numbered from `first_step`; returns the context the final answer is written from.

    With a `builder`, a question that only lacks a metric climbs to the data model and builds one.
    """
    definitions_by_id = {item["id"]: item for item in user_definitions}
    user_rules_by_id = {item_id: item for item_id, item in definitions_by_id.items() if item["kind"] == "expert_rule"}
    terms_by_id = {term["id"]: term for term in catalog.glossary_terms}
    metrics_by_key = {(metric["dataset_id"], metric["metric_id"]): metric for metric in catalog.metrics}
    rules_by_id = {rule["id"]: rule for rule in catalog.expert_rules}

    understanding_input = "\n\n".join((
        _today_block(today),
        f"Lịch sử hội thoại:\n{_conversation(history)}",
        f"Câu hỏi mới nhất: {question}",
        _data_block("Thuật ngữ đã duyệt", _described(catalog.glossary_terms, TERM_FIELDS)),
        _data_block("Metric đã duyệt", _described(catalog.metrics, METRIC_FIELDS)),
        _data_block("Expert rule đã duyệt", _described(catalog.expert_rules, RULE_FIELDS)),
        _data_block("Cách hiểu người dùng đã xác nhận", _described(list(user_definitions), USER_DEFINITION_FIELDS)),
        _data_block("Dataset: cột và giá trị có nhãn", catalog.datasets),
    ))
    understanding = yield from _tool_call(
        first_step, "understand_question", {"question": question, "history_turns": len(history) // 2},
        lambda: _ask(model, Understanding, prompts.UNDERSTAND_QUESTION, understanding_input),
        lambda value: _understanding_shown(value, terms_by_id, metrics_by_key, rules_by_id, definitions_by_id),
    )
    outcome.rewritten_question = understanding.standalone_question.strip() or question
    asked = outcome.rewritten_question
    chosen_terms = [terms_by_id[term_id] for term_id in _known(understanding.term_ids, terms_by_id)]
    outcome.glossary_terms = [{"id": term["id"], "name": term["name"]} for term in chosen_terms]
    outcome.undefined_concepts = [concept.strip() for concept in understanding.undefined_concepts if concept.strip()]
    chosen_metrics: list[dict[str, Any]] = []
    for item in understanding.metrics:
        metric = metrics_by_key.get((item.dataset_id, item.metric_id))
        if metric and metric not in chosen_metrics:
            chosen_metrics.append(metric)
    outcome.metrics = [
        {"dataset_id": metric["dataset_id"], "metric_id": metric["metric_id"], "name": metric["name"]}
        for metric in chosen_metrics
    ]
    used_definition_ids = _known(understanding.user_definition_ids, definitions_by_id)
    outcome.requires_expert_rule = understanding.question_kind == "qualitative"
    chosen_rules = []
    if outcome.requires_expert_rule:
        chosen_rules = [rules_by_id[rule_id] for rule_id in _known(understanding.rule_ids, rules_by_id)]
        outcome.expert_rules = [{"id": rule["id"], "name": rule["name"]} for rule in chosen_rules]
        used_definition_ids += _known(understanding.user_rule_ids, user_rules_by_id)
    outcome.user_definitions = [
        {key: definitions_by_id[item_id][key] for key in ("id", "kind", "name", "definition")}
        for item_id in dict.fromkeys(used_definition_ids)
    ]

    missing = outcome.missing
    step = first_step + 1
    used_definitions = [definitions_by_id[item["id"]] for item in outcome.user_definitions]
    rules = chosen_rules + _stored_conditions(used_definitions)
    built_metric = None
    if missing == ["metric"] and builder is not None and run_metric is not None:
        found = {
            # A term's columns tell the metric draft which column the concept lives in.
            "glossary_terms": _described(chosen_terms, ("name", "definition", "columns", "dataset_ids")),
            "expert_rules": _described(chosen_rules, ("name", "conditions")),
            "user_definitions": _described(used_definitions, ("name", "definition", "conditions")),
        }
        sources = yield from _tool_call(step, "load_data_model", {}, builder.load_sources, DataModelSources.summary)
        step, built_metric = yield from _build_metric(
            step + 1, sources, asked, found, today, model, builder, run_metric, rules, outcome,
        )
        if built_metric is None and sources.bases:
            step = yield from _propose_dataset(step, sources, asked, found, today, model, builder, outcome)
    # A metric or dataset proposed to administrators by the steps above already says what is missing.
    if missing and built_metric is None and not outcome.suggestions:
        proposal_context = {
            "question": asked,
            "question_kind": understanding.question_kind,
            "undefined_concepts": outcome.undefined_concepts,
            "qualitative_concepts": understanding.qualitative_concepts,
            "missing": missing,
            "found": {
                "glossary_terms": outcome.glossary_terms,
                "metrics": _described(chosen_metrics, ("name", "definition", "unit")),
                "expert_rules": outcome.expert_rules,
                "user_definitions": outcome.user_definitions,
            },
            "existing_names": catalog.summary(),
        }
        try:
            proposals = yield from _tool_call(
                step, "propose_missing_items", {"question": asked, "missing": missing},
                lambda: _ask(
                    model, MissingProposals, prompts.PROPOSE_MISSING_ITEMS,
                    _data_block("Câu hỏi và phần còn thiếu", proposal_context),
                ),
                lambda value: _proposals_shown(value, missing),
            )
        except Exception:  # noqa: BLE001
            # A proposal is a convenience; the error is already reported and the answer still goes on.
            proposals = MissingProposals()
        outcome.clarifications = _clarifications(proposals, missing)
        outcome.suggestions = _suggestions(proposals, missing)
    elif not missing and run_metric is not None:
        plan = understanding.query.model_dump() if understanding.query else None
        try:
            planned = plan_query(plan, catalog.datasets, catalog.metrics, rules)
            outcome.result = yield from _tool_call(
                step, "query_metrics", {"plan": plan, "expert_rule_ids": [rule["id"] for rule in chosen_rules]},
                lambda: _run_metrics(planned, run_metric),
                _query_shown,
            )
            yield "result", outcome.result
        except (QueryPlanError, SemanticLayerRefusedError) as error:
            outcome.query_error = str(error)

    answered_metrics = chosen_metrics or ([built_metric] if built_metric else [])
    context = {
        "question": asked,
        "glossary_terms": _described(chosen_terms, ("name", "definition")),
        "metrics": _described(answered_metrics, ("name", "definition", "unit")),
        "expert_rules": _described(chosen_rules, ("name", "conditions", "exceptions")),
        "user_definitions": outcome.user_definitions,
        # A provisional answer is not a missing metric to the asker; the administrator still sees it as one.
        "missing": [kind for kind in missing if not (kind == "metric" and outcome.result is not None)],
        "clarifications": outcome.clarifications,
        "suggestions": outcome.suggestions,
    }
    if outcome.result is not None:
        context["result"] = result_for_answer(outcome.result)
    if outcome.query_error:
        context["query_error"] = outcome.query_error
    return context


def _lead_value(result: dict[str, Any] | None) -> str | None:
    """What a later sub-question takes from this result: the first row's first dimension, or its first figure.

    A key no label names (no labelled values for its column) comes with its column, "thanh_pho_key = 1", so the later
    sub-question can still filter on it rather than read a bare number.
    """
    if not result or not result["rows"]:
        return None
    row = result["rows"][0]
    for dimension in result["dimensions"]:
        value = row.get(dimension["name"])
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not dimension.get("temporal"):
            return f"{dimension['name']} = {value}"
        return str(value)
    value = next((row[metric["name"]] for metric in result["metrics"] if row.get(metric["name"]) is not None), None)
    return None if value is None else str(value)


def _filled(sub_question: SubQuestion, results: dict[int, dict[str, Any] | None]) -> str | None:
    """The sub-question with each {{q<id>}} replaced by what that earlier sub-question found; None when one of them
    found nothing to go on."""
    values = {part_id: _lead_value(result) for part_id, result in results.items()}
    needed = {int(part_id) for part_id in PLACEHOLDER.findall(sub_question.text)} | set(sub_question.depends_on)
    if any(values.get(part_id) is None for part_id in needed):
        return None
    return PLACEHOLDER.sub(lambda match: values[int(match.group(1))], sub_question.text)


def _in_part(part_id: int, run: Iterator[Event]):
    """One sub-question's events, told apart from the others': its tool calls carry the part and an ID of its own, and
    its result waits for the `part_result` sent when it finishes."""
    while True:
        try:
            name, data = next(run)
        except StopIteration as finished:
            return finished.value
        if name in ("tool_start", "tool_end"):
            yield name, {**data, "id": f"q{part_id}_{data['id']}", "part": part_id}
        elif name != "result":
            yield name, data


def _concurrently(runs: list[Iterator[Event]]):
    """Advance each run in a thread of its own and pass their events on as they come; returns what each run returned.

    Every run catches its own failure, so an error reaching here is a bug and stops the answer.
    """
    if len(runs) <= 1:
        # Nothing, or one run, to wait for: no thread is needed.
        values = []
        for run in runs:
            values.append((yield from run))
        return values
    events: Queue = Queue()

    def drain(index: int, run: Iterator[Event]) -> None:
        try:
            while True:
                try:
                    events.put(("event", index, next(run)))
                except StopIteration as finished:
                    events.put(("done", index, finished.value))
                    return
        except BaseException as error:  # noqa: BLE001
            events.put(("error", index, error))

    values: list[Any] = [None] * len(runs)
    with ThreadPoolExecutor(max_workers=len(runs)) as pool:
        for index, run in enumerate(runs):
            pool.submit(drain, index, run)
        remaining = len(runs)
        while remaining:
            kind, index, payload = events.get()
            if kind == "event":
                yield payload
            elif kind == "error":
                raise payload
            else:
                values[index] = payload
                remaining -= 1
    return values


def _answer_part(sub_question: SubQuestion, text: str, answer: Callable[[str, AgentOutcome], Any]):
    """Answer one sub-question; a failure is kept with it, so the other sub-questions still get answered."""
    part = AgentOutcome()
    try:
        context = yield from _in_part(sub_question.id, answer(text, part))
    except Exception as error:  # noqa: BLE001
        return part, None, error
    return part, context, None


def _merge_parts(outcome: AgentOutcome) -> None:
    """What the sub-questions found, gathered on the question so the log and the review list see all of it."""

    def unique(items: list[dict[str, Any]], key: Callable[[dict[str, Any]], Any]) -> list[dict[str, Any]]:
        return list({key(item): item for item in items}.values())

    parts = outcome.parts
    outcome.rewritten_question = "\n".join(part.rewritten_question or "" for part in parts).strip() or None
    outcome.glossary_terms = unique([term for part in parts for term in part.glossary_terms], lambda item: item["id"])
    outcome.undefined_concepts = list(dict.fromkeys(concept for part in parts for concept in part.undefined_concepts))
    outcome.metrics = unique(
        [metric for part in parts for metric in part.metrics], lambda item: (item["dataset_id"], item["metric_id"]),
    )
    outcome.expert_rules = unique([rule for part in parts for rule in part.expert_rules], lambda item: item["id"])
    outcome.user_definitions = unique(
        [definition for part in parts for definition in part.user_definitions], lambda item: item["id"],
    )
    outcome.requires_expert_rule = any(part.requires_expert_rule for part in parts)
    outcome.clarifications = unique(
        [draft for part in parts for draft in part.clarifications],
        lambda item: (item["kind"], item["name"].casefold()),
    )
    outcome.suggestions = {}
    for part in parts:
        for kind, suggestion in part.suggestions.items():
            outcome.suggestions.setdefault(kind, suggestion)


def _part_shown(
    part_id: int, question: str, part: AgentOutcome | None, query_error: str | None, skipped: str | None,
) -> dict[str, Any]:
    return {
        "id": part_id, "question": question, "result": part.result if part else None,
        "query_error": query_error, "skipped": skipped,
    }


def _answer_parts(triage: QuestionTriage, answer: Callable[[str, AgentOutcome], Any], outcome: AgentOutcome):
    """Answer a question split into sub-questions, each on its own: those that need nothing not yet answered run side
    by side, and a sequential one starts once the sub-questions it uses have answered.

    A sub-question that fails keeps its error and the others go on; one whose source found nothing is skipped. Returns
    each sub-question's answer context, in the classifier's order.
    """
    results: dict[int, dict[str, Any] | None] = {}
    contexts: dict[int, dict[str, Any]] = {}
    shown: dict[int, dict[str, Any]] = {}
    first_error: Exception | None = None
    waiting = list(triage.sub_questions)
    while waiting:
        ready = [item for item in waiting if all(part_id in results for part_id in item.depends_on)]
        waiting = [item for item in waiting if item not in ready]
        runs = []
        for sub_question in ready:
            text = _filled(sub_question, results)
            if text is None:
                unfilled = PLACEHOLDER.sub(lambda match: f"(kết quả ý {match.group(1)})", sub_question.text)
                results[sub_question.id] = None
                contexts[sub_question.id] = {"question": unfilled, "skipped": PART_SKIPPED_MESSAGE}
                shown[sub_question.id] = _part_shown(sub_question.id, unfilled, None, None, PART_SKIPPED_MESSAGE)
                yield "part_result", shown[sub_question.id]
            else:
                runs.append((sub_question, text))
        answered = yield from _concurrently([_answer_part(item, text, answer) for item, text in runs])
        for (sub_question, text), (part, context, error) in zip(runs, answered):
            if error is None:
                results[sub_question.id] = part.result
                contexts[sub_question.id] = context
                outcome.parts.append(part)
                shown[sub_question.id] = _part_shown(sub_question.id, text, part, part.query_error, None)
            else:
                first_error = first_error or error
                results[sub_question.id] = None
                contexts[sub_question.id] = {"question": text, "query_error": PART_FAILED_MESSAGE}
                shown[sub_question.id] = _part_shown(sub_question.id, text, None, PART_FAILED_MESSAGE, None)
            yield "part_result", shown[sub_question.id]
    if not outcome.parts and first_error is not None:
        # Nothing was answered: the question fails as a whole, as a single question would have.
        raise first_error
    order = [item.id for item in triage.sub_questions]
    outcome.part_results = [shown[part_id] for part_id in order]
    _merge_parts(outcome)
    return [contexts[part_id] for part_id in order]


def run_agent(
    question: str,
    history: list[dict[str, str]],
    model: Any,
    load_catalog: Callable[[], SemanticCatalog],
    user_definitions: Sequence[dict[str, Any]] = (),
    run_metric: MetricRunner | None = None,
    today: date | None = None,
    builder: MetricBuilder | None = None,
    dashboard: str | None = None,
) -> Iterator[Event]:
    """Run one question asked on the dashboard titled `dashboard`; `user_definitions` are what the user confirmed there.

    The question is classified first; only one answerable from the data goes on to the catalog and a metric query. A
    question split into sub-questions answers each on its own (docs/specs/taxonomy-cau-hoi.md §6), and the final
    answer takes them in turn.
    """
    today = today or date.today()
    outcome = AgentOutcome()

    triage_input = _triage_input(question, history, dashboard)
    triage = yield from _tool_call(
        1, "classify_question", {"question": question, "history_turns": len(history) // 2},
        lambda: checked_triage(_ask(model, QuestionTriage, prompts.CLASSIFY_QUESTION, triage_input)),
        lambda value: value.model_dump(),
    )
    outcome.triage = triage.model_dump()
    if triage.triage != "answerable":
        yield from _answer_without_query(triage, question, history, dashboard, model, load_catalog, outcome)
        return outcome

    catalog = yield from _tool_call(2, "load_semantic_catalog", {}, load_catalog, SemanticCatalog.summary)
    if triage.execution in ("parallel", "sequential"):

        def answer(text: str, part: AgentOutcome):
            # Each sub-question already stands on its own, so it is understood without the conversation.
            return _answer_question(3, text, [], catalog, model, user_definitions, run_metric, today, builder, part)

        parts = yield from _answer_parts(triage, answer, outcome)
        context = {"today": iso_datetime(today), "question": question, "parts": parts}
    else:
        understood_question = triage.sub_questions[0].text if triage.sub_questions else question
        answered = yield from _answer_question(
            3, understood_question, history, catalog, model, user_definitions, run_metric, today, builder, outcome,
        )
        context = {"today": iso_datetime(today), **answered}
    if triage.set_aside:
        context["set_aside"] = [part.model_dump() for part in triage.set_aside]
    answer_prompt = [("system", prompts.FINAL_ANSWER), ("human", _data_block("Bối cảnh trả lời", context))]
    yield from _streamed_answer(model, answer_prompt, outcome)
    return outcome
