"""Superset primitives shared by the dashboard builders.

Every builder in this repo does the same four things: find the analytics database,
register a virtual dataset over some SQL, (re)create a set of charts, and lay them
out on a dashboard. That code used to live in ``create_tag_dataset``, so a builder
that wanted a helper had to import a *script* — which meant its constants, its
argument parsing and its notion of what "the" dataset is came along uninvited.

This module is the library half, with no dashboard of its own. Builders import
from here; ``create_tag_dataset`` re-exports the names it used to own so anything
still importing from it keeps working.

Two Superset behaviours are worth knowing, because both are silent when wrong:

- A virtual dataset's column list is persisted at creation and is **not**
  re-introspected when its SQL changes. New columns simply never appear. Every
  dataset write here forces a refresh.
- Result caching will happily serve a chart built before today's tag values
  existed, so caching is disabled per dataset rather than relied on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

SCHEMA = "hopsworks"
ANALYTICS_CONNECTION = "hopsworks_analytics"

GRID_COLUMNS = 12
PAGE_SIZE = 100


@dataclass(frozen=True)
class ChartSpec:
    """One chart, and how much of the 12-column grid it wants.

    ``params`` is Superset's chart configuration, passed through as-is: it is a
    large, viz-type-specific and undocumented blob, and pretending otherwise by
    wrapping each field would age badly.
    """

    name: str
    viz_type: str
    params: dict[str, Any]
    width: int = 6
    height: int = 50


@dataclass(frozen=True)
class Chart:
    """A chart that exists in Superset, with the layout it asked for."""

    id: int
    spec: ChartSpec

    @property
    def width(self) -> int:
        return min(self.spec.width, GRID_COLUMNS)


class Superset:
    """Thin wrapper over the project's Superset API.

    Holds the one piece of state every call needs — which database the analytics
    connection is — so builders stop threading ``db_id`` through every function.
    """

    def __init__(self, api: Any, database_id: int, database_name: str) -> None:
        self.api = api
        self.database_id = database_id
        self.database_name = database_name

    @classmethod
    def connect(cls, api: Any) -> Superset:
        """Resolve the analytics connection, which the backend names
        ``<connector>__<superset user>``.

        Matching on the mysql backend alone is not enough: a project with an
        online feature store has a MySQL connection too, and picking the first
        match silently points every chart at the wrong database.
        """
        mysql = [
            db
            for db in api.list_databases()["result"]
            if (db.get("backend") or "").lower() == "mysql"
        ]
        for db in mysql:
            name = db.get("database_name") or ""
            if name.startswith(ANALYTICS_CONNECTION):
                return cls(api, db["id"], name)
        raise RuntimeError(
            f"No Superset connection named {ANALYTICS_CONNECTION}* found. "
            f"MySQL connections present: {[db.get('database_name') for db in mysql]}"
        )

    # -- SQL ---------------------------------------------------------------- #

    def sql(self, statement: str) -> list[dict[str, Any]]:
        """Run a statement through SQL Lab and return rows as dicts."""
        response = self.api._request(
            "POST",
            "/api/v1/sqllab/execute/",
            json_data={
                "database_id": self.database_id,
                "sql": statement,
                "schema": SCHEMA,
                "runAsync": False,
                "select_as_cta": False,
                "json": True,
            },
        )
        columns = [c["name"] for c in response.get("columns", [])]
        return [
            {c: row.get(c) for c in columns} for row in response.get("data", [])
        ]

    def scalar(self, statement: str) -> Any:
        """The first column of the first row, or None."""
        rows = self.sql(statement)
        return next(iter(rows[0].values())) if rows else None

    def preview(self, statement: str, limit: int = 5) -> list[dict[str, Any]]:
        """Run a statement as a subquery, to prove it works before registering it."""
        return self.sql(f"SELECT * FROM (\n{statement}\n) _preview LIMIT {limit}")

    # -- paging ------------------------------------------------------------- #

    def _each(self, resource: str) -> Iterator[dict[str, Any]]:
        page = 0
        while True:
            batch = self.api._request(
                "GET", f"/api/v1/{resource}/?q=(page:{page},page_size:{PAGE_SIZE})"
            ).get("result", [])
            yield from batch
            if len(batch) < PAGE_SIZE:
                return
            page += 1

    # -- datasets ----------------------------------------------------------- #

    def ensure_dataset(self, name: str, statement: str) -> int:
        """Register or update a virtual dataset, and make sure it reflects the SQL.

        Creating fails outright when ``(schema, table_name)`` already exists, so
        this looks first rather than catching.
        """
        existing = next(
            (
                ds
                for ds in self._each("dataset")
                if ds.get("table_name") == name and ds.get("schema") == SCHEMA
            ),
            None,
        )
        if existing:
            dataset_id = existing["id"]
            self.api.update_dataset(dataset_id, sql=statement)
            print(f"Updated existing dataset id={dataset_id}")
        else:
            dataset_id = self.api.create_dataset(
                database_id=self.database_id,
                table_name=name,
                schema=SCHEMA,
                sql=statement,
            )["id"]
            print(f"Created dataset id={dataset_id}")

        self.api._request("PUT", f"/api/v1/dataset/{dataset_id}/refresh")
        self.api.update_dataset(dataset_id, cache_timeout=0)
        columns = self.api.get_dataset(dataset_id).get("result", {}).get("columns", [])
        print(f"  synced {len(columns)} columns; cache disabled (cache_timeout=0)")
        return dataset_id

    # -- charts and dashboards ---------------------------------------------- #

    def replace_chart(self, spec: ChartSpec, dataset_id: int) -> Chart:
        """Recreate a chart by name, so re-running is idempotent."""
        for chart in list(self._each("chart")):
            if chart.get("slice_name") == spec.name:
                self.api.delete_chart(chart["id"])
        chart_id = self.api.create_chart(
            slice_name=spec.name,
            viz_type=spec.viz_type,
            datasource_id=dataset_id,
            params=json.dumps(spec.params),
        )["id"]
        return Chart(id=chart_id, spec=spec)

    def ensure_dashboard(self, title: str, charts: Sequence[Chart]) -> int:
        position = layout_json(charts, title)
        dashboard_id = next(
            (
                d["id"]
                for d in self._each("dashboard")
                if d.get("dashboard_title") == title
            ),
            None,
        )
        if dashboard_id is None:
            dashboard_id = self.api.create_dashboard(
                dashboard_title=title, published=True, position_json=position
            )["id"]
            print(f"Created dashboard id={dashboard_id}")
        else:
            self.api.update_dashboard(
                dashboard_id,
                dashboard_title=title,
                published=True,
                position_json=position,
            )
            print(f"Updated dashboard id={dashboard_id}")
        for chart in charts:
            # Persist the chart -> dashboard link; the layout alone does not.
            self.api.update_chart(chart.id, dashboards=[dashboard_id])
        return dashboard_id

    def build(
        self,
        *,
        dataset: str,
        title: str,
        statement: str,
        specs: Sequence[ChartSpec],
        host: str | None = None,
    ) -> tuple[int, int]:
        """Preview the SQL, register the dataset, recreate the charts, lay them out."""
        print(f"\nGenerated SQL for '{dataset}':\n")
        print(statement)

        rows = self.preview(statement)
        print(f"\nPreview returned {len(rows)} row(s). Sample:")
        for row in rows:
            print("  ", json.dumps(row, default=str))

        dataset_id = self.ensure_dataset(dataset, statement)
        print(f"Dataset '{dataset}' ready (id={dataset_id}).")

        print("Creating charts:")
        charts = []
        for spec in specs:
            chart = self.replace_chart(spec, dataset_id)
            charts.append(chart)
            print(f"  [{spec.viz_type}] {spec.name} -> id={chart.id}")

        dashboard_id = self.ensure_dashboard(title, charts)
        print(f"Dashboard '{title}' ready (id={dashboard_id}).")
        if host:
            print(
                f"Open it: {host}/hopsworks-api/superset/superset/dashboard/{dashboard_id}/"
            )
        return dataset_id, dashboard_id


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #
def layout_json(charts: Sequence[Chart], title: str) -> str:
    """Greedily pack charts into rows of 12 columns, in Superset's v2 layout shape."""
    layout: dict[str, Any] = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {
            "type": "GRID",
            "id": "GRID_ID",
            "children": [],
            "parents": ["ROOT_ID"],
        },
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
    }
    rows = 0
    used = 0
    row_id = ""

    def start_row() -> None:
        nonlocal rows, used, row_id
        rows += 1
        used = 0
        row_id = f"ROW-{rows}"
        layout[row_id] = {
            "type": "ROW",
            "id": row_id,
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"background": "BACKGROUND_TRANSPARENT"},
        }
        layout["GRID_ID"]["children"].append(row_id)

    start_row()
    for chart in charts:
        if used + chart.width > GRID_COLUMNS:
            start_row()
        node = f"CHART-{chart.id}"
        layout[node] = {
            "type": "CHART",
            "id": node,
            "children": [],
            "parents": ["ROOT_ID", "GRID_ID", row_id],
            "meta": {
                "width": chart.width,
                "height": chart.spec.height,
                "chartId": chart.id,
                "sliceName": chart.spec.name,
            },
        }
        layout[row_id]["children"].append(node)
        used += chart.width
    return json.dumps(layout)


