import hashlib
import re
from datetime import datetime
from typing import Any

from ....common.utils.text import normalized_text
from .goal import resolve_goal
from .graph import SchemaGraph
from .paths import join_tree
from .sql import build_dataset_sql

# How the answer and the page tell a figure computed straight from base tables from one an approved metric gave.
BASE_TABLES_SOURCE = "base_tables"
DATASET_NAME_LENGTH = 60
# Aggregations a proposed dataset's metric may use: the Semantic Layer's metric contract takes no DISTINCT.
METRIC_AGGREGATIONS = ("SUM", "AVG", "MIN", "MAX", "COUNT")


def _day_or_moment(values: list[Any]) -> list[Any]:
    """Moments all at midnight read as days, as the metric answers show them; any other time keeps its moment."""
    moments = []
    for value in values:
        try:
            moments.append(datetime.fromisoformat(str(value)))
        except ValueError:
            return values
    if moments and all(moment.time() == datetime.min.time() for moment in moments):
        return [moment.date().isoformat() for moment in moments]
    return values


def _presentation(dimensions: list[dict[str, Any]]) -> str:
    if not dimensions:
        return "number"
    if any(dimension["temporal"] for dimension in dimensions):
        return "line"
    return "bar" if len(dimensions) == 1 else "table"


def base_table_result(trace: dict[str, Any], answer: dict[str, Any], graph: SchemaGraph) -> dict[str, Any]:
    """The rows a schema link SQL returned, in the shape the answer and the page read a metric result in.

    The SQL selects the goal's groupings first, then its measures, so the columns are named in that order. A figure from
    base tables is provisional: no approved metric stands behind it.
    """
    goal = trace["stages"]["goal"]
    columns = answer.get("columns", [])
    group_by = goal.get("group_by", [])
    dimensions = [
        {
            "name": name, "label": item["label"],
            "temporal": bool(item.get("time_grain"))
            or bool((graph.column(item["dataset_id"], item["column"]) or {}).get("temporal_kind")),
        }
        for name, item in zip(columns, group_by)
    ]
    metrics = [
        {
            "name": name, "label": item["label"],
            "unit": (item.get("indicator") or {}).get("unit")
            or (graph.column(item["dataset_id"], item["column"] or "") or {}).get("unit"),
            "format": None, "compared_name": None,
        }
        for name, item in zip(columns[len(group_by):], goal.get("measures", []))
    ]
    rows = [dict(row) for row in answer.get("rows", [])]
    for dimension in dimensions:
        if dimension["temporal"]:
            for row, value in zip(rows, _day_or_moment([row.get(dimension["name"]) for row in rows])):
                row[dimension["name"]] = value
    filters = [
        {
            "column": item["column"], "label": item["label"], "operator": item["operator"], "value": item["value"],
            "shown_value": item["value"], "source": "question",
        }
        for item in goal.get("filters", [])
    ]
    path = trace["stages"]["path"]
    joined = [graph.tables[table].get("label") or graph.tables[table]["table"] for table in path.get("tables", [])]
    scope = ", ".join(item["label"] for item in goal.get("filters", []) if item.get("source") != "chỉ số")
    return {
        "scope": scope or "Dữ liệu gốc",
        "presentation": _presentation(dimensions),
        "dimensions": dimensions,
        "metrics": metrics,
        "filters": filters,
        "order": None,
        "limit": None,
        "comparison": None,
        "coverage": None,
        "defaults": [],
        "rows": rows,
        "row_count": answer.get("row_count", len(rows)),
        "total_rows": answer.get("row_count", len(rows)),
        "truncated": bool(answer.get("truncated")),
        "notes": [f"Tính trực tiếp từ dữ liệu gốc: {', '.join(joined)}, nối theo quan hệ đã duyệt."],
        "queries": [{
            "metric": ", ".join(metric["label"] for metric in metrics), "dataset_id": path.get("root"), "metric_id": 0,
            "period": None, "sql": answer.get("sql"),
            "request": {
                "dimensions": [dimension["name"] for dimension in dimensions],
                "filters": [{key: item[key] for key in ("column", "operator", "value")} for item in filters],
            },
        }],
        "provisional": True,
        "source": BASE_TABLES_SOURCE,
        "join": {"tables": joined, "steps": [step["on"] for step in path.get("steps", [])]},
    }


def _identifier(text: str) -> str:
    return re.sub(r"_+", "_", normalized_text(text).replace(" ", "_")).strip("_") or "du_lieu"


def dataset_proposal(goal_raw: dict[str, Any], graph: SchemaGraph, question: str) -> dict[str, Any] | None:
    """A virtual dataset that would answer this kind of question again, with the metric it needs, for an administrator
    to create: the join without the asked period or place (`build_dataset_sql`). None when its measure cannot be a
    metric contract (a distinct count) or it measures several things.
    """
    goal = resolve_goal(goal_raw, graph)
    if len(goal.measures) != 1 or goal.measures[0].aggregation not in METRIC_AGGREGATIONS:
        return None
    tree = join_tree(graph, goal.root, goal.terminals)
    sql, measured, selected = build_dataset_sql(goal, tree, graph)
    measure = goal.measures[0]
    root = graph.tables[goal.root]
    # Counting rows: a metric contract counts a column, so it counts the first one selected.
    measured_column = measured[0] or (selected[0] if selected else None)
    if measured_column is None:
        return None
    measure_name = _identifier((measure.indicator or {}).get("code") or measure.column or f"so_dong_{root['table']}")
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()[:6]
    joined = ", ".join(graph.tables[table].get("label") or graph.tables[table]["table"] for table in tree.tables)
    return {
        "name": f"{measure_name[:DATASET_NAME_LENGTH]}_goc_{digest}",
        "label": f"{measure.label} (từ dữ liệu gốc)",
        "description": f"Mỗi dòng là một dòng của {root.get('label') or root['table']}, nối {joined} theo quan hệ đã "
                       "duyệt; AI Agent soạn khi trả lời câu hỏi từ dữ liệu gốc.",
        "sql": sql,
        "metric": {
            "metric_name": _identifier(f"{measure.aggregation}_{measure_name}")[:DATASET_NAME_LENGTH],
            "label": measure.label,
            "expression": f"{measure.aggregation}({measured_column})",
            "definition": f"{measure.label}, tính bằng {measure.aggregation} trên {measured_column}.",
            "unit": (measure.indicator or {}).get("unit") or "",
        },
        "question": question,
    }
