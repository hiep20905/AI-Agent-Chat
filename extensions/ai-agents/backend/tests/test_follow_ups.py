from test_agent_pipeline import CATALOG, TODAY, WEEK_FILTERS, FakeModel, FakeSemanticLayer, _answers

from text_to_bi.superset_ai_agent.modules.chat.services import pipeline
from text_to_bi.superset_ai_agent.modules.chat.services.triage import QuestionTriage, SubQuestion

IN_HANOI = {"column": "thanh_pho_key", "operator": "==", "value": 1, "source": "place"}
LAST_WEEK = [{key: item[key] for key in ("column", "operator", "value", "source")} for item in WEEK_FILTERS[:2]]
# What "nhiệt độ cao nhất ở Hà Nội tuần trước" ran, as the log keeps it.
EARLIER_RESULT = {
    "scope": "Hà Nội, 19/10–25/10/2026",
    "dimensions": [],
    "metrics": [{"name": "nhiet_do_cao_nhat", "label": "Nhiệt độ cao nhất"}],
    "filters": [
        {**LAST_WEEK[0], "label": "Ngày", "shown_value": "2026-10-19"},
        {**LAST_WEEK[1], "label": "Ngày", "shown_value": "2026-10-25"},
        {**IN_HANOI, "label": "Thành phố", "shown_value": "Ha Noi"},
        {"column": "nhiet_do_cao_nhat_c", "operator": ">=", "value": 37, "source": "expert_rule"},
    ],
    "comparison": {"label": "tuần trước nữa", "start": "2026-10-12", "end": "2026-10-18", "coverage": None},
    "rows": [{"nhiet_do_cao_nhat": 38}],
    "queries": [
        {"metric": "Nhiệt độ cao nhất", "dataset_id": 12, "metric_id": 17, "period": None},
        {"metric": "Nhiệt độ cao nhất", "dataset_id": 12, "metric_id": 17, "period": "tuần trước nữa"},
    ],
}
EARLIER = pipeline.previous_queries(EARLIER_RESULT, None)


def _triage(follow_up, text="Nhiệt độ cao nhất ở Hà Nội tuần trước là bao nhiêu?"):
    return QuestionTriage(
        triage="answerable", follow_up=follow_up, sub_questions=[SubQuestion(id=1, text=text, intent="value")],
    )


def _run(follow_up, filters, previous=EARLIER, dataset_id=12):
    answers = _answers(question_kind="quantitative", rule_ids=(), dimensions=(), filters=filters)
    answers["Understanding"].query.dataset_id = dataset_id
    model = FakeModel({**answers, "QuestionTriage": _triage(follow_up)})
    semantic_layer = FakeSemanticLayer()
    history = [{"role": "user", "content": "Nhiệt độ cao nhất ở Hà Nội tuần trước?"},
               {"role": "assistant", "content": "38 °C."}]
    generator = pipeline.run_agent(
        "Còn tuần đó thì sao?", history, model, lambda: CATALOG, (), run_metric=semantic_layer, today=TODAY,
        previous=previous,
    )
    while True:
        try:
            next(generator)
        except StopIteration as finished:
            return finished.value, model, semantic_layer


def _sent_filters(semantic_layer):
    return semantic_layer.calls[0][2]["filters"]


def test_a_turn_is_kept_as_the_query_it_ran_without_the_rules_it_applied():
    assert EARLIER == [{
        "dataset_id": 12, "metric_ids": [17], "metrics": ["Nhiệt độ cao nhất"], "dimensions": [],
        "filters": [*LAST_WEEK, IN_HANOI],
        "comparison": {"label": "tuần trước nữa", "start": "2026-10-12", "end": "2026-10-18"},
        "scope": "Hà Nội, 19/10–25/10/2026", "rows": [{"nhiet_do_cao_nhat": 38}],
    }]


def test_a_split_turn_is_kept_as_each_sub_question_that_answered():
    parts = [{"id": 1, "result": EARLIER_RESULT}, {"id": 2, "result": None}, {"id": 3, "result": EARLIER_RESULT}]

    assert pipeline.previous_queries(None, parts) == EARLIER * 2
    assert pipeline.previous_queries(None, None) == []
    assert pipeline.previous_queries({"rows": [], "queries": []}, None) == []


def test_a_follow_up_sees_the_query_before_and_the_classifier_its_scope():
    _, model, _ = _run("replace", [IN_HANOI])

    assert "Truy vấn lượt trước" in model.prompt("Understanding") and '"value": 1' in model.prompt("Understanding")
    assert "Phạm vi lượt trước" in model.prompt("QuestionTriage")
    assert "Hà Nội, 19/10–25/10/2026" in model.prompt("QuestionTriage")


def test_a_follow_up_that_names_no_period_keeps_the_one_before():
    outcome, _, semantic_layer = _run("replace", [IN_HANOI])

    assert _sent_filters(semantic_layer) == [
        {"column": "thanh_pho_key", "operator": "==", "value": 1},
        {"column": "ngay", "operator": ">=", "value": "2026-10-19"},
        {"column": "ngay", "operator": "<=", "value": "2026-10-25"},
    ]
    assert outcome.result["notes"][-1] == "Giữ khoảng thời gian của câu hỏi trước: 2026-10-19 – 2026-10-25."


def test_a_follow_up_with_a_period_of_its_own_keeps_it():
    week_before = [
        {"column": "ngay", "operator": ">=", "value": "2026-10-12", "source": "time"},
        {"column": "ngay", "operator": "<=", "value": "2026-10-18", "source": "time"},
    ]
    outcome, _, semantic_layer = _run("replace", [*week_before, IN_HANOI])

    assert [item["value"] for item in _sent_filters(semantic_layer) if item["column"] == "ngay"] == [
        "2026-10-12", "2026-10-18",
    ]
    assert not any("Giữ khoảng thời gian" in note for note in outcome.result["notes"])


def test_a_new_question_starts_afresh():
    _, model, semantic_layer = _run("new", [IN_HANOI])

    assert "Truy vấn lượt trước" not in model.prompt("Understanding")
    assert _sent_filters(semantic_layer) == [{"column": "thanh_pho_key", "operator": "==", "value": 1}]


def test_a_follow_up_on_another_dataset_keeps_no_period():
    other = [{**EARLIER[0], "dataset_id": 99}]
    _, _, semantic_layer = _run("refine", [IN_HANOI], previous=other)

    assert _sent_filters(semantic_layer) == [{"column": "thanh_pho_key", "operator": "==", "value": 1}]
