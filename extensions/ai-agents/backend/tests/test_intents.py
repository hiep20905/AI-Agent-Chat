import pytest
from test_agent_pipeline import CATALOG, TODAY, WEEK_FILTERS, FakeModel, FakeSemanticLayer, _answers

from text_to_bi.superset_ai_agent.modules.chat.exceptions import QueryPlanError
from text_to_bi.superset_ai_agent.modules.chat.services import pipeline
from text_to_bi.superset_ai_agent.modules.chat.services.intents import shaped_plan
from text_to_bi.superset_ai_agent.modules.chat.services.triage import QuestionTriage, SubQuestion

PLACE = [item for item in WEEK_FILTERS if item["source"] == "place"]


def _plan(**changes):
    return {"dataset_id": 12, "metric_ids": [17], "dimensions": [], "filters": PLACE, "presentation": "number",
            **changes}


def _shaped(intent, **changes):
    return shaped_plan(_plan(**changes), intent, CATALOG.datasets, CATALOG.metrics)


def test_a_trend_not_split_by_time_is_split_by_the_dataset_date_and_drawn_as_a_line():
    shaped, changes = _shaped("trend")

    assert shaped["dimensions"] == ["ngay"] and shaped["presentation"] == "line"
    assert changes == ["chia theo Ngày để thấy diễn biến"]


def test_a_trend_comparing_two_periods_keeps_its_one_figure_per_period():
    comparison = {"start": "2026-10-12", "end": "2026-10-18", "label": "tuần trước"}

    shaped, changes = _shaped("trend", comparison=comparison)

    assert shaped["dimensions"] == [] and changes == []


def test_a_ranking_without_an_order_is_ordered_by_its_metric_highest_first():
    shaped, changes = _shaped("ranking", dimensions=["thanh_pho_key"], limit=1)

    assert shaped["order_by"] == {"metric_id": 17, "direction": "desc"} and shaped["limit"] == 1
    assert changes == ["xếp theo Nhiệt độ cao nhất từ cao xuống thấp"]


def test_a_ranking_the_model_ordered_lowest_first_stays_so():
    shaped, changes = _shaped("ranking", dimensions=["ngay"], order_by={"metric_id": 17, "direction": "asc"})

    assert shaped["order_by"]["direction"] == "asc" and changes == []


@pytest.mark.parametrize("intent", ["ranking", "list", "breakdown"])
def test_a_question_about_groups_without_a_grouping_goes_back_to_the_model(intent):
    with pytest.raises(QueryPlanError, match="dimensions"):
        _shaped(intent)


def test_a_comparison_with_nothing_to_compare_goes_back_to_the_model():
    with pytest.raises(QueryPlanError, match="comparison"):
        _shaped("comparison")
    assert _shaped("comparison", dimensions=["thanh_pho_key"])[1] == []


def test_a_value_question_and_a_question_with_no_intent_are_left_as_planned():
    counted_days = _plan(dimensions=["ngay"])

    assert shaped_plan(counted_days, "value", CATALOG.datasets, CATALOG.metrics) == (counted_days, [])
    assert shaped_plan(counted_days, None, CATALOG.datasets, CATALOG.metrics) == (counted_days, [])


def test_the_intent_reaches_the_understanding_step_and_shapes_the_query_it_runs():
    triage = QuestionTriage(
        triage="answerable", execution="single",
        sub_questions=[
            SubQuestion(id=1, text="Nhiệt độ cao nhất ở Hà Nội tuần trước diễn biến thế nào?", intent="trend"),
        ],
    )
    answers = _answers(term_ids=(), rule_ids=(), question_kind="quantitative", dimensions=())
    model = FakeModel({**answers, "QuestionTriage": triage})
    semantic_layer = FakeSemanticLayer()

    events = list(pipeline.run_agent(
        "Nhiệt độ cao nhất ở Hà Nội tuần trước diễn biến thế nào?", [], model, lambda: CATALOG,
        run_metric=semantic_layer, today=TODAY,
    ))

    assert "Loại câu hỏi: trend" in model.prompt("Understanding")
    assert semantic_layer.calls[0][2]["dimensions"] == ["ngay"]
    started = next(data for name, data in events if name == "tool_start" and data["name"] == "query_metrics")
    assert started["arguments"]["shaped"] == ["chia theo Ngày để thấy diễn biến"]
