import copy
from typing import Any

from ..exceptions import QueryPlanError
from .query import MAX_DIMENSIONS

# What each kind of question asks of a metric query, checked after the model planned it (docs/specs/taxonomy-cau-hoi.md
# §5.1). A plan missing only its form (no time split for a trend, no order for a ranking) is completed here; a plan that
# cannot answer the kind of question at all goes back to the model as an invalid plan, with what it lacks.
NEEDS_GROUPS = {
    "list": "Câu hỏi liệt kê cần nhóm theo đối tượng được liệt kê (thành phố, ngày…) trong `dimensions`.",
    "ranking": "Câu hỏi xếp hạng cần nhóm theo đối tượng được xếp (thành phố, ngày…) trong `dimensions`.",
    "breakdown": "Câu hỏi phân tích theo nhóm cần nhóm theo chiều được hỏi trong `dimensions`.",
}
NEEDS_TWO_SIDES = (
    "Câu hỏi so sánh cần `comparison` (so với kỳ khác), hoặc nhóm theo các đối tượng đem so trong `dimensions`, "
    "hoặc hai metric."
)
NO_TIME_COLUMN = "Câu hỏi diễn biến cần chia theo thời gian, nhưng metric không có cột thời gian nhóm được."
NEEDS_TWO_METRICS = "Câu hỏi tương quan cần hai metric trong `metric_ids`, đo trên cùng các ngày hoặc cùng các nhóm."
NEEDS_CONTRAST = (
    "Câu hỏi vì sao cần số liệu để đối chiếu: `comparison` với kỳ trước, hoặc chia theo nhóm trong `dimensions` để "
    "thấy phần nào thay đổi."
)


def _time_column(dataset: dict[str, Any], metrics: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The column a trend is split by: the time column the metrics declare, else the dataset's one date column."""
    columns = {column["name"]: column for column in dataset["columns"]}
    declared = [(metric.get("query_defaults") or {}).get("time_column") for metric in metrics]
    named = next((columns[name] for name in declared if name in columns and columns[name].get("groupby")), None)
    if named is not None:
        return named
    temporal = [column for column in dataset["columns"] if column.get("temporal") and column.get("groupby")]
    return temporal[0] if len(temporal) == 1 else None


def shaped_plan(
    plan: dict[str, Any] | None, intent: str | None, datasets: list[dict[str, Any]], metrics: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """The plan in the form its kind of question needs, and what was changed, in words.

    - `trend`: split by the metrics' time column (unless it compares two periods), drawn as a line;
    - `ranking`: grouped, ordered by its first metric, highest first unless the model said otherwise;
    - `list` and `breakdown`: grouped; a breakdown drawn as bars;
    - `comparison`: two sides to compare (another period, groups, or two metrics);
    - `correlation`: two metrics over the same points, split by time when the plan names no grouping;
    - `diagnostic`: something to contrast (another period, or groups), never a bare figure.
    A plan on a dataset the catalog lacks is left for the plan check to refuse.
    """
    dataset = next((item for item in datasets if plan and item["id"] == plan.get("dataset_id")), None)
    if plan is None or intent is None or dataset is None:
        return plan, []
    shaped = copy.deepcopy(plan)
    changes = []
    columns = {column["name"]: column for column in dataset["columns"]}
    dimensions = list(shaped.get("dimensions") or [])
    split_by_time = any((columns.get(name) or {}).get("temporal") for name in dimensions)
    chosen = [metric for metric in metrics if metric["dataset_id"] == dataset["id"]]
    chosen = [metric for metric in chosen if metric["metric_id"] in (shaped.get("metric_ids") or [])]

    if intent == "correlation":
        if len(shaped.get("metric_ids") or []) < 2:
            raise QueryPlanError(NEEDS_TWO_METRICS)
        if not dimensions:
            time_column = _time_column(dataset, chosen)
            if time_column is None:
                raise QueryPlanError(NEEDS_TWO_METRICS)
            shaped["dimensions"] = [time_column["name"]]
            changes.append(f"chia theo {time_column.get('label') or time_column['name']} để có các cặp giá trị")
    elif intent == "diagnostic":
        if not shaped.get("comparison") and not dimensions:
            raise QueryPlanError(NEEDS_CONTRAST)
    elif intent == "trend" and not shaped.get("comparison"):
        if not split_by_time:
            time_column = _time_column(dataset, chosen)
            if time_column is None:
                raise QueryPlanError(NO_TIME_COLUMN)
            shaped["dimensions"] = [time_column["name"], *dimensions][:MAX_DIMENSIONS]
            changes.append(f"chia theo {time_column.get('label') or time_column['name']} để thấy diễn biến")
        if shaped.get("presentation") != "line":
            shaped["presentation"] = "line"
    elif intent in NEEDS_GROUPS:
        if not dimensions:
            raise QueryPlanError(NEEDS_GROUPS[intent])
        if intent == "ranking" and not shaped.get("order_by") and shaped.get("metric_ids"):
            first = next((metric for metric in chosen if metric["metric_id"] == shaped["metric_ids"][0]), None)
            shaped["order_by"] = {"metric_id": shaped["metric_ids"][0], "direction": "desc"}
            changes.append(f"xếp theo {first['name'] if first else 'metric đầu'} từ cao xuống thấp")
        if intent == "breakdown" and shaped.get("presentation") == "number":
            shaped["presentation"] = "bar"
    elif intent == "comparison":
        if not shaped.get("comparison") and not dimensions and len(shaped.get("metric_ids") or []) < 2:
            raise QueryPlanError(NEEDS_TWO_SIDES)
    return shaped, changes
