import json
from pathlib import Path

import pytest
from test_agent_pipeline import CATALOG, TODAY, FakeModel, FakeSemanticLayer, _answers

from text_to_bi.superset_ai_agent.modules.chat.services import pipeline, prompts
from text_to_bi.superset_ai_agent.modules.chat.services.triage import (
    BLOCKED_REPLIES,
    QuestionTriage,
    SetAsidePart,
    SubQuestion,
    checked_triage,
)

LABELLED_PATH = Path(__file__).parent / "fixtures" / "question_taxonomy.jsonl"
LABELLED = [json.loads(line) for line in LABELLED_PATH.read_text(encoding="utf-8").splitlines()]


def _labels(example):
    return {key: value for key, value in example.items() if key not in ("id", "question", "history")}


def _sub(sub_id, text, intent="value", depends_on=()):
    return SubQuestion(id=sub_id, text=text, intent=intent, depends_on=list(depends_on))


class CountingCatalog:
    def __init__(self):
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return CATALOG


def _run(triage, question="Câu hỏi", answers=None, history=(), dashboard="Thời tiết các thành phố"):
    model = FakeModel({"QuestionTriage": triage, **(answers or {})})
    catalog = CountingCatalog()
    events = []
    generator = pipeline.run_agent(
        question, list(history), model, catalog, (), run_metric=FakeSemanticLayer(), today=TODAY, dashboard=dashboard,
    )
    while True:
        try:
            events.append(next(generator))
        except StopIteration as finished:
            return finished.value, events, model, catalog


def _steps(events):
    return [data["name"] for name, data in events if name == "tool_start"]


@pytest.mark.parametrize("example", LABELLED, ids=lambda example: f"{example['id']}")
def test_every_labelled_example_fits_the_triage_schema_and_its_rules(example):
    labels = QuestionTriage(**_labels(example))

    assert checked_triage(labels) == labels


def test_the_labelled_examples_cover_every_label():
    assert {example["triage"] for example in LABELLED} == {
        "answerable", "metadata", "chitchat", "out_of_domain", "unsupported", "unclear", "blocked",
    }
    assert {example.get("blocked_reason") for example in LABELLED} - {None} == {
        "injection", "write", "pii", "access", "harmful",
    }
    assert {sub["intent"] for example in LABELLED for sub in example["sub_questions"]} == set(
        SubQuestion.model_fields["intent"].annotation.__args__
    )
    assert {example["follow_up"] for example in LABELLED} == {"new", "refine", "replace", "about_result"}
    assert {example["execution"] for example in LABELLED} == {None, "single", "parallel", "sequential"}


def test_a_question_that_is_not_answerable_keeps_no_sub_questions_and_only_a_blocked_one_a_reason():
    labels = QuestionTriage(
        triage="chitchat", blocked_reason="write", execution="single", sub_questions=[_sub(1, "Xin chào")],
        set_aside=[SetAsidePart(text="giá vàng", triage="out_of_domain")],
    )

    checked = checked_triage(labels)

    assert (checked.blocked_reason, checked.execution, checked.sub_questions, checked.set_aside) == (None, None, [], [])
    assert checked_triage(QuestionTriage(triage="blocked", blocked_reason="pii")).blocked_reason == "pii"


def test_sub_questions_are_capped_cleaned_and_their_execution_follows_from_the_dependencies():
    labels = QuestionTriage(
        triage="answerable", blocked_reason="injection", execution="parallel",
        sub_questions=[
            _sub(1, " Thành phố nào mưa nhiều nhất? ", "ranking"),
            _sub(2, "   "),
            _sub(1, "Câu trùng id"),
            _sub(3, "Độ ẩm ở {{q1}}?", depends_on=(1, 1, 7)),
            _sub(4, "Nhiệt độ ở {{q5}}?", depends_on=(5,)),
            _sub(5, "Câu thứ tư"),
            _sub(6, "Câu thứ năm bị bỏ"),
        ],
    )

    checked = checked_triage(labels)

    assert [(item.id, item.text, item.depends_on) for item in checked.sub_questions] == [
        (1, "Thành phố nào mưa nhiều nhất?", []),
        (3, "Độ ẩm ở {{q1}}?", [1]),
        (4, "Nhiệt độ ở {{q5}}?", []),
        (5, "Câu thứ tư", []),
    ]
    assert (checked.execution, checked.blocked_reason) == ("sequential", None)
    independent = QuestionTriage(
        triage="answerable", execution="sequential", sub_questions=[_sub(1, "A"), _sub(2, "B")],
    )
    assert checked_triage(independent).execution == "parallel"
    assert checked_triage(QuestionTriage(triage="answerable", execution="parallel")).execution == "single"


