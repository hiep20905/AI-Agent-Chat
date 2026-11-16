import copy
import json
from pathlib import Path

import pytest

from text_to_bi.superset_ai_agent.modules.chat.exceptions import QueryPlanError
from text_to_bi.superset_ai_agent.modules.chat.services.query import plan_query
from text_to_bi.superset_ai_agent.modules.schema_link.exceptions import GoalError
from text_to_bi.superset_ai_agent.modules.schema_link.services.goal import resolve_goal
from text_to_bi.superset_ai_agent.modules.schema_link.services.graph import SchemaGraph
from text_to_bi.superset_ai_agent.modules.schema_link.services.linking import candidates_view, rank
from text_to_bi.superset_ai_agent.modules.semantic_layer.services.catalog import dataset_entry

# A staff dataset whose name column holds personal data, as the data dictionary marks it.
STAFF = {
    "id": 30,
    "name": "nhan_vien_tram",
    "dimensions": [
        {"name": "tram", "label": "Trạm", "type": "TEXT", "groupby": True, "filterable": True},
        {"name": "ho_ten", "label": "Họ tên", "type": "TEXT", "groupby": True, "filterable": True,
         "contains_pii": True},
        {"name": "nhan_vien_key", "label": "Nhân viên", "type": "INTEGER", "groupby": True, "filterable": True,
         "contains_pii": True},
    ],
}
MEMBERS = {"dimensions": [
    {"key_column": "nhan_vien_key", "label": "Nhân viên", "members": [{"value": 1, "label": "Nguyen Van A"}]},
]}
HEADCOUNT = {"dataset_id": 30, "metric_id": 1, "name": "Số nhân viên", "metric_name": "so_nhan_vien"}


def _plan(**changes):
    return {"dataset_id": 30, "metric_ids": [1], "dimensions": [], "filters": [], **changes}


def test_a_personal_column_is_marked_and_its_labelled_values_are_left_out():
    entry = dataset_entry(STAFF, MEMBERS)

    assert [column.get("contains_pii", False) for column in entry["columns"]] == [False, True, True]
    assert entry["labelled_values"] == []


def test_a_count_over_personal_data_runs_but_grouping_or_filtering_by_a_person_does_not():
    datasets = [dataset_entry(STAFF, MEMBERS)]

    assert plan_query(_plan(dimensions=["tram"]), datasets, [HEADCOUNT], []).dimensions[0]["name"] == "tram"
    with pytest.raises(QueryPlanError, match="dữ liệu cá nhân"):
        plan_query(_plan(dimensions=["ho_ten"]), datasets, [HEADCOUNT], [])
    with pytest.raises(QueryPlanError, match="dữ liệu cá nhân"):
        plan_query(_plan(filters=[{"column": "ho_ten", "operator": "==", "value": "Nguyen Van A"}]), datasets,
                   [HEADCOUNT], [])


WEATHER = json.loads((Path(__file__).parent / "fixtures" / "schema_graph_weather.json").read_text(encoding="utf-8"))
CITIES = 10


def _graph_with_personal_city_names():
    payload = copy.deepcopy(WEATHER)
    city_table = next(table for table in payload["tables"] if table["dataset_id"] == CITIES)
    for column in city_table["columns"]:
        if column["name"] == "ten_thanh_pho":
            column["contains_pii"] = True
    return SchemaGraph.from_payload(payload)


def test_base_tables_count_rows_of_personal_data_but_never_group_or_filter_by_it():
    graph = _graph_with_personal_city_names()

    assert resolve_goal({"measures": [{"dataset_id": CITIES, "aggregation": "COUNT"}]}, graph).measures
    with pytest.raises(GoalError, match="dữ liệu cá nhân"):
        resolve_goal({"measures": [{"dataset_id": CITIES, "aggregation": "COUNT"}],
                      "group_by": [{"dataset_id": CITIES, "column": "ten_thanh_pho"}]}, graph)
    with pytest.raises(GoalError, match="dữ liệu cá nhân"):
        resolve_goal({"measures": [{"dataset_id": CITIES, "aggregation": "COUNT"}],
                      "filters": [{"dataset_id": CITIES, "column": "ten_thanh_pho", "operator": "==",
                                   "value": "Ha Noi"}]}, graph)


def test_the_model_is_told_which_candidate_column_is_personal_and_sees_none_of_its_values():
    graph = _graph_with_personal_city_names()

    view = candidates_view(rank("Số thành phố theo tên thành phố", graph), graph)

    city_table = next(table for table in view["tables"] if table["dataset_id"] == CITIES)
    name_column = next(column for column in city_table["columns"] if column["name"] == "ten_thanh_pho")
    assert name_column["contains_pii"] is True and "values" not in name_column
