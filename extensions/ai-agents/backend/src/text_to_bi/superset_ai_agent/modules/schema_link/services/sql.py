from typing import Any

from sqlglot import exp

from ...chat.services.query import whole_day_filters
from .goal import Goal
from .graph import SchemaGraph
from .paths import JoinTree

DIALECTS = {"postgresql": "postgres", "postgres": "postgres", "mysql": "mysql", "sqlite": "sqlite"}
_AGGREGATES = {"SUM": exp.Sum, "AVG": exp.Avg, "MIN": exp.Min, "MAX": exp.Max}
_COMPARISONS = {"==": exp.EQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}


def _literal(value: Any) -> exp.Expression:
    if isinstance(value, bool):
        return exp.Boolean(this=value)
    if isinstance(value, (int, float)):
        return exp.Literal.number(value)
    return exp.Literal.string(str(value))


def _condition(column: exp.Expression, operator: str, value: Any) -> exp.Expression:
    if operator in ("IN", "NOT IN"):
        membership = exp.In(this=column, expressions=[_literal(item) for item in value])
        return exp.Not(this=membership) if operator == "NOT IN" else membership
    return _COMPARISONS[operator](this=column, expression=_literal(value))


def _alias(name: str, taken: set[str]) -> str:
    candidate, number = name, 2
    while candidate in taken:
        candidate, number = f"{name}_{number}", number + 1
    taken.add(candidate)
    return candidate


def build_sql(goal: Goal, tree: JoinTree, graph: SchemaGraph) -> str:
    """The SELECT answering the goal over the join tree.

    A table reached against a key (one root row to many of its rows) is never joined, since each root row would be
    counted once per matching row: its filters become `EXISTS (SELECT 1 …)` on the table it hangs from, so a root row
    is kept once when at least one related row passes them. Grouping by such a table has no single right answer and is
    refused before this (`grouped_behind_fanout`).
    """
    dialect = DIALECTS.get(graph.dialect, "postgres")
    aliases = {table: f"t{index}" for index, table in enumerate(tree.tables)}
    behind = tree.behind_fanout()

    def table(table_id: int) -> exp.Expression:
        node = graph.tables[table_id]
        return exp.table_(node["table"], db=node.get("schema"), alias=aliases[table_id], quoted=True)

    def column(table_id: int, name: str) -> exp.Expression:
        return exp.column(name, table=aliases[table_id], quoted=True)

    def conditions_on(tables: set[int]) -> list[exp.Expression]:
        found = []
        for item in goal.filters:
            if item.table not in tables:
                continue
            timestamp = (graph.column(item.table, item.column) or {}).get("temporal_kind") == "timestamp"
            sent = {"column": item.column, "operator": item.operator, "value": item.value}
            for part in whole_day_filters(sent, timestamp):
                found.append(_condition(column(item.table, item.column), part["operator"], part["value"]))
        found += [
            _condition(column(item.table, item.column), "==", item.value)
            for item in goal.row_kinds if item.table in tables
        ]
        return found

    query = exp.select().from_(table(tree.root))
    for step in tree.steps:
        if step.end not in behind:
            joined_on = exp.EQ(this=column(step.start, step.start_column), expression=column(step.end, step.end_column))
            query = query.join(table(step.end), on=joined_on)
    semi_joins = []
    for branch in dict.fromkeys(behind.values()):
        tables = {table_id for table_id, first in behind.items() if first is branch}
        related = exp.select(exp.Literal.number(1)).from_(table(branch.end))
        for step in tree.steps:
            if step is not branch and step.end in tables:
                joined_on = exp.EQ(
                    this=column(step.start, step.start_column), expression=column(step.end, step.end_column),
                )
                related = related.join(table(step.end), on=joined_on)
        linked = exp.EQ(
            this=column(branch.end, branch.end_column), expression=column(branch.start, branch.start_column),
        )
        semi_joins.append(exp.Exists(this=related.where(exp.and_(linked, *conditions_on(tables)))))

    taken: set[str] = set()
    groups = []
    measured: list[str] = []
    for item in goal.group_by:
        expression = column(item.table, item.column)
        if item.time_grain:
            expression = exp.Anonymous(this="DATE_TRUNC", expressions=[exp.Literal.string(item.time_grain), expression])
        groups.append(expression)
        query = query.select(exp.alias_(expression.copy(), _alias(item.column, taken), quoted=True))
    for measure in goal.measures:
        if measure.column is None:
            aggregated = exp.Count(this=exp.Star())
        elif measure.aggregation == "COUNT":
            aggregated = exp.Count(this=column(measure.table, measure.column))
        elif measure.aggregation == "COUNT_DISTINCT":
            aggregated = exp.Count(this=exp.Distinct(expressions=[column(measure.table, measure.column)]))
        else:
            aggregated = _AGGREGATES[measure.aggregation](this=column(measure.table, measure.column))
        name = (measure.indicator or {}).get("code") or f"{measure.aggregation.lower()}_{measure.column or 'dong'}"
        measured.append(_alias(name, taken))
        query = query.select(exp.alias_(aggregated, measured[-1], quoted=True))

    conditions = conditions_on(set(tree.tables) - set(behind)) + semi_joins
    if conditions:
        query = query.where(exp.and_(*conditions))
    if groups:
        query = query.group_by(*[expression.copy() for expression in groups])
    if groups and goal.order:
        # A ranking ("nhiều nhất", "top 3"): the first measure decides, the groups only break ties.
        ranked = exp.Ordered(this=exp.column(measured[0], quoted=True), desc=goal.order == "desc")
        query = query.order_by(ranked, *[expression.copy() for expression in groups])
    elif groups:
        query = query.order_by(*[expression.copy() for expression in groups])
    return query.limit(goal.limit).sql(dialect=dialect, pretty=True)


