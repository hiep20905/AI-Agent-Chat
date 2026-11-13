import json

import pytest
from test_agent_pipeline import TODAY, WEEK_FILTERS, FakeModel, _answers

from text_to_bi.superset_ai_agent.modules.chat.services import pipeline
from text_to_bi.superset_ai_agent.modules.chat.services.repair import (
    MAX_QUERY_ATTEMPTS,
    closest_value,
    empty_result_kind,
    normalized_text,
    refusal_kind,
    respelled_plan,
)
from text_to_bi.superset_ai_agent.modules.semantic_layer.exceptions import SemanticLayerRefusedError
from text_to_bi.superset_ai_agent.modules.semantic_layer.services.catalog import SemanticCatalog

# A city kept by its name without accents, and a key column no labelled values name, as on a fresh installation.
DATASET = {
    "id": 12,
    "name": "fact_tong_hop_thoi_tiet",
    "columns": [
        {"name": "ngay", "label": "Ngày", "type": "DATE", "temporal": True, "groupby": True, "filterable": True},
        {"name": "thanh_pho_key", "type": "INTEGER", "groupby": True, "filterable": True},
        {"name": "ten_thanh_pho", "label": "Tên thành phố", "type": "TEXT", "groupby": True, "filterable": True},
    ],
    "labelled_values": [],
}
CATALOG = SemanticCatalog(
    metrics=[{
        "dataset_id": 12, "metric_id": 17, "name": "Nhiệt độ cao nhất", "metric_name": "nhiet_do_cao_nhat",
        "unit": "°C",
    }],
    datasets=[DATASET],
)
WEEK = WEEK_FILTERS[:2]


def _in_city(city, column="ten_thanh_pho"):
    return [*WEEK, {"column": column, "operator": "==", "value": city, "source": "place"}]


def _plan(filters):
    return pipeline.QueryPlan(dataset_id=12, metric_ids=[17], filters=filters, presentation="number", scope="Huế")


class CitiesSemanticLayer:
    """Holds Hà Nội and Huế as "Ha Noi" and "Hue"; `failures` are raised by the first calls, one each."""

    def __init__(self, failures=()):
        self.calls, self.failures = [], list(failures)

    def __call__(self, dataset_id, metric_id, query):
        self.calls.append(query)
        if self.failures:
            raise self.failures.pop(0)
        if query["dimensions"] == ["ten_thanh_pho"]:
            return {"rows": [{"ten_thanh_pho": "Ha Noi", "nhiet_do_cao_nhat": "30"},
                             {"ten_thanh_pho": "Hue", "nhiet_do_cao_nhat": "23.1"}]}
        cities = [item["value"] for item in query["filters"] if item["column"] == "ten_thanh_pho"]
        return {"rows": [{"nhiet_do_cao_nhat": "23.1"}] if cities == ["Hue"] else []}


def _run(filters, repair=None, semantic_layer=None):
    answers = _answers(question_kind="quantitative", rule_ids=(), term_ids=(), dimensions=(), filters=filters)
    if repair is not None:
        answers["QueryRepair"] = repair
    model = FakeModel(answers)
    semantic_layer = semantic_layer or CitiesSemanticLayer()
    events = []
    generator = pipeline.run_agent(
        "Nhiệt độ cao nhất ở Huế tuần trước?", [], model, lambda: CATALOG, (), run_metric=semantic_layer, today=TODAY,
    )
    while True:
        try:
            events.append(next(generator))
        except StopIteration as finished:
            return finished.value, events, model, semantic_layer


def _steps(events):
    return [data["name"] for name, data in events if name == "tool_start"]


def _final_context(model):
    return json.loads(model.prompt("final").split("```json\n", 1)[1].rsplit("\n```", 1)[0])


def test_text_is_compared_without_accents_case_or_punctuation():
    assert normalized_text("TP. Hồ Chí Minh") == "tp ho chi minh"
    assert normalized_text("Đà Nẵng") == "da nang"


@pytest.mark.parametrize(("asked", "stored", "found"), [
    ("Huế", ["Ha Noi", "Hue"], "Hue"),
    ("TP Hồ Chí Minh", ["Ho Chi Minh", "Ha Noi"], "Ho Chi Minh"),
    ("Ha Nôi", ["Ha Noi", "Hai Phong"], "Ha Noi"),
    ("Hà Nọi", ["Ha Noi", "Hai Phong"], "Ha Noi"),
    ("Ha", ["Ha Noi", "Ha Long"], None),
    ("Paris", ["Ha Noi", "Hue"], None),
])
def test_the_closest_stored_value_is_the_only_one_the_asked_value_can_stand_for(asked, stored, found):
    assert closest_value(asked, stored) == found


