import json
from pathlib import Path
from types import SimpleNamespace

from test_agent_pipeline import CATALOG, TODAY, FakeModel, _answers

from text_to_bi.superset_ai_agent.modules.chat.services import pipeline
from text_to_bi.superset_ai_agent.modules.schema_link.services.analysis import analyze
from text_to_bi.superset_ai_agent.modules.schema_link.services.answering import base_table_result, dataset_proposal
from text_to_bi.superset_ai_agent.modules.schema_link.services.drafting import (
    FilterDraft,
    GoalDraft,
    GroupByDraft,
    MeasureDraft,
    RowKindDraft,
    goal_from_draft,
)
from text_to_bi.superset_ai_agent.modules.schema_link.services.goal import resolve_goal
from text_to_bi.superset_ai_agent.modules.schema_link.services.graph import SchemaGraph
from text_to_bi.superset_ai_agent.modules.schema_link.services.paths import join_tree
from text_to_bi.superset_ai_agent.modules.schema_link.services.sql import build_dataset_sql
from text_to_bi.superset_ai_agent.modules.semantic_layer.exceptions import SemanticLayerRefusedError
from text_to_bi.superset_ai_agent.modules.semantic_layer.services.data_model import DataModelSources

WEATHER = json.loads((Path(__file__).parent / "fixtures" / "schema_graph_weather.json").read_text(encoding="utf-8"))
GRAPH = SchemaGraph.from_payload(WEATHER)
VALUES, OBSERVATIONS, CITIES, INDICATORS, LOADS = 13, 15, 10, 6, 14
HOURLY_IN_HANOI = GoalDraft(
    found=True,
    measures=[MeasureDraft(indicator="nhiet_do_c", label="Nhiệt độ trung bình")],
    group_by=[GroupByDraft(dataset_id=OBSERVATIONS, column="thoi_diem_quan_sat", time_grain="hour")],
    filters=[
        FilterDraft(dataset_id=CITIES, column="ten_thanh_pho", operator="==", value="Ha Noi"),
        FilterDraft(dataset_id=OBSERVATIONS, column="thoi_diem_quan_sat", operator="==", value="2026-11-12"),
    ],
    row_kinds=[RowKindDraft(dataset_id=OBSERVATIONS, value="DU_BAO_GIO")],
    reason="Nhiệt độ theo giờ ở Hà Nội hôm qua.",
)
HOURLY_ROWS = {
    "columns": ["thoi_diem_quan_sat", "nhiet_do_c"],
    "rows": [
        {"thoi_diem_quan_sat": "2026-11-12T00:00:00", "nhiet_do_c": 22.4},
        {"thoi_diem_quan_sat": "2026-11-12T01:00:00", "nhiet_do_c": 22.1},
    ],
    "row_count": 2, "truncated": False, "sql": "SELECT …",
}
# Nothing on an aggregate dataset measures it; the base tables live on database 1.
SOURCES = DataModelSources(aggregates=[], bases=[{"id": VALUES, "name": "tb_gia_tri_quan_sat", "database_id": 1}])


def _trace(draft):
    return analyze(goal_from_draft(draft), GRAPH)


def test_a_reusable_dataset_keeps_the_join_and_what_defines_a_row_but_not_the_asked_period_or_place():
    goal = resolve_goal(goal_from_draft(HOURLY_IN_HANOI), GRAPH)
    tree = join_tree(GRAPH, goal.root, goal.terminals)

    sql, measured, selected = build_dataset_sql(goal, tree, GRAPH)

    assert sql.count("JOIN") == 3 and "GROUP BY" not in sql and "LIMIT" not in sql
    assert "'nhiet_do_c'" in sql and "'DU_BAO_GIO'" in sql
    assert "Ha Noi" not in sql and "2026-11-12" not in sql
    assert measured == ["gia_tri_so"]
    assert {"gia_tri_so", "thoi_diem_quan_sat", "ten_thanh_pho"} <= set(selected)


def test_a_dataset_proposal_carries_the_metric_that_answers_the_question_on_its_columns():
    proposal = dataset_proposal(goal_from_draft(HOURLY_IN_HANOI), GRAPH, "Nhiệt độ theo giờ ở Hà Nội hôm qua?")

    assert proposal["name"].startswith("nhiet_do_c_goc_") and proposal["name"].replace("_", "").isalnum()
    assert proposal["metric"]["expression"] == "AVG(gia_tri_so)"
    assert proposal["metric"]["unit"] and proposal["question"] == "Nhiệt độ theo giờ ở Hà Nội hôm qua?"


