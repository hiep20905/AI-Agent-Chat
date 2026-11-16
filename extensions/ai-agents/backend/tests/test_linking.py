import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from text_to_bi.superset_ai_agent.modules.schema_link.services.analysis import link_question
from text_to_bi.superset_ai_agent.modules.schema_link.services.drafting import (
    FilterDraft,
    GoalDraft,
    GroupByDraft,
    MeasureDraft,
    RowKindDraft,
    goal_from_draft,
)
from text_to_bi.superset_ai_agent.modules.schema_link.services.graph import SchemaGraph
from text_to_bi.superset_ai_agent.modules.schema_link.services.linking import candidates_view, rank

WEATHER = json.loads((Path(__file__).parent / "fixtures" / "schema_graph_weather.json").read_text(encoding="utf-8"))
GRAPH = SchemaGraph.from_payload(WEATHER)
VALUES, OBSERVATIONS, CITIES, INDICATORS, DAYS, LOADS = 13, 15, 10, 6, 8, 14
TODAY = date(2026, 11, 13)


class DraftingModel:
    """Answers the drafting call with a fixed goal and keeps the messages it was given."""

    def __init__(self, draft):
        self.draft, self.messages = draft, None

    def with_structured_output(self, schema):
        def invoke(messages):
            self.messages = messages
            return self.draft

        return SimpleNamespace(invoke=invoke)


def _columns(found):
    return [(hit.entry.table, hit.entry.column) for hit in found.columns]


def _indicators(found):
    return [hit.entry.indicator["code"] for hit in found.indicators]


def _tables(found):
    return [item["dataset_id"] for item in found.tables]


def test_the_most_specific_indicator_and_a_named_city_come_first():
    found = rank("Nhiệt độ cao nhất ở Đà Nẵng", GRAPH)

    assert _indicators(found)[:2] == ["nhiet_do_cao_nhat_c", "nhiet_do_c"]
    assert found.values == [{"dataset_id": CITIES, "column": "ten_thanh_pho", "value": "Da Nang"}]
    assert _tables(found)[0] == INDICATORS and CITIES in _tables(found)


def test_an_indicator_brings_its_values_table_and_what_that_table_reaches():
    found = rank("Nhiệt độ trung bình theo giờ ở Hà Nội hôm qua", GRAPH)

    assert {VALUES, OBSERVATIONS, DAYS} <= set(_tables(found))
    context = {item["dataset_id"]: item["reasons"] for item in found.tables if item["score"] == 0.0}
    assert context[VALUES] == ["nối được bằng khoá từ bảng chứa phép đo"]


def test_a_catalog_of_indicators_is_not_matched_on_how_its_indicators_aggregate():
    found = rank("Nhiệt độ trung bình theo giờ ở Hà Nội hôm qua", GRAPH)

    assert found.values == [{"dataset_id": CITIES, "column": "ten_thanh_pho", "value": "Ha Noi"}]
    assert (INDICATORS, "cach_tong_hop") not in _columns(found)[:3]


def test_an_indicator_code_without_its_unit_names_it():
    assert _indicators(rank("Tổng lượng mưa theo vùng tháng này", GRAPH))[0] == "tong_luong_mua_mm"


def test_a_stored_code_value_and_a_measure_table_rank_first():
    found = rank("Số lần nạp dữ liệu thất bại tuần này", GRAPH)

    assert _tables(found)[0] == LOADS
    assert _columns(found)[0] == (LOADS, "trang_thai")
    assert found.values == [{"dataset_id": LOADS, "column": "trang_thai", "value": "THAT_BAI"}]


def test_a_kind_of_row_and_a_grouping_are_found_by_their_words():
    found = rank("Số quan sát hiện tại theo thành phố", GRAPH)

    assert _columns(found)[0] == (OBSERVATIONS, "loai_quan_sat")
    assert (CITIES, "ten_thanh_pho") in _columns(found)[:3]


def test_a_question_naming_nothing_in_the_schema_finds_nothing():
    found = rank("Giá vàng SJC hôm nay", GRAPH)

    assert (found.columns, found.indicators, found.values) == ([], [], [])


def test_the_model_sees_each_candidate_tables_time_grouping_and_row_kinds():
    view = candidates_view(rank("Nhiệt độ trung bình theo giờ ở Hà Nội hôm qua", GRAPH), GRAPH)

    observations = next(table for table in view["tables"] if table["dataset_id"] == OBSERVATIONS)
    names = [column["name"] for column in observations["columns"]]
    assert "thoi_diem_quan_sat" in names and "loai_quan_sat" in names and "quan_sat_id" not in names
    assert [kind["value"] for kind in observations["row_kinds"]] == ["DU_BAO_GIO", "DU_BAO_NGAY", "HIEN_TAI"]
    cities = next(table for table in view["tables"] if table["dataset_id"] == CITIES)
    city = next(column for column in cities["columns"] if column["name"] == "ten_thanh_pho")
    assert city["values"][0] == "Ha Noi" and city["matched"] == ["giá trị “Ha Noi”"]
    assert view["indicators"][0]["code"] == "nhiet_do_c"


