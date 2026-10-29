from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .contracts import ContractMetricFormula, ContractMetricTerm

# What one row is, by grain, so a count of rows reads as "số ngày" or "số giờ".
GRAIN_WORDS = {
    "instant": "lần quan sát",
    "hour": "giờ",
    "day": "ngày",
    "week": "tuần",
    "month": "tháng",
    "quarter": "quý",
    "year": "năm",
}
_AGGREGATE_WORDS = {
    "SUM": "tổng của",
    "AVG": "trung bình của",
    "MAX": "giá trị lớn nhất của",
    "MIN": "giá trị nhỏ nhất của",
    "COUNT": "số giá trị của",
}
_COMPARISONS = {exp.EQ: "là", exp.NEQ: "khác", exp.GT: "lớn hơn", exp.LT: "nhỏ hơn", exp.LTE: "không quá"}


@dataclass(frozen=True)
class RowKindGrain:
    """The grain of the rows that hold one value of the dataset's row kind column; `value` None covers every row."""

    value: str | None
    grain: str
    label: str | None = None


@dataclass(frozen=True)
class FormulaVocabulary:
    """The words a dataset's data dictionary gives its columns and rows."""

    column_labels: dict[str, str] = field(default_factory=dict)
    row_kind_column: str | None = None
    grains: tuple[RowKindGrain, ...] = ()

    def label(self, column_name: str) -> str:
        return self.column_labels.get(column_name) or column_name

    def grain_of(self, row_kind_value: str | None) -> RowKindGrain | None:
        return next((grain for grain in self.grains if grain.value == row_kind_value), None)


def _unwrapped(node: Any) -> Any:
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(condition: str | None) -> list[Any]:
    """The parts of a condition joined by a top-level AND, each kept whole."""
    if not condition:
        return []
    try:
        tree = _unwrapped(sqlglot.parse_one(condition, read="postgres"))
    except SqlglotError:
        return []
    if isinstance(tree, exp.And):
        return [*_conjuncts(tree.left.sql(dialect="postgres")), *_conjuncts(tree.right.sql(dialect="postgres"))]
    return [tree]


def _value(node: Any) -> str:
    if isinstance(node, exp.Literal):
        return f"“{node.this}”" if node.is_string else node.this
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return f"-{node.this.this}"
    if isinstance(node, exp.Boolean):
        return "đúng" if node.this else "sai"
    return node.sql(dialect="postgres")


def _told(node: Any, vocabulary: FormulaVocabulary) -> str:
    """One condition in words, with the dictionary's column labels; anything unusual keeps its SQL."""
    node = _unwrapped(node)
    if isinstance(node, (exp.And, exp.Or)):
        joiner = " và " if isinstance(node, exp.And) else " hoặc "
        return f"{_told(node.left, vocabulary)}{joiner}{_told(node.right, vocabulary)}"
    if isinstance(node, exp.Not):
        inner = _unwrapped(node.this)
        if isinstance(inner, exp.Is) and isinstance(inner.this, exp.Column) and isinstance(inner.expression, exp.Null):
            return f"{vocabulary.label(inner.this.name)} có giá trị"
        if isinstance(inner, exp.In) and isinstance(inner.this, exp.Column):
            values = ", ".join(_value(item) for item in inner.expressions)
            return f"{vocabulary.label(inner.this.name)} không thuộc {values}"
        return f"không phải ({_told(inner, vocabulary)})"
    if isinstance(node, exp.Is) and isinstance(node.this, exp.Column) and isinstance(node.expression, exp.Null):
        return f"{vocabulary.label(node.this.name)} trống"
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        return f"{vocabulary.label(node.this.name)} thuộc {', '.join(_value(item) for item in node.expressions)}"
    if isinstance(node, exp.GTE) and isinstance(node.this, exp.Column):
        return f"{vocabulary.label(node.this.name)} từ {_value(node.expression)} trở lên"
    word = _COMPARISONS.get(type(node))
    if word and isinstance(node.this, exp.Column):
        return f"{vocabulary.label(node.this.name)} {word} {_value(node.expression)}"
    return node.sql(dialect="postgres")


