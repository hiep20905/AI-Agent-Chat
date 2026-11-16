import time
from datetime import date
from typing import Any

from ..exceptions import GoalError, NoJoinPathError
from .drafting import draft_goal, goal_from_draft
from .goal import Goal, grain_problems, resolve_goal
from .graph import SchemaGraph
from .linking import candidates_view, rank
from .paths import JoinTree, join_tree
from .sql import build_sql

STAGES = ("goal", "nodes", "graph", "path")


def _goal_view(goal: Goal, graph: SchemaGraph) -> dict[str, Any]:
    def where(table: int, column: str | None) -> dict[str, Any]:
        return {"dataset_id": table, "table": graph.table_name(table), "column": column}

    return {
        "measures": [
            {**where(item.table, item.column), "label": item.label, "aggregation": item.aggregation,
             "source": item.source, "indicator": item.indicator}
            for item in goal.measures
        ],
        "group_by": [
            {**where(item.table, item.column), "label": item.label, "time_grain": item.time_grain}
            for item in goal.group_by
        ],
        "filters": [
            {**where(item.table, item.column), "label": item.label, "operator": item.operator, "value": item.value,
             "source": item.source}
            for item in goal.filters
        ],
        "row_kinds": [{**where(item.table, item.column), "label": item.label, "value": item.value, "grain": item.grain}
                      for item in goal.row_kinds],
        "limit": goal.limit,
        "order": goal.order,
    }


def _node_reasons(goal: Goal) -> dict[int, list[str]]:
    reasons: dict[int, list[str]] = {}
    for item in goal.measures:
        reasons.setdefault(item.table, []).append(f"Đo {item.aggregation} {item.label}")
    for item in goal.group_by:
        grain = f" ({item.time_grain})" if item.time_grain else ""
        reasons.setdefault(item.table, []).append(f"Chia theo {item.label}{grain}")
    for item in goal.filters:
        reasons.setdefault(item.table, []).append(f"Lọc {item.label}")
    for item in goal.row_kinds:
        reasons.setdefault(item.table, []).append(f"Loại dòng {item.label}")
    return reasons


def graph_view(graph: SchemaGraph) -> dict[str, Any]:
    """The tables and edges the page draws, without the columns, coded values and indicators the search reads."""
    return {
        "tables": [
            {"dataset_id": table_id, "table": table["table"], "label": table.get("label"), "layer": table.get("layer")}
            for table_id, table in sorted(graph.tables.items())
        ],
        "edges": [
            {"id": edge.id, "from": edge.from_table, "from_column": edge.from_column, "to": edge.to_table,
             "to_column": edge.to_column, "cardinality": edge.cardinality}
            for edge in graph.edges
        ],
        "excluded": graph.excluded,
    }


def _path_view(tree: JoinTree, graph: SchemaGraph) -> dict[str, Any]:
    return {
        "algorithm": tree.algorithm,
        "root": tree.root,
        "cost": tree.cost,
        "fanout": tree.fanout,
        "tables": tree.tables,
        "steps": [
            {
                "edge": step.edge.id, "from": step.start, "to": step.end, "forward": step.forward,
                "fanout": step.fanout, "cost": step.cost,
                "on": f"{graph.table_name(step.start)}.{step.start_column} = "
                      f"{graph.table_name(step.end)}.{step.end_column}",
            }
            for step in tree.steps
        ],
        "alternatives": {str(table): options for table, options in tree.alternatives.items()},
    }


def analyze(raw_goal: dict[str, Any], graph: SchemaGraph) -> dict[str, Any]:
    """The trace of one analysis; `status` is `ok` with SQL, or why it stopped, with `message`."""
    trace: dict[str, Any] = {
        "status": "ok", "message": None, "sql": None,
        "stages": {name: {"status": "pending", "ms": None} for name in STAGES},
    }

    def stage(name: str, started: float, status: str = "done", **found: Any) -> None:
        trace["stages"][name] = {"status": status, "ms": round((time.monotonic() - started) * 1000, 1), **found}

    def stop(status: str, message: str) -> dict[str, Any]:
        trace["status"], trace["message"] = status, message
        return trace

    started = time.monotonic()
    try:
        goal = resolve_goal(raw_goal, graph)
    except GoalError as error:
        stage("goal", started, "failed", error=str(error))
        return stop("invalid_goal", str(error))
    stage("goal", started, **_goal_view(goal, graph))

    started = time.monotonic()
    stage("nodes", started, root=goal.root, terminals=goal.terminals,
          reasons={str(table): reasons for table, reasons in _node_reasons(goal).items()})

    started = time.monotonic()
    stage("graph", started, **graph_view(graph))

    started = time.monotonic()
    try:
        tree = join_tree(graph, goal.root, goal.terminals)
    except NoJoinPathError as error:
        names = ", ".join(graph.table_name(table) for table in error.unreachable)
        message = f"Không có quan hệ đã duyệt nối tới: {names}."
        stage("path", started, "failed", error=message, unreachable=error.unreachable)
        return stop("no_path", f"Chưa nối được dữ liệu: không có quan hệ đã duyệt nào dẫn tới {names}.")
    path = _path_view(tree, graph)
    problems = grain_problems(goal, tree.tables, graph)
    if tree.fanout:
        stage("path", started, "failed", **path, error="Đường nối đi ngược chiều một → nhiều nên sẽ nhân dòng.")
        return stop("fanout", "Đường nối duy nhất sẽ nhân dòng; cần gộp trước ở bảng gốc (chưa hỗ trợ).")
    if problems:
        stage("path", started, "failed", **path, problems=problems)
        return stop(problems[0]["kind"], problems[0]["message"])
    stage("path", started, **path)
    trace["sql"] = build_sql(goal, tree, graph)
    return trace


def link_question(
    question: str, graph: SchemaGraph, model: Any, today: date, feedback: str | None = None,
) -> dict[str, Any]:
    """The trace of one question taken from words to SQL over base tables (docs/specs/schema-link.md §5.1).

    `linking` holds the ranked candidates, found without a model, and the goal the model drafted from them; the stages
    after it are those of `analyze`. `status` is `not_found` when no candidate holds what the question measures.
    `feedback` tells the model why the goal it drafted before failed, when this is another try.
    """
    started = time.monotonic()
    candidates = candidates_view(rank(question, graph), graph)
    ranked_ms = round((time.monotonic() - started) * 1000, 1)
    started = time.monotonic()
    draft = draft_goal(question, candidates, today, model, feedback)
    linking = {
        "question": question, "candidates": candidates, "ranked_ms": ranked_ms, "draft": draft.model_dump(),
        "drafted_ms": round((time.monotonic() - started) * 1000, 1),
    }
    if not draft.found or not draft.measures:
        reason = draft.reason or "Không bảng nào trong kho chứa thứ câu hỏi cần đo."
        return {
            "status": "not_found", "message": reason, "sql": None, "linking": linking,
            "stages": {name: {"status": "pending", "ms": None} for name in STAGES},
        }
    goal = goal_from_draft(draft)
    return {**analyze(goal, graph), "linking": {**linking, "goal": goal}}
