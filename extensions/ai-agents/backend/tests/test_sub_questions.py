import json
import threading
from types import SimpleNamespace

import pytest
from test_agent_pipeline import CATALOG, TODAY, WEEK_FILTERS, FakeModel, _answers

from text_to_bi.superset_ai_agent.modules.chat.services import pipeline
from text_to_bi.superset_ai_agent.modules.chat.services.triage import QuestionTriage, SubQuestion

HOTTEST = "Thành phố nào nóng nhất tuần trước?"
IN_HUE = "Nhiệt độ cao nhất ở Huế hôm qua?"
IN_FOUND_CITY = "Nhiệt độ cao nhất ngày nào ở {{q1}} tuần trước?"
BY_CITY = _answers(question_kind="quantitative", rule_ids=(), filters=WEEK_FILTERS[:2], dimensions=("thanh_pho_key",))
BY_DAY = _answers(question_kind="quantitative", rule_ids=())
NO_METRIC = _answers(question_kind="quantitative", rule_ids=(), metrics=(), term_ids=())
# What the final answer is given for each answered sub-question.
BY_PART_KEYS = (
    "question", "glossary_terms", "metrics", "expert_rules", "user_definitions", "missing", "clarifications",
    "suggestions", "result",
)


class PartsModel(FakeModel):
    """Understands each sub-question from its own answers, keyed by the question it was given.

    `failing` questions raise; `barrier` makes every understanding wait for the others, which only parallel parts pass.
    """

    def __init__(self, triage, understood, failing=(), barrier=None):
        super().__init__({"QuestionTriage": triage, "MissingProposals": NO_METRIC["MissingProposals"]})
        self.understood, self.failing, self.barrier = understood, failing, barrier

    def with_structured_output(self, schema):
        if schema is not pipeline.Understanding:
            return super().with_structured_output(schema)

        def invoke(messages):
            self.prompts.append((schema.__name__, messages))
            question = messages[1][1].split("Câu hỏi mới nhất: ", 1)[1].split("\n", 1)[0]
            if self.barrier is not None:
                self.barrier.wait(timeout=5)
            if question in self.failing:
                raise RuntimeError("model hết thời gian chờ")
            return self.understood[question]["Understanding"]

        return SimpleNamespace(invoke=invoke)


class CitySemanticLayer:
    """One row per requested dimension: Hà Nội by city, two days by date; `empty` metrics answer nothing."""

    def __init__(self, empty=()):
        self.calls, self.empty = [], set(empty)

    def __call__(self, dataset_id, metric_id, query):
        self.calls.append(query)
        if query["dimensions"] == ["thanh_pho_key"]:
            rows = [{"thanh_pho_key": 1, "nhiet_do_cao_nhat": "38.5"}]
        else:
            rows = [{"ngay": "Mon, 19 Oct 2026 00:00:00 GMT", "nhiet_do_cao_nhat": "37.0"}]
        return {"rows": [] if len(self.calls) in self.empty else rows, "truncated": False}


def _sub(sub_id, text, intent="value", depends_on=()):
    return SubQuestion(id=sub_id, text=text, intent=intent, depends_on=list(depends_on))


def _run(model, question="Câu hỏi nhiều ý", semantic_layer=None):
    semantic_layer = semantic_layer or CitySemanticLayer()
    events = []
    generator = pipeline.run_agent(
        question, [], model, lambda: CATALOG, (), run_metric=semantic_layer, today=TODAY, dashboard="Thời tiết",
    )
    while True:
        try:
            events.append(next(generator))
        except StopIteration as finished:
            return finished.value, events, semantic_layer


def _parallel(*texts):
    subs = [_sub(index, text) for index, text in enumerate(texts, 1)]
    return QuestionTriage(triage="answerable", execution="parallel", sub_questions=subs)


def _sequential():
    return QuestionTriage(triage="answerable", execution="sequential", sub_questions=[
        _sub(1, HOTTEST, "ranking"), _sub(2, IN_FOUND_CITY, "ranking", depends_on=(1,)),
    ])


def _understood(events, part):
    return next(
        data for name, data in events
        if name == "tool_start" and data["name"] == "understand_question" and data["part"] == part
    )