# --------------------------------------------------------------------------- #
# SQL helpers
# --------------------------------------------------------------------------- #
def sanitize(name: str) -> str:
    """A name safe to use as a SQL column alias."""
    return re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower()).strip("_") or "field"


def sql_str(value: str) -> str:
    """A safe single-quoted SQL string literal."""
    escaped = value.replace("'", "''")
    return f"'{escaped}'"


def simple_filter(column: str, operator: str, comparator: Any) -> dict[str, Any]:
    return {
        "expressionType": "SIMPLE",
        "subject": column,
        "operator": operator,
        "comparator": comparator,
        "clause": "WHERE",
    }


def count_metric(column: str, label: str = "count") -> dict[str, Any]:
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": column},
        "aggregate": "COUNT",
        "label": label,
    }


def categorical_bar(
    *,
    x_axis: str,
    series: str | None,
    metrics: list[dict[str, Any]],
    adhoc_filters: list[dict[str, Any]] | None = None,
    row_limit: int = 100,
    y_axis_format: str | None = None,
) -> dict[str, Any]:
    """Params for a bar chart whose x axis is a category, not a time column.

    Uses `echarts_timeseries_bar`. The obvious choice, `dist_bar`, is the legacy Bar Chart, and
    the legacy viz plugins were removed in Superset 4. Nothing rejects it on the way in: the
    chart saves happily, the dashboard builds, and the panel then fails at render time with
    `Item with key "dist_bar" is not registered`. So it looks like a working build until someone
    opens it.

    The echarts chart takes the category on `x_axis` and the series breakdown on `groupby`, which
    is the opposite way round from the legacy one, where `groupby` was the axis and `columns` the
    breakdown. `time_grain_sqla` is deliberately unset: the axis is a string.
    """
    params: dict[str, Any] = {
        "viz_type": "echarts_timeseries_bar",
        "x_axis": x_axis,
        "groupby": [series] if series else [],
        "metrics": metrics,
        "adhoc_filters": adhoc_filters or [],
        "row_limit": row_limit,
        "orientation": "vertical",
        "x_axis_sort_asc": True,
        "show_legend": True,
    }
    if y_axis_format:
        params["y_axis_format"] = y_axis_format
    return params


def sql_metric(expression: str, label: str) -> dict[str, Any]:
    return {"expressionType": "SQL", "sqlExpression": expression, "label": label}


@dataclass(frozen=True)
class TagSchema:
    """A schematised tag and the fields its values carry."""

    id: int
    name: str
    fields: list[tuple[str, str]] = field(default_factory=list)


def load_tags(superset: Superset) -> list[TagSchema]:
    """Read the tag schemas defined on the cluster."""
    tags = []
    for row in superset.sql(
        "SELECT id, name, tag_schema FROM feature_store_tag ORDER BY id"
    ):
        try:
            schema = json.loads(row["tag_schema"])
        except (TypeError, ValueError) as e:
            print(f"  ! skipping tag {row.get('name')!r}: bad schema JSON ({e})")
            continue
        properties = schema.get("properties") or {}
        fields = [
            (name, (spec or {}).get("type", "string"))
            for name, spec in properties.items()
        ]
        tags.append(TagSchema(id=int(row["id"]), name=row["name"], fields=fields))
        print(f"  tag #{row['id']} {row['name']!r} -> [{', '.join(n for n, _ in fields)}]")
    return tags