def test_a_row_count_proposes_counting_a_column_and_a_distinct_count_proposes_nothing():
    counted = {"measures": [{"dataset_id": OBSERVATIONS, "aggregation": "COUNT"}],
               "group_by": [{"dataset_id": CITIES, "column": "ten_thanh_pho"}]}
    distinct = {"measures": [{"dataset_id": OBSERVATIONS, "column": "thanh_pho_id", "aggregation": "COUNT_DISTINCT"}]}

    assert dataset_proposal(counted, GRAPH, "Số quan sát?")["metric"]["expression"].startswith("COUNT(")
    assert dataset_proposal(distinct, GRAPH, "Bao nhiêu thành phố?") is None


def test_rows_from_base_tables_read_like_a_metric_result_marked_provisional():
    result = base_table_result(_trace(HOURLY_IN_HANOI), HOURLY_ROWS, GRAPH)

    assert result["dimensions"] == [{"name": "thoi_diem_quan_sat", "label": "Thời điểm quan sát", "temporal": True}]
    assert result["metrics"][0]["name"] == "nhiet_do_c" and result["metrics"][0]["unit"]
    assert result["presentation"] == "line" and result["rows"][1]["thoi_diem_quan_sat"] == "2026-11-12T01:00:00"
    assert (result["provisional"], result["source"]) == (True, "base_tables")
    assert "Ha Noi" in result["scope"] and result["notes"][0].startswith("Tính trực tiếp từ dữ liệu gốc")
    daily = base_table_result(_trace(HOURLY_IN_HANOI), {**HOURLY_ROWS, "rows": [
        {"thoi_diem_quan_sat": "2026-11-12T00:00:00", "nhiet_do_c": 22.4},
    ]}, GRAPH)
    assert daily["rows"][0]["thoi_diem_quan_sat"] == "2026-11-12"


class SequenceModel(FakeModel):
    """Drafts each goal in turn, so a test sees a second try; the other steps answer as the fake model does."""

    def __init__(self, answers, drafts):
        super().__init__(answers)
        self.drafts = list(drafts)

    def with_structured_output(self, schema):
        if schema is not GoalDraft:
            return super().with_structured_output(schema)

        def invoke(messages):
            self.prompts.append(("GoalDraft", messages))
            return self.drafts.pop(0)

        return SimpleNamespace(invoke=invoke)


class FakeBaseTables:
    """The builder's base-table steps: the weather graph, SQL answers (or failures) in turn, and kept proposals."""

    def __init__(self, answers=(HOURLY_ROWS,), graph=WEATHER, kept=None):
        self.answers, self.graph, self.sql, self.proposed = list(answers), graph, [], []
        self.kept = kept or {"proposal": {"id": 9, "name": "nhiet_do_c_goc", "status": "pending"}}

    def builder(self):
        return pipeline.MetricBuilder(
            load_sources=lambda: SOURCES,
            create_metric=lambda draft: (_ for _ in ()).throw(AssertionError("no aggregate to build on")),
            propose_dataset=self.propose,
            load_schema_graph=lambda database_id: self.graph,
            run_sql=self.run,
        )

    def run(self, sql):
        self.sql.append(sql)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def propose(self, draft):
        self.proposed.append(draft)
        return self.kept


def _run(drafts, base_tables=None, dataset_draft=None):
    base_tables = base_tables or FakeBaseTables()
    answers = _answers(question_kind="quantitative", rule_ids=(), term_ids=(), metrics=())
    if dataset_draft is not None:
        answers["DatasetDraft"] = dataset_draft
    model = SequenceModel(answers, drafts)
    events = []
    generator = pipeline.run_agent(
        "Nhiệt độ theo giờ ở Hà Nội hôm qua?", [], model, lambda: CATALOG, (), run_metric=lambda *args: {},
        today=TODAY, builder=base_tables.builder(),
    )
    while True:
        try:
            events.append(next(generator))
        except StopIteration as finished:
            return finished.value, events, model, base_tables


def _steps(events):
    return [data["name"] for name, data in events if name == "tool_start"]