def test_the_classifier_sees_the_dashboard_the_conversation_and_the_question_but_no_catalog():
    history = [{"role": "user", "content": "Nhiệt độ ở Huế?"}, {"role": "assistant", "content": "25 °C."}]
    _, events, model, _ = _run(QuestionTriage(triage="chitchat"), "Còn Hải Phòng?", history=history)

    prompt = model.prompt("QuestionTriage")
    assert "Dashboard: Thời tiết các thành phố" in prompt and "user: Nhiệt độ ở Huế?" in prompt
    assert "Câu hỏi mới nhất: Còn Hải Phòng?" in prompt and "Nhiệt độ cao nhất" not in prompt
    classified = next(data for name, data in events if name == "tool_end" and data["name"] == "classify_question")
    assert classified["result"]["triage"] == "chitchat"


@pytest.mark.parametrize("reason", sorted(BLOCKED_REPLIES))
def test_a_blocked_question_gets_a_fixed_reply_and_reaches_no_other_model_call_or_the_catalog(reason):
    outcome, events, model, catalog = _run(QuestionTriage(triage="blocked", blocked_reason=reason))

    assert _steps(events) == ["classify_question"] and catalog.reads == 0
    assert [schema for schema, _ in model.prompts] == ["QuestionTriage"]
    assert outcome.answer == BLOCKED_REPLIES[reason]
    assert [data["text"] for name, data in events if name == "token"] == [BLOCKED_REPLIES[reason]]
    assert (outcome.status, outcome.missing, outcome.requires_expert_rule) == ("blocked", [], False)
    assert outcome.triage["blocked_reason"] == reason


@pytest.mark.parametrize(("triage", "status"), [
    ("chitchat", "resolved"), ("out_of_domain", "declined"), ("unsupported", "declined"), ("unclear", "declined"),
])
def test_a_question_needing_no_data_is_answered_in_a_sentence_without_the_catalog(triage, status):
    outcome, events, model, catalog = _run(QuestionTriage(triage=triage), "Giá vàng hôm nay?")

    assert _steps(events) == ["classify_question"] and catalog.reads == 0
    assert [schema for schema, _ in model.prompts] == ["QuestionTriage", "final"]
    final = next(messages for name, messages in model.prompts if name == "final")
    assert final[0] == ("system", prompts.DECLINED_REPLY)
    assert f'"triage": "{triage}"' in final[1][1] and "Thời tiết các thành phố" in final[1][1]
    assert (outcome.status, outcome.missing, outcome.answer) == (status, [], "Đã xác định.")


def test_a_question_about_the_data_is_answered_from_the_catalog_without_a_query():
    outcome, events, model, catalog = _run(QuestionTriage(triage="metadata"), "Nhiệt độ cao nhất tính thế nào?")

    assert _steps(events) == ["classify_question", "load_semantic_catalog"] and catalog.reads == 1
    assert [schema for schema, _ in model.prompts] == ["QuestionTriage", "final"]
    final = next(messages for name, messages in model.prompts if name == "final")
    assert final[0] == ("system", prompts.DATA_DESCRIPTION) and "Nhiệt độ cao nhất" in final[1][1]
    assert (outcome.status, outcome.missing, outcome.result) == ("resolved", [], None)


def test_a_single_sub_question_is_what_the_question_understanding_step_reads():
    triage = QuestionTriage(
        triage="answerable", sub_questions=[_sub(1, "Nhiệt độ cao nhất ở Hà Nội tuần trước là bao nhiêu?")],
        set_aside=[SetAsidePart(text="giá vàng hôm nay", triage="out_of_domain")],
    )
    outcome, events, model, _ = _run(triage, "Xin chào, nhiệt độ ở Hà Nội tuần trước? Và giá vàng hôm nay?", _answers())

    assert _steps(events) == ["classify_question", "load_semantic_catalog", "understand_question", "query_metrics"]
    understood = next(data for name, data in events if name == "tool_start" and data["name"] == "understand_question")
    assert understood["arguments"]["question"] == "Nhiệt độ cao nhất ở Hà Nội tuần trước là bao nhiêu?"
    assert "Câu hỏi mới nhất: Nhiệt độ cao nhất ở Hà Nội tuần trước là bao nhiêu?" in model.prompt("Understanding")
    assert '"set_aside"' in model.prompt("final") and "giá vàng hôm nay" in model.prompt("final")
    assert (outcome.status, outcome.triage["execution"]) == ("resolved", "single")


def test_a_question_split_in_several_is_still_understood_as_asked():
    triage = QuestionTriage(
        triage="answerable",
        sub_questions=[
            _sub(1, "Thành phố nào mưa nhiều nhất?", "ranking"), _sub(2, "Độ ẩm ở {{q1}}?", depends_on=(1,)),
        ],
    )
    _, events, _, _ = _run(triage, "Thành phố nào mưa nhiều nhất, độ ẩm ở đó bao nhiêu?", _answers())

    understood = next(data for name, data in events if name == "tool_start" and data["name"] == "understand_question")
    assert understood["arguments"]["question"] == "Thành phố nào mưa nhiều nhất, độ ẩm ở đó bao nhiêu?"