def grouped_behind_fanout(goal: Goal, tree: JoinTree) -> list[str]:
    """The groupings on a table reached against a key: a root row would belong to several groups at once."""
    behind = tree.behind_fanout()
    return [item.label for item in goal.group_by if item.table in behind]


def build_dataset_sql(goal: Goal, tree: JoinTree, graph: SchemaGraph) -> tuple[str, list[str | None], list[str]]:
    """A reusable virtual dataset behind the goal: one row per row of the root table, joined as the tree joins, with the
    columns the goal measures, groups or filters by and each joined table's time column.

    Only the conditions that define what a row is are kept (the indicator's code, the kind of row); the asked period
    and place are left to whoever queries the dataset, so it answers other periods and places too. Returns the SQL,
    per measure the name of the column it aggregates (None when it counts rows), and every column name selected.
    """
    dialect = DIALECTS.get(graph.dialect, "postgres")
    aliases = {table: f"t{index}" for index, table in enumerate(tree.tables)}

    def table(table_id: int) -> exp.Expression:
        node = graph.tables[table_id]
        return exp.table_(node["table"], db=node.get("schema"), alias=aliases[table_id], quoted=True)

    def column(table_id: int, name: str) -> exp.Expression:
        return exp.column(name, table=aliases[table_id], quoted=True)

    query = exp.select().from_(table(tree.root))
    for step in tree.steps:
        joined_on = exp.EQ(this=column(step.start, step.start_column), expression=column(step.end, step.end_column))
        query = query.join(table(step.end), on=joined_on)

    wanted = [(measure.table, measure.column) for measure in goal.measures if measure.column]
    wanted += [(item.table, item.column) for item in goal.group_by]
    wanted += [(item.table, item.column) for item in goal.filters if item.source != "chỉ số"]
    wanted += [
        (table_id, (graph.tables[table_id].get("time") or {}).get("column")) for table_id in tree.tables
    ]
    taken: set[str] = set()
    names: dict[tuple[int, str], str] = {}
    for table_id, name in wanted:
        if name and (table_id, name) not in names and graph.column(table_id, name) is not None:
            names[(table_id, name)] = _alias(name, taken)
            query = query.select(exp.alias_(column(table_id, name), names[(table_id, name)], quoted=True))

    conditions = [
        _condition(column(item.table, item.column), item.operator, item.value)
        for item in goal.filters if item.source == "chỉ số"
    ]
    conditions += [_condition(column(item.table, item.column), "==", item.value) for item in goal.row_kinds]
    if conditions:
        query = query.where(exp.and_(*conditions))
    measured = [names.get((measure.table, measure.column)) if measure.column else None for measure in goal.measures]
    return query.sql(dialect=dialect, pretty=True), measured, list(names.values())