@pytest.mark.parametrize(("error", "kind"), [
    (SemanticLayerRefusedError("mất kết nối", transient=True), "transient"),
    (SemanticLayerRefusedError("bận", status_code=503, transient=True), "transient"),
    (SemanticLayerRefusedError("Superset không nhận truy vấn metric này.", status_code=400), "rejected_query"),
    (SemanticLayerRefusedError("Metric execution failed.", status_code=500), "rejected_query"),
    (SemanticLayerRefusedError("A current approved metric contract is required.", status_code=403), "not_allowed"),
    (SemanticLayerRefusedError("Metric chưa được duyệt."), "not_allowed"),
])
def test_a_refusal_is_told_apart_by_whether_trying_again_can_help(error, kind):
    assert refusal_kind(error) == kind


def test_a_respelled_plan_changes_only_values_the_data_spells_otherwise():
    plan = {"dataset_id": 12, "filters": [
        *WEEK, {"column": "ten_thanh_pho", "operator": "IN", "value": ["Huế", "Ha Noi"], "source": "place"},
    ]}

    fixed, changes = respelled_plan(plan, {"ten_thanh_pho": ["Ha Noi", "Hue"]})

    assert fixed["filters"][:2] == WEEK and fixed["filters"][2]["value"] == ["Hue", "Ha Noi"]
    assert changes == ["“Huế” → “Hue”"]
    assert respelled_plan(plan, {"ten_thanh_pho": ["Ha Noi", "Hue", "Huế"]}) == (None, [])
    assert respelled_plan(plan, {"ten_thanh_pho": ["Da Nang"]}) == (None, [])


@pytest.mark.parametrize(("result", "kind"), [
    ({"total_rows": 1, "filters": []}, None),
    ({"total_rows": 0, "coverage": {"status": "none"}, "filters": []}, "out_of_range"),
    ({"total_rows": 0, "filters": [{"column": "ten_thanh_pho", "operator": "==", "value": "Huế", "source": "place"}]},
     "value_mismatch"),
    ({"total_rows": 0, "filters": [{"column": "ngay", "operator": ">=", "value": "2026-11-01", "source": "time"}]},
     "no_match"),
    ({"total_rows": 0, "filters": [{"column": "thanh_pho_key", "operator": "==", "value": 6, "source": "place"}]},
     "no_match"),
])
def test_an_empty_result_is_told_apart_by_why_it_is_empty(result, kind):
    columns = {column["name"]: column for column in DATASET["columns"]}

    assert empty_result_kind(result, columns) == kind


def test_a_name_the_data_spells_without_accents_is_respelled_from_the_stored_values_and_run_again():
    outcome, events, model, semantic_layer = _run(_in_city("Huế"))

    assert _steps(events)[3:] == ["query_metrics", "explore_values", "query_metrics"]
    assert "QueryRepair" not in [schema for schema, _ in model.prompts]
    assert semantic_layer.calls[1] == {"dimensions": ["ten_thanh_pho"], "filters": []}
    explored = next(data for name, data in events if name == "tool_end" and data["name"] == "explore_values")
    assert explored["result"] == {"ten_thanh_pho": {"count": 2, "values": ["Ha Noi", "Hue"]}}
    assert outcome.result["rows"] == [{"nhiet_do_cao_nhat": 23.1}]
    assert outcome.result["notes"][-1] == "Đã đổi giá trị lọc theo cách viết trong dữ liệu: “Huế” → “Hue”."
    assert outcome.attempts == [
        {"attempt": 1, "kind": "value_mismatch", "detail": pipeline.EMPTY_RESULT_DETAIL, "fix": "“Huế” → “Hue”"},
    ]
    assert (outcome.status, outcome.query_error) == ("resolved", None)
    assert _final_context(model)["attempts"] == outcome.attempts