def test_a_question_no_metric_measures_is_answered_from_base_tables_and_its_join_proposed_as_a_dataset():
    outcome, events, model, base_tables = _run([HOURLY_IN_HANOI])

    assert _steps(events)[3:] == ["load_data_model", "load_schema_graph", "link_question", "run_sql", "propose_dataset"]
    assert base_tables.sql[0].count("JOIN") == 3 and "'Ha Noi'" in base_tables.sql[0]
    assert outcome.result["source"] == "base_tables" and outcome.result["row_count"] == 2
    assert outcome.result["notes"][-1].startswith("Đã đề xuất quản trị viên tạo dataset")
    assert base_tables.proposed[0]["metric"]["expression"] == "AVG(gia_tri_so)"
    assert "propose_missing_items" not in _steps(events) and "draft_dataset" not in _steps(events)
    assert outcome.status == "needs_review" and outcome.missing == ["metric"]
    assert '"base_tables"' in model.prompt("final")


def test_a_join_already_kept_as_a_dataset_is_named_instead_of_proposed_again():
    kept = {"existing_dataset": {"id": 31, "name": "nhiet_do_goc"}}
    outcome, _, _, _ = _run([HOURLY_IN_HANOI], FakeBaseTables(kept=kept))

    assert outcome.result["notes"][-1] == (
        "Dữ liệu này đã có trong dataset “nhiet_do_goc”; quản trị viên chỉ cần thêm chỉ số."
    )


def test_a_goal_the_graph_rejects_is_drafted_again_with_the_reason():
    wrong = HOURLY_IN_HANOI.model_copy(update={
        "filters": [FilterDraft(dataset_id=CITIES, column="ten_tinh", operator="==", value="Ha Noi")],
    })
    outcome, events, model, _ = _run([wrong, HOURLY_IN_HANOI])

    assert _steps(events)[5:] == ["link_question", "link_question", "run_sql", "propose_dataset"]
    second = [messages for name, messages in model.prompts if name == "GoalDraft"][1][1][1]
    assert "Lần chọn trước không dùng được" in second and "ten_tinh" in second
    assert [attempt["kind"] for attempt in outcome.attempts] == ["invalid_plan"]
    assert outcome.result["source"] == "base_tables"


def test_sql_the_semantic_layer_rejects_is_drafted_again_and_a_busy_one_is_run_again():
    rejected = SemanticLayerRefusedError("Superset không nhận truy vấn.", status_code=400)
    busy = SemanticLayerRefusedError("Không kết nối được tới Semantic Layer.", transient=True)
    base_tables = FakeBaseTables(answers=[rejected, busy, HOURLY_ROWS])
    outcome, events, _, base_tables = _run([HOURLY_IN_HANOI, HOURLY_IN_HANOI], base_tables)

    assert _steps(events)[5:] == ["link_question", "run_sql", "link_question", "run_sql", "run_sql", "propose_dataset"]
    assert [attempt["kind"] for attempt in outcome.attempts] == ["rejected_query", "transient"]
    assert outcome.result["row_count"] == 2


def test_no_row_while_filtering_on_words_is_drafted_again_to_spell_as_the_data_does():
    nothing = {**HOURLY_ROWS, "rows": [], "row_count": 0}
    base_tables = FakeBaseTables(answers=[nothing, HOURLY_ROWS])
    outcome, _, model, _ = _run([HOURLY_IN_HANOI, HOURLY_IN_HANOI], base_tables)

    assert [attempt["kind"] for attempt in outcome.attempts] == ["value_mismatch"]
    second = [messages for name, messages in model.prompts if name == "GoalDraft"][1][1][1]
    assert "values" in second and outcome.result["row_count"] == 2


def test_without_approved_relationships_the_question_still_goes_to_a_dataset_proposal():
    empty_graph = {"database": {"dialect": "postgresql"}, "tables": [], "edges": []}
    dataset_draft = pipeline.DatasetDraft(name="", sql="", reason="Không soạn được.")
    outcome, events, _, base_tables = _run([], FakeBaseTables(graph=empty_graph), dataset_draft)

    assert _steps(events)[3:6] == ["load_data_model", "load_schema_graph", "draft_dataset"]
    assert base_tables.sql == [] and outcome.result is None


def test_a_question_nothing_in_the_graph_holds_is_not_tried_again():
    nothing = GoalDraft(found=False, reason="Kho không có dữ liệu này.")
    dataset_draft = pipeline.DatasetDraft(name="", sql="", reason="Không soạn được.")
    outcome, events, _, base_tables = _run([nothing], dataset_draft=dataset_draft)

    # Then, as before schema link, the dataset draft and the metric asked of administrators.
    assert _steps(events)[5:] == ["link_question", "draft_dataset", "propose_missing_items"]
    assert [attempt["kind"] for attempt in outcome.attempts] == ["not_found"] and base_tables.sql == []
