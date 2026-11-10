from types import SimpleNamespace

import pytest

from text_to_bi.superset_ai_agent.modules.chat.exceptions import QueryPlanError
from text_to_bi.superset_ai_agent.modules.chat.services.query import combined_result, plan_query
from text_to_bi.superset_ai_agent.modules.definitions.services.conditions import (
    DefinitionConditions,
    structure_definitions,
    structured_conditions,
)
from text_to_bi.superset_ai_agent.modules.semantic_layer.services.catalog import SemanticCatalog

COLUMNS = [
    {"name": "ngay", "label": "Ngày", "type": "DATE", "temporal": True, "groupby": True, "filterable": True},
    {"name": "thanh_pho_key", "type": "INTEGER", "groupby": True, "filterable": True},
    {"name": "toc_do_gio", "label": "Tốc độ gió tối đa", "type": "NUMERIC(6,2)", "groupby": False, "filterable": True},
]
DATASET = {
    "id": 12, "name": "fact", "columns": COLUMNS,
    "labelled_values": [{"column": "thanh_pho_key", "label": "Thành phố", "values": [
        {"value": 1, "label": "Ha Noi"}, {"value": 2, "label": "Hue"}, {"value": 3, "label": "Vinh"},
    ]}],
    "time_ranges": [{"column": "ngay", "min": "2026-10-12", "max": "2026-11-01"}],
}
METRICS = [
    {"dataset_id": 12, "metric_id": 20, "name": "Tổng lượng mưa", "metric_name": "tong_luong_mua", "unit": "mm"},
    {"dataset_id": 12, "metric_id": 23, "name": "Tốc độ gió", "metric_name": "toc_do_gio_toi_da", "unit": "km/h"},
]
WEEK = [
    {"column": "ngay", "operator": ">=", "value": "2026-10-19", "source": "time"},
    {"column": "ngay", "operator": "<=", "value": "2026-10-25", "source": "time"},
]
STORED_WIND = {
    "name": "Ngày gió mạnh", "dataset_id": 12, "source": "user_definition",
    "filters": [{"column": "toc_do_gio", "operator": ">=", "value": 45}],
}


def _plan(**changes):
    return {"dataset_id": 12, "metric_ids": [20], "dimensions": [], "filters": list(WEEK), **changes}


def test_a_stored_user_threshold_wins_over_the_models_copy_of_it():
    retyped = {"column": "toc_do_gio", "operator": ">=", "value": 30, "source": "user_definition"}
    planned = plan_query(_plan(filters=WEEK + [retyped]), [DATASET], METRICS, [STORED_WIND])

    wind = [item for item in planned.filters if item["column"] == "toc_do_gio"]
    assert [(item["value"], item["source"]) for item in wind] == [(45, "user_definition")]


def test_rows_are_ranked_by_a_metric_and_cut_to_the_limit():
    plan = _plan(dimensions=["thanh_pho_key"], order_by={"metric_id": 20, "direction": "desc"}, limit=2)
    planned = plan_query(plan, [DATASET], METRICS, [])
    rows = [{"thanh_pho_key": key, "tong_luong_mua": value} for key, value in ((1, "6"), (2, "15.8"), (3, None))]

    result = combined_result(planned, [{"rows": rows}])

    assert [row["thanh_pho_key"] for row in result["rows"]] == ["Hue", "Ha Noi"]
    assert (result["row_count"], result["total_rows"], result["limit"]) == (2, 2, 2)
    assert result["order"] == {"metric_name": "tong_luong_mua", "label": "Tổng lượng mưa", "direction": "desc"}


@pytest.mark.parametrize("changes", [
    {"order_by": {"metric_id": 23}},
    {"dimensions": ["thanh_pho_key"], "limit": 0},
    {"comparison": {"start": "2026-10-12", "end": "bad"}},
    {"comparison": {"start": "2026-10-12", "end": "2026-10-18"}, "filters": []},
    {"comparison": {"start": "2026-10-12", "end": "2026-10-18"}, "dimensions": ["ngay"]},
])
def test_an_order_limit_or_comparison_that_cannot_run_is_refused(changes):
    with pytest.raises(QueryPlanError):
        plan_query(_plan(**changes), [DATASET], METRICS, [])


def test_a_comparison_runs_the_same_query_over_the_other_period_and_pairs_the_values():
    last_week = {"start": "2026-10-12", "end": "2026-10-18", "label": "tuần trước"}
    plan = _plan(dimensions=["thanh_pho_key"], comparison=last_week)
    planned = plan_query(plan, [DATASET], METRICS, [])

    compared_filters = planned.request_body(compared=True)["filters"]
    assert compared_filters == [
        {"column": "ngay", "operator": ">=", "value": "2026-10-12"},
        {"column": "ngay", "operator": "<=", "value": "2026-10-18"},
    ]
    result = combined_result(
        planned, [{"rows": [{"thanh_pho_key": 1, "tong_luong_mua": 6}]}],
        [{"rows": [{"thanh_pho_key": 1, "tong_luong_mua": "4.5"}]}],
    )
    assert result["rows"] == [{"thanh_pho_key": "Ha Noi", "tong_luong_mua": 6, "tong_luong_mua__compare": 4.5}]
    assert result["metrics"][0]["compared_name"] == "tong_luong_mua__compare"
    assert result["comparison"]["label"] == "tuần trước" and result["comparison"]["coverage"]["status"] == "full"
    assert [query["period"] for query in result["queries"]] == [None, "tuần trước"]


