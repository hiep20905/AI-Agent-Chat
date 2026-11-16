from typing import Any

from ....common.utils.text import normalized_text

# The values a figure in each unit can take (`suspicious_value` in docs/specs/xu-ly-loi-truy-van.md): a share outside
# 0–100 %, a negative amount of rain or speed, a temperature or pressure no weather has. Units are read without accents
# or case, so "°C", "độ C" and "C" are the same; a unit not listed is not checked.
PLAUSIBLE_RANGES = {
    "%": (0, 100), "pct": (0, 100), "phan tram": (0, 100),
    "c": (-90, 60), "°c": (-90, 60), "do c": (-90, 60),
    "hpa": (800, 1100),
    "mm": (0, None), "cm": (0, None), "m": (0, None), "km": (0, None),
    "km/h": (0, None), "kmh": (0, None), "m/s": (0, None),
    "ngay": (0, None), "gio": (0, None), "lan": (0, None),
}


def _range(unit: Any) -> tuple[float | None, float | None] | None:
    if not unit:
        return None
    return PLAUSIBLE_RANGES.get(unit.strip().lower()) or PLAUSIBLE_RANGES.get(normalized_text(unit))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def implausible_values(result: dict[str, Any]) -> list[str]:
    """What in the result no data of its unit can be, in words: one line per metric, with its first such value.

    Such a figure points to a wrong formula, a wrong unit, or bad data at the source; it is shown, but never stated as
    fact.
    """
    found = []
    for metric in result.get("metrics", []):
        bounds = _range(metric.get("unit"))
        if bounds is None:
            continue
        low, high = bounds
        names = [metric["name"], *([metric["compared_name"]] if metric.get("compared_name") else [])]
        values = [_number(row.get(name)) for row in result.get("rows", []) for name in names]
        outside = next(
            (value for value in values
             if value is not None and ((low is not None and value < low) or (high is not None and value > high))),
            None,
        )
        if outside is not None:
            unit = metric["unit"]
            shown = f"từ {low} đến {high} {unit}" if high is not None else f"từ {low} {unit} trở lên"
            label = metric.get("label") or metric["name"]
            found.append(f"{label} có giá trị {outside:g} {unit}, ngoài khoảng hợp lý ({shown}).")
    return found
