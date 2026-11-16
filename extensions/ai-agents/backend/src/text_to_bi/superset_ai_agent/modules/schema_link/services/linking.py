import math
from dataclasses import dataclass, field
from typing import Any

from ....common.utils.text import normalized_text
from .graph import SchemaGraph

# How many of each kind of candidate the model is shown, best first.
TOP_TABLES = 6
TOP_COLUMNS = 12
TOP_INDICATORS = 5
# How many key-to-one joins away from a table that measures something a table still counts as reachable context,
# where the question's period, place or grouping usually lives (an observation's time, a city's name).
CONTEXT_DEPTH = 2
# How many key-to-one joins away the model is told a candidate table reaches another.
REACH_DEPTH = 4
# How many stored values of a column the model is shown, besides the ones the question names.
MAX_VALUES = 12
# Roles of the columns kept for a context table: what a question groups, filters or reads time by.
CONTEXT_ROLES = ("time", "row_kind", "label", "dimension")
# A phrase of the question naming a column, a value or an indicator weighs this much per word it has, so "nhiệt độ cao
# nhất" outranks "nhiệt độ"; a single shared word only adds its rarity.
PHRASE_WEIGHT = 2.0
VALUE_WEIGHT = 3.0
# Without a phrase or a value named whole, an entry needs this many words in common with the question to count.
MIN_SHARED_WORDS = 2
# Words of a question that name nothing in a schema: question words, linking words, and words of time already read
# as a period by the model ("hôm qua", "tuần này").
STOPWORDS = frozenset({
    "la", "bao", "nhieu", "nao", "o", "cua", "trong", "theo", "cac", "co", "khong", "va", "nhung", "mot", "cho", "toi",
    "biet", "hay", "duoc", "nhu", "tai", "voi", "tu", "den", "gi", "ai", "sao", "the", "bang", "moi", "tung", "hom",
    "nay", "qua", "tuan", "thang", "nam", "truoc", "sau", "nhat",
})
# Units an indicator code may end with ("tong_luong_mua_mm"); the code without it is how a question names it.
UNIT_SUFFIXES = frozenset({"c", "mm", "cm", "m", "km", "kmh", "hpa", "pct", "h", "s"})


def _code_phrases(code: str) -> list[str]:
    parts = code.split("_")
    return [" ".join(parts), *([" ".join(parts[:-1])] if len(parts) > 1 and parts[-1] in UNIT_SUFFIXES else [])]


@dataclass
class Entry:
    """One thing a question can name: a column (by its label, synonyms or name), an indicator, or a table."""

    kind: str
    table: int
    column: str | None = None
    indicator: dict[str, Any] | None = None
    phrases: list[str] = field(default_factory=list)
    words: set[str] = field(default_factory=set)
    values: list[Any] = field(default_factory=list)


@dataclass
class Hit:
    entry: Entry
    score: float
    matched: list[str]
    named_values: list[Any] = field(default_factory=list)


@dataclass
class Candidates:
    """The tables, columns, indicators and stored values a question names, best first, with why each was kept."""

    tables: list[dict[str, Any]]
    columns: list[Hit]
    indicators: list[Hit]
    values: list[dict[str, Any]]


def _words(*texts: Any) -> set[str]:
    return {word for text in texts if text for word in normalized_text(text).split() if word not in STOPWORDS}


def _phrases(*texts: Any) -> list[str]:
    return list(dict.fromkeys(phrase for phrase in (normalized_text(text) for text in texts if text) if phrase))


def build_index(graph: SchemaGraph) -> list[Entry]:
    """Everything the question can be matched against, read from the schema graph the asker may see."""
    entries = []
    for table_id, table in graph.tables.items():
        entries.append(Entry(
            "table", table_id,
            phrases=_phrases(table.get("label"), table["table"].replace("_", " ")),
            words=_words(table.get("label"), table.get("description")),
        ))
        # A catalog of indicators describes the indicators ("TRUNG_BINH" is how one aggregates, "Gio" a unit): its
        # values are not data a question filters on, and the indicators themselves are matched below.
        catalog = (table.get("indicator_model") or {}).get("kind") == "catalog"
        for column in table.get("columns", []):
            if column.get("role") == "key" and not column.get("synonyms"):
                # A bare identifier names nothing a person asks about; joins reach it through the graph.
                continue
            entries.append(Entry(
                "column", table_id, column["name"],
                phrases=_phrases(column.get("label"), *column.get("synonyms", []), column["name"].replace("_", " ")),
                words=_words(column.get("label"), *column.get("synonyms", []), column.get("description")),
                values=[] if catalog else column.get("values", []),
            ))
        for indicator in (table.get("indicators") or {}).get("indicators", []):
            entries.append(Entry(
                "indicator", table_id, indicator=indicator,
                phrases=_phrases(indicator.get("name"), *_code_phrases(indicator.get("code", ""))),
                words=_words(indicator.get("name"), indicator.get("unit")),
            ))
    return entries


