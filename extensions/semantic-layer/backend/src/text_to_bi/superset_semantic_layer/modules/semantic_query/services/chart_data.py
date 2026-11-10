import logging
from typing import Any

from superset.connectors.sqla.models import SqlaTable

from ....core.security import is_superset_access_error
from ...data_dictionary.models import ColumnDictionary
from .members import build_latest_time_payload, build_values_payload, coded_value_columns, coded_values_from_rows

logger = logging.getLogger(__name__)


def latest_moment(dataset: SqlaTable, column: str, filters: list[dict[str, Any]]) -> Any:
    """The latest value of a time column among the rows the ChartData filters keep, read as the caller.

    A metric's time window anchored to the latest data counts back from it; None when it cannot be read, and the window
    then counts back from now.
    """
    from superset.charts.schemas import ChartDataQueryContextSchema
    from superset.commands.chart.data.get_data_command import ChartDataCommand

    payload = build_latest_time_payload(dataset, column, filters)
    if payload is None:
        return None
    try:
        command = ChartDataCommand(ChartDataQueryContextSchema().load(payload))
        command.validate()
        result = command.run()["queries"][0]
    except Exception as ex:
        if not is_superset_access_error(ex):
            logger.exception("Reading the latest %s of %s failed", column, dataset.table_name)
        return None
    rows = [] if result.get("error") else result.get("data", [])
    return rows[0].get("latest_moment") if rows else None


def coded_values(dataset: SqlaTable, entries: dict[str, ColumnDictionary]) -> dict[str, list[str]]:
    """The stored values of each code column, read through ChartData as the caller; free text is left out."""
    from superset.charts.schemas import ChartDataQueryContextSchema
    from superset.commands.chart.data.get_data_command import ChartDataCommand

    columns = coded_value_columns(dataset, entries)
    if not columns:
        return {}
    try:
        command = ChartDataCommand(ChartDataQueryContextSchema().load(build_values_payload(dataset, columns)))
        command.validate()
        results = command.run()["queries"]
    except Exception as ex:
        if not is_superset_access_error(ex):
            logger.exception("Reading the coded values of %s failed", dataset.table_name)
        return {}
    coded = {}
    for name, result in zip(columns, results):
        values = None if result.get("error") else coded_values_from_rows(result.get("data", []), name)
        if values:
            coded[name] = values
    return coded