def test_a_draft_becomes_the_goal_schema_link_resolves():
    draft = GoalDraft(
        found=True,
        measures=[MeasureDraft(indicator="nhiet_do_c"), MeasureDraft(dataset_id=LOADS, aggregation="COUNT")],
        group_by=[GroupByDraft(dataset_id=OBSERVATIONS, column="thoi_diem_quan_sat", time_grain="hour")],
        filters=[FilterDraft(dataset_id=CITIES, column="ten_thanh_pho", operator="==", value="Ha Noi")],
        row_kinds=[RowKindDraft(dataset_id=OBSERVATIONS, value="DU_BAO_GIO")],
    )

    assert goal_from_draft(draft) == {
        "measures": [{"indicator": "nhiet_do_c"}, {"dataset_id": LOADS, "aggregation": "COUNT"}],
        "group_by": [{"dataset_id": OBSERVATIONS, "column": "thoi_diem_quan_sat", "time_grain": "hour"}],
        "filters": [{"dataset_id": CITIES, "column": "ten_thanh_pho", "operator": "==", "value": "Ha Noi"}],
        "row_kinds": [{"dataset_id": OBSERVATIONS, "value": "DU_BAO_GIO"}],
    }


HANOI_YESTERDAY = GoalDraft(
    found=True,
    measures=[MeasureDraft(indicator="nhiet_do_c")],
    group_by=[GroupByDraft(dataset_id=OBSERVATIONS, column="thoi_diem_quan_sat", time_grain="hour")],
    filters=[
        FilterDraft(dataset_id=CITIES, column="ten_thanh_pho", operator="==", value="Ha Noi"),
        FilterDraft(dataset_id=OBSERVATIONS, column="thoi_diem_quan_sat", operator="==", value="2026-11-12"),
    ],
    row_kinds=[RowKindDraft(dataset_id=OBSERVATIONS, value="DU_BAO_GIO")],
    reason="Chỉ số nhiệt độ, theo giờ, ở Hà Nội hôm qua.",
)


def test_a_question_is_taken_to_the_tables_to_join_and_the_columns_to_select():
    model = DraftingModel(HANOI_YESTERDAY)

    trace = link_question("Nhiệt độ trung bình theo giờ ở Hà Nội hôm qua", GRAPH, model, TODAY)

    assert trace["status"] == "ok"
    assert set(trace["stages"]["path"]["tables"]) == {VALUES, INDICATORS, OBSERVATIONS, CITIES}
    assert trace["stages"]["nodes"]["root"] == VALUES
    sql = trace["sql"]
    assert sql.count("JOIN") == 3 and '"gia_tri_so"' in sql and '"ten_thanh_pho" = \'Ha Noi\'' in sql
    assert trace["linking"]["goal"]["measures"] == [{"indicator": "nhiet_do_c"}]
    assert trace["linking"]["candidates"]["indicators"][0]["code"] == "nhiet_do_c"
    prompt = model.messages[1][1]
    assert "today: 2026-11-13" in prompt and '"ten_thanh_pho"' in prompt and '"Ha Noi"' in prompt


def test_a_draft_naming_what_the_graph_lacks_stops_at_the_goal():
    wrong = HANOI_YESTERDAY.model_copy(update={
        "filters": [FilterDraft(dataset_id=CITIES, column="ten_tinh", operator="==", value="Ha Noi")],
    })

    trace = link_question("Nhiệt độ ở Hà Nội", GRAPH, DraftingModel(wrong), TODAY)

    assert trace["status"] == "invalid_goal" and "ten_tinh" in trace["message"]
    assert trace["sql"] is None and trace["linking"]["goal"]["filters"][0]["column"] == "ten_tinh"


def test_a_question_no_candidate_answers_is_told_and_nothing_is_built():
    nothing = GoalDraft(found=False, reason="Kho thời tiết không có giá vàng.")

    trace = link_question("Giá vàng SJC hôm nay", GRAPH, DraftingModel(nothing), TODAY)

    assert (trace["status"], trace["message"], trace["sql"]) == ("not_found", "Kho thời tiết không có giá vàng.", None)
    assert all(stage["status"] == "pending" for stage in trace["stages"].values())


def test_each_candidate_table_says_which_candidates_it_joins_to_by_keys():
    view = candidates_view(rank("Số quan sát hiện tại theo thành phố", GRAPH), GRAPH)
    reaches = {table["dataset_id"]: table.get("reaches", []) for table in view["tables"]}

    assert CITIES in reaches[OBSERVATIONS]
    assert CITIES not in reaches[LOADS]