def _rarity(entries: list[Entry]) -> dict[str, float]:
    """How much a shared word says: words most entries share (such as "mã", "thời điểm") say little."""
    counts: dict[str, int] = {}
    for entry in entries:
        for word in entry.words:
            counts[word] = counts.get(word, 0) + 1
    return {word: math.log(1 + len(entries) / count) for word, count in counts.items()}


def _named(phrase: str, question: str) -> bool:
    return f" {phrase} " in f" {question} "


def _scored(entry: Entry, question: str, words: set[str], rarity: dict[str, float]) -> Hit | None:
    named_phrases = [phrase for phrase in entry.phrases if _named(phrase, question)]
    named_values = [
        value for value in entry.values
        if len(normalized_text(value)) >= 3 and _named(normalized_text(value), question)
    ]
    shared = sorted(words & entry.words)
    if not named_phrases and not named_values and len(shared) < MIN_SHARED_WORDS:
        # One word in common ("giá" in "giá vàng" and "giá trị") says too little to make a candidate.
        return None
    score = sum(rarity.get(word, 0.0) for word in shared)
    if named_phrases:
        score += PHRASE_WEIGHT * max(len(phrase.split()) for phrase in named_phrases)
    if named_values:
        score += VALUE_WEIGHT * max(len(normalized_text(value).split()) for value in named_values)
    if score <= 0:
        return None
    matched = [f"“{phrase}”" for phrase in named_phrases] + [f"giá trị “{value}”" for value in named_values]
    return Hit(entry, round(score, 2), matched or [f"từ: {', '.join(shared)}"], named_values)


def _reachable(graph: SchemaGraph, start: int, depth: int) -> set[int]:
    """The tables a table reaches by following keys, many to one, at most `depth` joins away."""
    found, frontier = {start}, {start}
    for _ in range(depth):
        frontier = {
            step.end for table in frontier for step in graph.steps_from(table, allow_fanout=False)
        } - found
        found |= frontier
    return found


def rank(question: str, graph: SchemaGraph, entries: list[Entry] | None = None) -> Candidates:
    """The schema a question names, ranked: no model, only the question's words against labels, synonyms,
    descriptions, stored values and indicator names, with accents and case set aside.

    A table counts by its best column or indicator and its own name. The tables a measured table reaches by keys are
    kept as context, since the period, place or grouping a question asks for often lives there.
    """
    entries = entries if entries is not None else build_index(graph)
    rarity = _rarity(entries)
    normalized = normalized_text(question)
    words = _words(question)
    hits = [hit for hit in (_scored(entry, normalized, words, rarity) for entry in entries) if hit]
    hits.sort(key=lambda hit: (-hit.score, hit.entry.table, hit.entry.column or ""))

    columns = [hit for hit in hits if hit.entry.kind == "column"][:TOP_COLUMNS]
    indicators = [hit for hit in hits if hit.entry.kind == "indicator"][:TOP_INDICATORS]
    table_scores: dict[int, float] = {}
    reasons: dict[int, list[str]] = {}
    kept = [*columns, *indicators, *(hit for hit in hits if hit.entry.kind == "table")]
    for hit in sorted(kept, key=lambda hit: -hit.score):
        table = hit.entry.table
        table_scores[table] = max(table_scores.get(table, 0.0), hit.score)
        what = hit.entry.column or (hit.entry.indicator or {}).get("code") or graph.table_name(table)
        reasons.setdefault(table, []).append(f"{what}: {', '.join(hit.matched)}")
    measured = {hit.entry.table for hit in indicators} | {
        hit.entry.table for hit in columns if _column_role(graph, hit) == "measure"
    }
    for hit in indicators:
        # An indicator is measured on the table keeping one value per indicator, which reaches its catalog.
        measured |= _value_tables(graph, hit.entry.table)
    ranked = sorted(table_scores, key=lambda table: -table_scores[table])[:TOP_TABLES]
    context = sorted(
        {table for start in measured for table in _reachable(graph, start, CONTEXT_DEPTH)} - set(ranked)
    )
    tables = [{"dataset_id": table, "score": table_scores[table], "reasons": reasons[table]} for table in ranked]
    tables += [
        {"dataset_id": table, "score": 0.0, "reasons": ["nối được bằng khoá từ bảng chứa phép đo"]}
        for table in context
    ]
    values = [
        {"dataset_id": hit.entry.table, "column": hit.entry.column, "value": value}
        for hit in columns for value in hit.named_values
    ]
    return Candidates(tables, columns, indicators, values)