def test_independent_sub_questions_each_get_their_own_steps_result_and_paragraph():
    model = PartsModel(_parallel(HOTTEST, IN_HUE), {HOTTEST: BY_CITY, IN_HUE: BY_DAY})
    outcome, events, semantic_layer = _run(model)

    starts = [data for name, data in events if name == "tool_start"]
    assert [data["name"] for data in starts[:2]] == ["classify_question", "load_semantic_catalog"]
    assert sorted((data["part"], data["id"], data["name"]) for data in starts[2:]) == [
        (1, "q1_call_3", "understand_question"), (1, "q1_call_4", "query_metrics"),
        (2, "q2_call_3", "understand_question"), (2, "q2_call_4", "query_metrics"),
    ]
    assert len(semantic_layer.calls) == 2 and "result" not in [name for name, _ in events]
    assert [data["id"] for name, data in events if name == "part_result"] in ([1, 2], [2, 1])
    assert [part["question"] for part in outcome.part_results] == [HOTTEST, IN_HUE]
    assert outcome.part_results[0]["result"]["rows"] == [{"thanh_pho_key": "Ha Noi", "nhiet_do_cao_nhat": 38.5}]
    assert outcome.result is None and (outcome.status, outcome.missing) == ("resolved", [])
    assert outcome.metrics == [{"dataset_id": 12, "metric_id": 17, "name": "Nhiệt độ cao nhất"}]
    context = json.loads(model.prompt("final").split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert context["question"] == "Câu hỏi nhiều ý" and "result" not in context
    assert [sorted(part) for part in context["parts"]] == [sorted(BY_PART_KEYS)] * 2


def test_parallel_sub_questions_run_at_the_same_time():
    # Each understanding waits until both have started; run one after the other, the first would time out.
    barrier = threading.Barrier(2)
    model = PartsModel(_parallel(HOTTEST, IN_HUE), {HOTTEST: BY_CITY, IN_HUE: BY_DAY}, barrier=barrier)
    outcome, _, _ = _run(model)

    assert [part["query_error"] for part in outcome.part_results] == [None, None]
    assert not barrier.broken


def test_a_sequential_sub_question_waits_for_and_uses_what_the_earlier_one_found():
    filled = "Nhiệt độ cao nhất ngày nào ở Ha Noi tuần trước?"
    model = PartsModel(_sequential(), {HOTTEST: BY_CITY, filled: BY_DAY})
    outcome, events, _ = _run(model, "Thành phố nào nóng nhất tuần trước, và ngày nào ở đó nóng nhất?")

    assert _understood(events, 2)["arguments"]["question"] == filled
    first_done = events.index(next(event for event in events if event[0] == "part_result" and event[1]["id"] == 1))
    assert events.index(("tool_start", _understood(events, 2))) > first_done
    assert [part["question"] for part in outcome.part_results] == [HOTTEST, filled]
    assert outcome.part_results[1]["result"]["rows"][0]["ngay"] == "2026-10-19"


def test_a_sub_question_whose_source_found_nothing_is_skipped_and_said_so():
    model = PartsModel(_sequential(), {HOTTEST: BY_CITY})
    outcome, events, _ = _run(model, semantic_layer=CitySemanticLayer(empty=(1,)))

    assert [data["part"] for name, data in events if name == "tool_start" and "part" in data] == [1, 1]
    assert outcome.part_results[1] == {
        "id": 2, "question": "Nhiệt độ cao nhất ngày nào ở (kết quả ý 1) tuần trước?", "result": None,
        "query_error": None, "skipped": pipeline.PART_SKIPPED_MESSAGE,
    }
    assert pipeline.PART_SKIPPED_MESSAGE in model.prompt("final")


def test_a_failing_sub_question_keeps_its_error_and_the_others_are_still_answered():
    model = PartsModel(_parallel(HOTTEST, IN_HUE), {IN_HUE: BY_DAY}, failing=(HOTTEST,))
    outcome, events, _ = _run(model)

    failed = [data for name, data in events if name == "tool_end" and data["status"] == "error"]
    assert [(data["id"], data["error"]) for data in failed] == [("q1_call_3", "model hết thời gian chờ")]
    assert outcome.part_results[0]["query_error"] == pipeline.PART_FAILED_MESSAGE
    assert outcome.part_results[1]["result"] is not None
    assert outcome.status == "resolved" and outcome.answer == "Đã xác định."


def test_a_question_whose_every_sub_question_fails_fails_as_a_whole():
    model = PartsModel(_parallel(HOTTEST, IN_HUE), {}, failing=(HOTTEST, IN_HUE))

    with pytest.raises(RuntimeError, match="model hết thời gian chờ"):
        _run(model)


def test_what_any_sub_question_lacks_is_what_the_question_lacks():
    model = PartsModel(_parallel(HOTTEST, IN_HUE), {HOTTEST: BY_CITY, IN_HUE: NO_METRIC})
    outcome, events, _ = _run(model)

    assert (outcome.status, outcome.missing) == ("needs_review", ["metric"])
    assert list(outcome.suggestions) == ["metric"]
    assert [data["part"] for name, data in events if name == "tool_start" and data["name"] == "propose_missing_items"] \
        == [2]
    assert outcome.part_results[0]["result"] is not None and outcome.part_results[1]["result"] is None


def test_a_single_sub_question_keeps_the_single_answer_shape():
    triage = QuestionTriage(triage="answerable", sub_questions=[_sub(1, IN_HUE)])
    outcome, events, _ = _run(PartsModel(triage, {IN_HUE: BY_DAY}), IN_HUE)

    assert "part_result" not in [name for name, _ in events] and outcome.part_results == []
    assert outcome.result is not None and "result" in [name for name, _ in events]
    assert all("part" not in data for name, data in events if name == "tool_start")


@pytest.mark.parametrize(("result", "lead"), [
    ({"dimensions": [{"name": "thanh_pho_key"}], "metrics": [{"name": "m"}],
      "rows": [{"thanh_pho_key": "Hue", "m": 3}]}, "Hue"),
    ({"dimensions": [{"name": "thanh_pho_key", "temporal": False}], "metrics": [{"name": "m"}],
      "rows": [{"thanh_pho_key": 1, "m": 42}]}, "thanh_pho_key = 1"),
    ({"dimensions": [], "metrics": [{"name": "m"}], "rows": [{"m": 28.5}]}, "28.5"),
    ({"dimensions": [{"name": "ngay"}], "metrics": [{"name": "m"}], "rows": []}, None),
    (None, None),
])
def test_the_lead_value_is_the_first_rows_dimension_or_figure(result, lead):
    assert pipeline._lead_value(result) == lead
