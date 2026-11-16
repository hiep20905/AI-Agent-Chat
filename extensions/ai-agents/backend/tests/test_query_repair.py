import json

import pytest
from test_agent_pipeline import TODAY, WEEK_FILTERS, FakeModel, _answers

from text_to_bi.superset_ai_agent.modules.chat.services import pipeline
from text_to_bi.superset_ai_agent.modules.chat.services.repair import (
    MAX_QUERY_ATTEMPTS,
    closest_value,
    empty_result_kind,
    normalized_text,
    ranked_values,
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
    ("Buôn Ma Thuật", ["Buon Ma Thuot", "Ha Noi"], "Buon Ma Thuot"),
    ("Ha", ["Ha Noi", "Ha Long"], None),
    ("Paris", ["Ha Noi", "Hue"], None),
    # A code one digit away is another record, however alike it reads.
    ("DH000123", ["DH000124", "DH000999"], None),
    ("dh-000123", ["DH000123", "DH000124"], "DH000123"),
    # A short name inside a longer one is too little to stand for it.
    ("Phú", ["Phu Quoc", "Ha Noi"], None),
    ("Nam", ["Mien Nam", "Mien Bac"], None),
    ("Nguyễn Văn An", ["Nguyen Van Anh", "Tran Thi Binh"], "Nguyen Van Anh"),
])
def test_the_closest_stored_value_is_the_only_one_the_asked_value_can_stand_for(asked, stored, found):
    assert closest_value(asked, stored) == found


def test_a_list_cut_short_takes_only_the_same_text_since_the_asked_value_may_be_left_out():
    assert closest_value("Nguyễn Văn An", ["Nguyen Van Anh"], exact_only=True) is None
    assert closest_value("Huế", ["Ha Noi", "Hue"], exact_only=True) == "Hue"


def test_the_values_shown_are_those_most_like_the_asked_ones():
    stored = [f"Khach hang {index:03d}" for index in range(500)] + ["Cong ty Hoa Binh", "Cong ty Hoa Phat"]

    shown = ranked_values(["Công ty Hòa Phát"], stored, 3)

    assert shown[0] == "Cong ty Hoa Phat" and shown[1] == "Cong ty Hoa Binh" and len(shown) == 3


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
    near = {"dataset_id": 12, "filters": [{"column": "ten_thanh_pho", "operator": "==", "value": "Huee"}]}
    assert respelled_plan(near, {"ten_thanh_pho": ["Hue"]})[1] == ["“Huee” → “Hue”"]
    assert respelled_plan(near, {"ten_thanh_pho": ["Hue"]}, frozenset({"ten_thanh_pho"})) == (None, [])


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
    assert explored["result"] == {"ten_thanh_pho": {"count": 2, "truncated": False, "closest": ["Hue", "Ha Noi"]}}
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


def test_a_near_spelling_in_a_list_cut_short_goes_to_the_model_rather_than_being_taken():
    mended = pipeline.QueryRepair(query=_plan(_in_city("Hue")), reason="Chọn Hue trong danh sách.")
    layer = CitiesSemanticLayer()

    def run_metric(dataset_id, metric_id, query):
        answer = layer(dataset_id, metric_id, query)
        return {**answer, "truncated": query["dimensions"] == ["ten_thanh_pho"]}

    outcome, events, model, _ = _run(_in_city("Huee"), repair=mended, semantic_layer=run_metric)

    assert _steps(events)[3:] == ["query_metrics", "explore_values", "repair_query", "query_metrics"]
    assert '"truncated": true' in model.prompt("QueryRepair")
    assert outcome.result["rows"] == [{"nhiet_do_cao_nhat": 23.1}]


class CountingSemanticLayer(CitiesSemanticLayer):
    """Counts rainy days: a city the data does not hold as written counts 0 days, in one row, as a count does."""

    def __call__(self, dataset_id, metric_id, query):
        self.calls.append(query)
        if query["dimensions"] == ["ten_thanh_pho"]:
            return {"rows": [{"ten_thanh_pho": "Ha Noi", "nhiet_do_cao_nhat": "5"},
                             {"ten_thanh_pho": "Hue", "nhiet_do_cao_nhat": "0"}]}
        cities = [item["value"] for item in query["filters"] if item["column"] == "ten_thanh_pho"]
        return {"rows": [{"nhiet_do_cao_nhat": "5" if cities == ["Ha Noi"] else "0"}]}


def test_a_count_of_zero_while_a_name_may_be_misspelled_is_respelled_and_run_again():
    outcome, events, _, _ = _run(_in_city("Hà Nội"), semantic_layer=CountingSemanticLayer())

    assert _steps(events)[3:] == ["query_metrics", "explore_values", "query_metrics"]
    assert outcome.result["rows"][0]["nhiet_do_cao_nhat"] == 5
    assert [attempt["kind"] for attempt in outcome.attempts] == ["value_mismatch"]


def test_a_count_of_zero_over_a_name_the_data_holds_as_written_is_the_answer():
    outcome, events, _, _ = _run(_in_city("Hue"), semantic_layer=CountingSemanticLayer())

    assert _steps(events)[3:] == ["query_metrics", "explore_values"]
    assert outcome.result["rows"][0]["nhiet_do_cao_nhat"] == 0 and outcome.attempts == []
    assert "repair_query" not in _steps(events)


def test_a_figure_outside_what_its_unit_allows_is_answered_with_a_warning():
    from text_to_bi.superset_ai_agent.modules.chat.services.plausibility import implausible_values

    result = {
        "metrics": [{"name": "do_am", "label": "Độ ẩm trung bình", "unit": "%"},
                    {"name": "mua", "label": "Tổng lượng mưa", "unit": "mm"},
                    {"name": "nhiet", "label": "Nhiệt độ", "unit": "°C"},
                    {"name": "dem", "label": "Số lần", "unit": None}],
        "rows": [{"do_am": 81.4, "mua": 3.2, "nhiet": 27.0, "dem": -5},
                 {"do_am": 120.0, "mua": -1.5, "nhiet": 25.0, "dem": 3}],
    }

    assert implausible_values(result) == [
        "Độ ẩm trung bình có giá trị 120 %, ngoài khoảng hợp lý (từ 0 đến 100 %).",
        "Tổng lượng mưa có giá trị -1.5 mm, ngoài khoảng hợp lý (từ 0 mm trở lên).",
    ]


def test_a_suspicious_figure_reaches_the_result_notes_and_the_answer():
    class WetSemanticLayer(CitiesSemanticLayer):
        def __call__(self, dataset_id, metric_id, query):
            self.calls.append(query)
            return {"rows": [{"nhiet_do_cao_nhat": "75"}]}

    outcome, _, model, _ = _run(_in_city("Hue"), semantic_layer=WetSemanticLayer())

    assert outcome.result["suspicious"] == [
        "Nhiệt độ cao nhất có giá trị 75 °C, ngoài khoảng hợp lý (từ -90 đến 60 °C).",
    ]
    assert outcome.result["notes"][-1].startswith("Cần kiểm tra: Nhiệt độ cao nhất")
    assert _final_context(model)["result"]["suspicious"]
