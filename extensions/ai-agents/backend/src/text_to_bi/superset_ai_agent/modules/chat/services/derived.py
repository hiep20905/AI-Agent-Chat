import math
from typing import Any

# Aggregates whose values add up across groups, so a group's share of the total means something.
ADDITIVE_AGGREGATIONS = ("SUM", "COUNT")
SHARE_SUFFIX = "__share"
# Fewer paired values than this say nothing about how two figures move together.
MIN_CORRELATION_POINTS = 3
CORRELATION_WORDS = ((0.7, "chặt"), (0.4, "vừa"), (0.2, "yếu"), (0.0, "gần như không có"))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def with_shares(result: dict[str, Any], aggregations: dict[str, str | None]) -> bool:
    """Add each group's share of the total, in %, beside every metric that adds up (`aggregations` by metric name).

    Only a whole, non-negative breakdown gets shares: a cut list (`truncated`) has no true total, and negative parts
    make a share meaningless. Returns whether any share was added.
    """
    if result.get("truncated") or not result.get("dimensions") or len(result.get("rows", [])) < 2:
        return False
    added = []
    for metric in list(result.get("metrics", [])):
        if aggregations.get(metric["name"]) not in ADDITIVE_AGGREGATIONS:
            continue
        values = [_number(row.get(metric["name"])) for row in result["rows"]]
        total = sum(value for value in values if value is not None)
        if total <= 0 or any(value is not None and value < 0 for value in values):
            continue
        share_name = f"{metric['name']}{SHARE_SUFFIX}"
        for row, value in zip(result["rows"], values):
            row[share_name] = None if value is None else round(100 * value / total, 2)
        added.append({
            "name": share_name, "label": f"Tỷ trọng {metric['label']}", "unit": "%", "format": None,
            "compared_name": None,
        })
    result["metrics"] = [*result["metrics"], *added]
    return bool(added)


def pearson(pairs: list[tuple[float, float]]) -> float | None:
    if len(pairs) < MIN_CORRELATION_POINTS:
        return None
    count = len(pairs)
    mean_x = sum(x for x, _ in pairs) / count
    mean_y = sum(y for _, y in pairs) / count
    covariance = sum((x - mean_x) * (y - mean_y) for x, y in pairs)
    spread_x = math.sqrt(sum((x - mean_x) ** 2 for x, _ in pairs))
    spread_y = math.sqrt(sum((y - mean_y) ** 2 for _, y in pairs))
    if spread_x == 0 or spread_y == 0:
        return None
    return covariance / (spread_x * spread_y)


def with_correlation(result: dict[str, Any]) -> dict[str, Any] | None:
    """Pearson's r between the result's first two metrics over its rows (each day, each city…), kept in
    `result["correlation"]` with a note; None when there are not two metrics or enough paired values."""
    metrics = result.get("metrics", [])
    if len(metrics) < 2:
        return None
    first, second = metrics[0], metrics[1]
    pairs = [
        (x, y) for row in result.get("rows", [])
        if (x := _number(row.get(first["name"]))) is not None and (y := _number(row.get(second["name"]))) is not None
    ]
    r = pearson(pairs)
    if r is None:
        return None
    strength = next(word for bound, word in CORRELATION_WORDS if abs(r) >= bound)
    direction = "cùng chiều" if r > 0 else "ngược chiều"
    correlation = {
        "metrics": [first["label"], second["label"]], "r": round(r, 2), "points": len(pairs),
        "strength": strength, "direction": direction,
    }
    result["correlation"] = correlation
    shown_r = f"{r:.2f}".replace(".", ",")
    note = (
        f"Hệ số tương quan Pearson giữa {first['label']} và {second['label']}: r = {shown_r} trên {len(pairs)} điểm "
        f"(tương quan {strength}, {direction}); tương quan không cho biết nguyên nhân."
    )
    result["notes"] = [*result.get("notes", []), note]
    return correlation