def names_in_schema(concept: str, graph: SchemaGraph) -> bool:
    """Whether the data names a concept whole: a table's or column's label or synonym, an indicator's name, or a
    stored value ("lần nạp dữ liệu" is a table, "thất bại" a stored status)."""
    entries = build_index(graph)
    normalized = normalized_text(concept)
    return any(
        any(_named(phrase, normalized) or _named(normalized, phrase) for phrase in entry.phrases)
        or any(_named(normalized_text(value), normalized) for value in entry.values if len(normalized_text(value)) >= 3)
        for entry in entries
    )


def _column_role(graph: SchemaGraph, hit: Hit) -> str | None:
    return (graph.column(hit.entry.table, hit.entry.column) or {}).get("role")


def _value_tables(graph: SchemaGraph, catalog: int) -> set[int]:
    """The tables keeping one value per indicator of this catalog, through an approved key."""
    return {
        edge.from_table for edge in graph.edges
        if edge.to_table == catalog and (graph.tables[edge.from_table].get("indicator_model") or {}).get("kind")
        == "values"
    }


def _column_view(column: dict[str, Any], hit: Hit | None, named: list[Any]) -> dict[str, Any]:
    values = list(dict.fromkeys([*named, *column.get("values", [])]))[:MAX_VALUES]
    view = {
        "name": column["name"], "label": column.get("label"), "role": column.get("role"), "unit": column.get("unit"),
        "aggregations": column.get("aggregations") or None, "temporal_kind": column.get("temporal_kind"),
        "values": None if column.get("contains_pii") else values or None,
        "contains_pii": column.get("contains_pii") or None,
    }
    if hit is not None:
        view["score"], view["matched"] = hit.score, hit.matched
    return {key: value for key, value in view.items() if value not in (None, [])}


def candidates_view(candidates: Candidates, graph: SchemaGraph) -> dict[str, Any]:
    """What the model chooses from, and the page shows: per candidate table its matched columns and, for every
    table, the columns a question groups, filters or reads time by; then the indicators and the values named."""
    hits = {(hit.entry.table, hit.entry.column): hit for hit in candidates.columns}
    named = {}
    for item in candidates.values:
        named.setdefault((item["dataset_id"], item["column"]), []).append(item["value"])
    tables = []
    candidate_ids = {item["dataset_id"] for item in candidates.tables}
    for item in candidates.tables:
        table = graph.tables[item["dataset_id"]]
        columns = [
            _column_view(column, hits.get((item["dataset_id"], column["name"])),
                         named.get((item["dataset_id"], column["name"]), []))
            for column in table.get("columns", [])
            if (item["dataset_id"], column["name"]) in hits or column.get("role") in CONTEXT_ROLES
        ]
        time = table.get("time") or {}
        tables.append({
            "dataset_id": item["dataset_id"], "table": table["table"], "label": table.get("label"),
            "score": item["score"], "reasons": item["reasons"],
            "row_kinds": time.get("row_kinds") or None, "row_kind_column": time.get("row_kind_column"),
            # The candidate tables this one joins to by keys, so the model measures where it can group and filter.
            "reaches": sorted(
                (_reachable(graph, item["dataset_id"], REACH_DEPTH) & candidate_ids) - {item["dataset_id"]}
            ) or None,
            "columns": columns,
        })
    return {
        "tables": [{key: value for key, value in table.items() if value is not None} for table in tables],
        "indicators": [
            {**{key: hit.entry.indicator.get(key) for key in ("code", "name", "unit", "aggregation")},
             "score": hit.score, "matched": hit.matched}
            for hit in candidates.indicators
        ],
        "values": candidates.values,
    }