def test_a_name_the_data_does_not_hold_goes_to_the_model_which_may_give_up():
    gave_up = pipeline.QueryRepair(query=None, reason="Dữ liệu không có thành phố Paris.")
    outcome, events, model, _ = _run(_in_city("Paris"), repair=gave_up)

    assert _steps(events)[3:] == ["query_metrics", "explore_values", "repair_query"]
    repair_prompt = model.prompt("QueryRepair")
    assert '"value_mismatch"' in repair_prompt and '"Hue"' in repair_prompt and "Paris" in repair_prompt
    assert outcome.result["rows"] == [] and outcome.query_error is None
    assert outcome.attempts == [{
        "attempt": 1, "kind": "value_mismatch", "detail": pipeline.EMPTY_RESULT_DETAIL,
        "unfixable": "Dữ liệu không có thành phố Paris.",
    }]
    assert _final_context(model)["attempts"] == outcome.attempts


def test_a_plan_the_checks_reject_is_mended_by_the_model_and_run():
    mended = pipeline.QueryRepair(query=_plan(_in_city("Hue")), reason="Lọc theo tên thành phố thay vì mã.")
    outcome, events, model, semantic_layer = _run(_in_city("Ha Noi", "thanh_pho_key"), repair=mended)

    assert _steps(events)[3:] == ["repair_query", "query_metrics"]
    assert "phải là số" in model.prompt("QueryRepair")
    assert outcome.attempts == [{
        "attempt": 1, "kind": "invalid_plan", "detail": "Giá trị “Ha Noi” của cột thanh_pho_key phải là số.",
        "fix": "Lọc theo tên thành phố thay vì mã.",
    }]
    assert outcome.result["rows"] == [{"nhiet_do_cao_nhat": 23.1}] and len(semantic_layer.calls) == 1


def test_a_semantic_layer_that_did_not_answer_is_asked_the_same_again():
    busy = SemanticLayerRefusedError("Không kết nối được tới Semantic Layer để chạy truy vấn.", transient=True)
    outcome, events, model, _ = _run(_in_city("Hue"), semantic_layer=CitiesSemanticLayer(failures=[busy]))

    assert _steps(events)[3:] == ["query_metrics", "query_metrics"]
    assert "QueryRepair" not in [schema for schema, _ in model.prompts]
    assert [attempt["kind"] for attempt in outcome.attempts] == ["transient"]
    assert outcome.result["rows"] == [{"nhiet_do_cao_nhat": 23.1}]


def test_trying_stops_after_the_last_attempt_and_the_last_reason_is_told():
    still_wrong = pipeline.QueryRepair(query=_plan(_in_city("Ha Noi", "thanh_pho_key")), reason="Thử lại.")
    outcome, events, _, semantic_layer = _run(_in_city("Ha Noi", "thanh_pho_key"), repair=still_wrong)

    assert _steps(events)[3:] == ["repair_query"] * (MAX_QUERY_ATTEMPTS - 1)
    assert len(outcome.attempts) == MAX_QUERY_ATTEMPTS and semantic_layer.calls == []
    assert outcome.result is None and outcome.query_error == "Giá trị “Ha Noi” của cột thanh_pho_key phải là số."


def test_a_refusal_no_new_attempt_can_mend_is_not_tried_again():
    denied = SemanticLayerRefusedError("A current approved metric contract is required.", status_code=403)
    outcome, events, model, _ = _run(_in_city("Hue"), semantic_layer=CitiesSemanticLayer(failures=[denied]))

    assert _steps(events)[3:] == ["query_metrics"]
    assert "QueryRepair" not in [schema for schema, _ in model.prompts]
    assert outcome.attempts == [
        {"attempt": 1, "kind": "not_allowed", "detail": "A current approved metric contract is required."},
    ]
    assert outcome.query_error == "A current approved metric contract is required."


def test_a_failing_look_up_still_lets_the_model_try():
    denied = SemanticLayerRefusedError("Superset không nhận truy vấn metric này.", status_code=400)
    mended = pipeline.QueryRepair(query=_plan(_in_city("Hue")), reason="Dùng tên trong dữ liệu.")
    layer = CitiesSemanticLayer()
    calls = []

    def run_metric(dataset_id, metric_id, query):
        calls.append(query)
        if query["dimensions"] == ["ten_thanh_pho"]:
            raise denied
        return layer(dataset_id, metric_id, query)

    outcome, events, _, _ = _run(_in_city("Huế"), repair=mended, semantic_layer=run_metric)

    assert _steps(events)[3:] == ["query_metrics", "explore_values", "repair_query", "query_metrics"]
    assert outcome.result["rows"] == [{"nhiet_do_cao_nhat": 23.1}]