def test_a_period_outside_the_data_is_told_apart_from_nothing_matching():
    september = [
        {"column": "ngay", "operator": ">=", "value": "2026-09-01", "source": "time"},
        {"column": "ngay", "operator": "<=", "value": "2026-09-30", "source": "time"},
    ]
    outside = plan_query(_plan(filters=september), [DATASET], METRICS, [])
    october = [{**september[0], "value": "2026-10-01"}, {**september[1], "value": "2026-10-31"}]
    partial = plan_query(_plan(filters=october), [DATASET], METRICS, [])
    inside = plan_query(_plan(), [DATASET], METRICS, [])

    assert outside.coverage["status"] == "none" and "12/10/2026" in outside.notes[0]
    assert partial.coverage["status"] == "partial" and partial.notes
    assert inside.coverage["status"] == "full" and inside.notes == []


class StructuringModel:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def with_structured_output(self, schema):
        def invoke(messages):
            self.calls += 1
            return self.answer

        return SimpleNamespace(invoke=invoke)


CATALOG = SemanticCatalog(metrics=METRICS, datasets=[DATASET])
WIND_DEFINITION = {"kind": "expert_rule", "name": "Ngày gió mạnh", "concept": "gió mạnh", "definition": "Gió ≥ 45."}


def test_a_definition_becomes_checked_conditions_with_their_labels():
    answer = DefinitionConditions(dataset_id=12, filters=[{"column": "toc_do_gio", "operator": ">=", "value": "45"}])

    conditions = structured_conditions(WIND_DEFINITION, CATALOG, StructuringModel(answer))

    assert conditions == {
        "dataset_id": 12,
        "filters": [{"column": "toc_do_gio", "label": "Tốc độ gió tối đa", "operator": ">=", "value": 45}],
    }


@pytest.mark.parametrize("answer", [
    DefinitionConditions(dataset_id=None, filters=[]),
    DefinitionConditions(dataset_id=99, filters=[{"column": "toc_do_gio", "operator": ">=", "value": 45}]),
    DefinitionConditions(dataset_id=12, filters=[{"column": "khong_co", "operator": ">=", "value": 45}]),
    DefinitionConditions(dataset_id=12, filters=[{"column": "toc_do_gio", "operator": ">=", "value": "mạnh"}]),
])
def test_a_definition_no_filter_can_express_keeps_no_conditions(answer):
    assert structured_conditions(WIND_DEFINITION, CATALOG, StructuringModel(answer)) is None


def test_only_qualitative_concepts_are_structured_and_the_catalog_is_read_only_for_them():
    answer = DefinitionConditions(dataset_id=12, filters=[{"column": "toc_do_gio", "operator": ">", "value": 1}])
    model = StructuringModel(answer)
    glossary = {"kind": "glossary", "name": "Khách du lịch", "concept": None, "definition": "Người lưu trú."}

    def unread_catalog():
        raise AssertionError("the catalog is not needed")

    assert structure_definitions([glossary], unread_catalog, model) == [None]
    conditions = structure_definitions([glossary, WIND_DEFINITION], lambda: CATALOG, model)
    assert conditions[0] is None and conditions[1]["dataset_id"] == 12 and model.calls == 1


HOURLY_TEMPERATURE = {
    "dataset_id": 12, "metric_id": 21, "name": "Nhiệt độ", "metric_name": "nhiet_do", "unit": "°C",
    "query_defaults": {"grain": "day", "time_column": "ngay", "native_grain": "hour"},
}
DAILY_RAIN = {**METRICS[0], "query_defaults": {"grain": "day", "time_column": "ngay", "native_grain": "day"}}


def test_a_time_grain_is_sent_only_when_every_metric_is_split_by_its_time_column():
    plan = _plan(metric_ids=[21], dimensions=["ngay"], time_grain="hour")
    by_hour = plan_query(plan, [DATASET], [HOURLY_TEMPERATURE], [])
    assert by_hour.time_grain == "hour" and by_hour.request_body()["time_grain"] == "hour"

    no_time_dimension = plan_query(_plan(metric_ids=[21], time_grain="hour"), [DATASET], [HOURLY_TEMPERATURE], [])
    assert no_time_dimension.time_grain is None and "time_grain" not in no_time_dimension.request_body()

    without_defaults = plan_query(_plan(dimensions=["ngay"], time_grain="month"), [DATASET], METRICS, [])
    assert without_defaults.time_grain is None


def test_a_grain_finer_than_the_data_is_answered_at_the_data_grain_and_said():
    planned = plan_query(_plan(metric_ids=[20], dimensions=["ngay"], time_grain="hour"), [DATASET], [DAILY_RAIN], [])
    assert planned.time_grain == "day"
    assert any("chỉ có số liệu theo ngày" in note for note in planned.notes)


def test_the_defaults_a_metric_applied_are_reported_with_the_default_period():
    planned = plan_query(_plan(filters=[]), [DATASET], METRICS, [])
    applied = {
        "filters": [
            {"column": "loai_quan_sat", "operator": "==", "value": "DU_BAO_NGAY", "source": "row_kind"},
            {"column": "ngay", "operator": ">=", "value": "2026-10-20", "source": "time_window"},
            {"column": "ngay", "operator": "<", "value": "2026-10-27", "source": "time_window"},
        ],
        "time_window": {"window": "last_7_day", "start": "2026-10-20", "end": "2026-10-27", "end_included": False},
        "time_grain": None,
    }

    result = combined_result(planned, [{"rows": [{"tong_luong_mua": 12}], "applied_defaults": applied}])

    assert result["defaults"][0]["filters"] == [applied["filters"][0]]
    assert "từ 20/10/2026 đến 26/10/2026" in result["notes"][-1]
