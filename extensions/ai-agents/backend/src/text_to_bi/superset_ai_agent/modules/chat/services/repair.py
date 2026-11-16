import difflib
import re
from typing import Any

from ....common.utils.text import normalized_text
from ...semantic_layer.exceptions import SemanticLayerRefusedError

# A metric query is run at most this many times: the first plan, then up to two repaired ones.
MAX_QUERY_ATTEMPTS = 3
TEXT_OPERATORS = ("==", "!=", "IN", "NOT IN")
# How close two spellings must be, once accents and case are set aside, for one to stand for the other.
CLOSE_MATCH_CUTOFF = 0.85
# A name inside another stands for it only when it covers this share of the longer one: "TP Hồ Chí Minh" is "Ho Chi
# Minh", but "Phú" is not "Phu Quoc" and "Nam" is not "Mien Nam".
CONTAINED_SHARE = 0.6

# The kinds of failure docs/specs/xu-ly-loi-truy-van.md names, and whether trying again can mend them.
REPAIRABLE_KINDS = ("value_mismatch", "wrong_row_kind", "invalid_plan", "rejected_query", "transient")
FINAL_KINDS = ("not_allowed", "out_of_range", "no_match")


def refusal_kind(error: SemanticLayerRefusedError) -> str:
    """A refusal from the Semantic Layer: worth running again, worth a new plan, or final."""
    if error.transient:
        return "transient"
    if error.status_code in (400, 500):
        return "rejected_query"
    return "not_allowed"


def _digits(text: str) -> list[str]:
    return re.findall(r"\d+", text)


def _contained(shorter: str, longer: str) -> bool:
    """Whether one name is the other with words around it, and covers most of it."""
    return (
        bool(shorter) and len(shorter) < len(longer)
        and f" {shorter} " in f" {longer} " and len(shorter) >= CONTAINED_SHARE * len(longer)
    )


def closest_value(asked: Any, stored: list[Any], exact_only: bool = False) -> Any | None:
    """The one stored value the asked one stands for, or None when none or several could.

    The same text without accents or case comes first ("Huế" is "Hue"); then one name inside the other, word for word
    and covering most of it ("TP Hồ Chí Minh" is "Ho Chi Minh"); then a near spelling ("Buôn Ma Thuật" is "Buon Ma
    Thuot"). Past the first, the numbers in both must be the same: a code one digit away names another record
    ("DH000123" is never "DH000124"). `exact_only` keeps the first alone, for a list the Semantic Layer cut: the value
    asked may be one it left out, and a near one shown would then stand in for it wrongly.
    """
    wanted = normalized_text(asked)
    if not wanted:
        return None
    by_text: dict[str, list[Any]] = {}
    for value in stored:
        by_text.setdefault(normalized_text(value), []).append(value)
    alike = {} if exact_only else {text: values for text, values in by_text.items() if _digits(text) == _digits(wanted)}
    for matches in (
        by_text.get(wanted, []),
        [value for text, values in alike.items() if _contained(text, wanted) or _contained(wanted, text)
         for value in values],
        [value for text in difflib.get_close_matches(wanted, list(alike), n=2, cutoff=CLOSE_MATCH_CUTOFF)
         for value in alike[text]],
    ):
        if len(matches) == 1:
            return matches[0]
        if matches:
            return None
    return None


def ranked_values(asked: list[Any], stored: list[Any], limit: int) -> list[Any]:
    """The `limit` stored values most like any asked one, likest first, so a long column still shows the right few."""
    wanted = [normalized_text(value) for value in asked]

    def likeness(value: Any) -> float:
        text = normalized_text(value)
        return max((difflib.SequenceMatcher(None, item, text).ratio() for item in wanted), default=0.0)

    return sorted(stored, key=lambda value: (-likeness(value), str(value)))[:limit]


def text_filters(filters: list[dict[str, Any]], columns: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """The checked filters that compare a text column with words, where a spelling can miss the data.

    Time filters and a rule's thresholds are left out; so is a key column, whose words were already turned into keys.
    """
    found = []
    for item in filters:
        column = columns.get(item["column"], {})
        values = item["value"] if isinstance(item["value"], list) else [item["value"]]
        if (
            item["source"] not in ("time", "expert_rule")
            and item["operator"] in TEXT_OPERATORS
            and not column.get("temporal")
            and values and all(isinstance(value, str) for value in values)
        ):
            found.append(item)
    return found


def empty_result_kind(result: dict[str, Any], columns: dict[str, dict[str, Any]]) -> str | None:
    """Why a result has no row, or None when it has rows: outside the data, a spelling that may miss, or no match."""
    if result.get("total_rows"):
        return None
    if (result.get("coverage") or {}).get("status") == "none":
        return "out_of_range"
    return "value_mismatch" if text_filters(result["filters"], columns) else "no_match"


def respelled_plan(
    plan: dict[str, Any], stored_values: dict[str, list[Any]], partial: frozenset[str] = frozenset(),
) -> tuple[dict[str, Any] | None, list[str]]:
    """The plan with each text filter value replaced by the stored value it stands for, and what was changed.

    `stored_values` holds, per column, the values the data has; on a `partial` column (a list cut short) only a value
    that is the same once accents and case are set aside is taken. None when a value matches no stored value, or when
    nothing needed changing (so trying again would only repeat the same empty query).
    """
    changes: list[str] = []
    filters = []
    for item in plan.get("filters", []):
        stored = stored_values.get(item.get("column"))
        if stored is None:
            filters.append(item)
            continue
        asked = item["value"] if isinstance(item["value"], list) else [item["value"]]
        found = []
        for value in asked:
            if value in stored:
                found.append(value)
                continue
            match = closest_value(value, stored, exact_only=item.get("column") in partial)
            if match is None:
                return None, []
            found.append(match)
            changes.append(f"“{value}” → “{match}”")
        filters.append({**item, "value": found if isinstance(item["value"], list) else found[0]})
    if not changes:
        return None, []
    return {**plan, "filters": filters}, changes