def _row_kind_value(node: Any, vocabulary: FormulaVocabulary) -> str | None:
    """The row kind a condition such as loai_quan_sat = 'DU_BAO_NGAY' selects, or None."""
    node = _unwrapped(node)
    if not vocabulary.row_kind_column or not isinstance(node, exp.EQ):
        return None
    column, value = node.this, node.expression
    if isinstance(column, exp.Column) and column.name == vocabulary.row_kind_column and isinstance(value, exp.Literal):
        return value.this if value.is_string else None
    return None


def _row_filter(node: Any, vocabulary: FormulaVocabulary) -> str:
    told = _told(node, vocabulary)
    row_kind = _row_kind_value(node, vocabulary)
    grain = vocabulary.grain_of(row_kind) if row_kind else None
    return f"{told} ({grain.label})" if grain and grain.label else told


def _counts_rows(term: ContractMetricTerm) -> bool:
    """COUNT of a constant and SUM of 1 both count the rows that meet the condition."""
    if term.measure_column:
        return False
    return term.aggregation == "COUNT" or (term.aggregation == "SUM" and term.measure_constant == "1")


def _rows_word(conditions: list[Any], vocabulary: FormulaVocabulary) -> str:
    """"ngày" when the term counts rows of a daily row kind (or a dataset with one grain), else "dòng"."""
    for node in conditions:
        if (row_kind := _row_kind_value(node, vocabulary)) and (grain := vocabulary.grain_of(row_kind)):
            return GRAIN_WORDS.get(grain.grain, "dòng")
    single = vocabulary.grain_of(None)
    return GRAIN_WORDS.get(single.grain, "dòng") if single else "dòng"


def _term_text(term: ContractMetricTerm, own: list[Any], every: list[Any], vocabulary: FormulaVocabulary) -> str:
    """The term in words: `own` are the conditions it states itself; `every` also holds those moved to the note."""
    conditions = " và ".join(_told(node, vocabulary) for node in own)
    if _counts_rows(term):
        text = f"số {_rows_word(every, vocabulary)}"
        return f"{text} có {conditions}" if conditions else text
    word = _AGGREGATE_WORDS.get(term.aggregation, term.aggregation.lower())
    measure = vocabulary.label(term.measure_column) if term.measure_column else f"giá trị {term.measure_constant}"
    text = f"{word} {measure}"
    return f"{text}, chỉ tính dòng có {conditions}" if conditions else text


def read_formula(formula: ContractMetricFormula, vocabulary: FormulaVocabulary) -> dict[str, Any]:
    """The formula in business words, for a reviewer: each aggregate as a phrase and the row filters set apart.

    A condition the numerator and denominator share, and a row kind condition, go to `only_rows`, so the phrases stay
    short. A plain metric that does not count rows moves all its conditions there.
    """
    numerator_conditions = _conjuncts(formula.numerator.condition)
    denominator_conditions = _conjuncts(formula.denominator.condition) if formula.denominator else []
    if formula.denominator:
        denominator_sql = {node.sql(dialect="postgres") for node in denominator_conditions}
        shared = [node for node in numerator_conditions if node.sql(dialect="postgres") in denominator_sql]
    elif _counts_rows(formula.numerator):
        shared = []
    else:
        shared = list(numerator_conditions)
    moved = list(shared)
    moved_sql = {node.sql(dialect="postgres") for node in moved}
    for node in [*numerator_conditions, *denominator_conditions]:
        if _row_kind_value(node, vocabulary) and node.sql(dialect="postgres") not in moved_sql:
            moved.append(node)
            moved_sql.add(node.sql(dialect="postgres"))

    def own(conditions: list[Any]) -> list[Any]:
        return [node for node in conditions if node.sql(dialect="postgres") not in moved_sql]

    numerator = _term_text(formula.numerator, own(numerator_conditions), numerator_conditions, vocabulary)
    denominator = (
        _term_text(formula.denominator, own(denominator_conditions), denominator_conditions, vocabulary)
        if formula.denominator else None
    )
    return {
        "scale": format(float(formula.scale), "g") if formula.scale else None,
        "numerator": numerator,
        "denominator": denominator,
        "only_rows": [_row_filter(node, vocabulary) for node in moved],
    }
