"""
Build the "Jobs Activity" dashboard in Superset.

Gives insight into how many / how often Hopsworks jobs are run, broken down by
project, with native filters to slice by project, job type and final status.

Data — joined live from the `hopsworks` MySQL metadata tables through the
`mysql_hopsworks` Superset JDBC connection (NO Trino), one row per *execution*:

    executions  e   one row per job run
      JOIN jobs j   ON j.id = e.job_id          (job name / type / project)
      JOIN project p ON p.id = j.project_id      (project name)

IMPORTANT: those tables were mounted as external feature groups with
platform-intelligence column inference, which *renames* columns. The SQL below
runs against raw MySQL, so it uses the PHYSICAL column names (e.g.
`executions.finalStatus`, `executions.user`, `project.projectname`,
`project.created`), NOT the renamed feature-group names.

The dataset has result caching disabled, so every open reflects current runs.
Re-run this program anytime to refresh charts/filters.

Run:  python create_jobs_dashboard.py
"""
import json

import hopsworks

SCHEMA = "hopsworks"
DATASET_NAME = "job_runs"
DASHBOARD_TITLE = "Jobs Activity Dashboard"
CHART_PREFIX = "Jobs · "                    # namespaces charts for idempotency

# One denormalized row per execution. Physical MySQL column names only.
DATASET_SQL = """SELECT
    e.id                                   AS execution_id,
    e.submission_time                      AS submission_time,
    DATE(e.submission_time)                AS submission_date,
    e.state                                AS state,
    e.finalStatus                          AS final_status,
    e.`user`                               AS run_by,
    j.id                                   AS job_id,
    j.name                                 AS job_name,
    j.type                                 AS job_type,
    p.id                                   AS project_id,
    p.projectname                          AS project_name,
    CASE WHEN e.execution_stop > e.execution_start AND e.execution_start > 0
         THEN ROUND((e.execution_stop - e.execution_start) / 1000.0, 1)
    END                                    AS duration_seconds
FROM hopsworks.executions e
JOIN hopsworks.jobs    j ON j.id = e.job_id
JOIN hopsworks.project p ON p.id = j.project_id"""

LAST_7D = [{"expressionType": "SQL",
            "sqlExpression": "submission_time >= NOW() - INTERVAL 7 DAY",
            "clause": "WHERE"}]


def sql_metric(expr, label, name):
    return {"expressionType": "SQL", "sqlExpression": expr,
            "label": label, "optionName": name, "hasCustomLabel": True}


COUNT_RUNS = sql_metric("COUNT(*)", "runs", "m_runs")
DISTINCT_JOBS = sql_metric("COUNT(DISTINCT job_id)", "distinct_jobs", "m_jobs")
DISTINCT_PROJ = sql_metric("COUNT(DISTINCT project_id)", "projects", "m_projects")
AVG_DURATION = sql_metric("ROUND(AVG(duration_seconds), 1)", "avg_seconds", "m_avgdur")


# --------------------------------------------------------------------------- #
# Superset helpers (same pattern as create_executive_dashboard.py)
# --------------------------------------------------------------------------- #
def find_mysql_db_id(api):
    for db in api.list_databases()["result"]:
        if (db.get("backend") or "").lower() == "mysql":
            return db["id"], db.get("database_name")
    raise RuntimeError("No mysql backend (mysql_hopsworks) connection in Superset")


def run_sql(api, db_id, sql):
    body = {"database_id": db_id, "sql": sql, "schema": SCHEMA,
            "runAsync": False, "select_as_cta": False, "json": True}
    r = api._request("POST", "/api/v1/sqllab/execute/", json_data=body)
    cols = [c["name"] for c in r.get("columns", [])]
    return [dict(zip(cols, [row.get(c) for c in cols])) for row in r.get("data", [])]


def ensure_dataset(api, db_id, name, sql):
    page, existing = 0, None
    while True:
        j = api._request("GET", f"/api/v1/dataset/?q=(page:{page},page_size:100)")
        batch = j.get("result", [])
        for ds in batch:
            if ds.get("table_name") == name and ds.get("schema") == SCHEMA:
                existing = ds
                break
        if existing or len(batch) < 100:
            break
        page += 1

    if existing:
        ds_id = existing["id"]
        api.update_dataset(ds_id, sql=sql)
        print(f"Updated existing dataset id={ds_id}")
    else:
        ds_id = api.create_dataset(
            database_id=db_id, table_name=name, schema=SCHEMA, sql=sql)["id"]
        print(f"Created dataset id={ds_id}")

    # Re-introspect columns after a SQL change and disable caching so the counts
    # stay live.
    api._request("PUT", f"/api/v1/dataset/{ds_id}/refresh")
    api.update_dataset(ds_id, cache_timeout=0)
    cols = api.get_dataset(ds_id).get("result", {}).get("columns", [])
    print(f"  synced {len(cols)} columns; cache disabled (cache_timeout=0)")
    return ds_id


def list_all(api, resource):
    items, page = [], 0
    while True:
        j = api._request("GET", f"/api/v1/{resource}/?q=(page:{page},page_size:100)")
        batch = j.get("result", [])
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


# --------------------------------------------------------------------------- #
# Chart specs
# --------------------------------------------------------------------------- #
def kpi(name, metric, subheader, filters=None):
    return (f"{CHART_PREFIX}{name}", "big_number_total",
            {"viz_type": "big_number_total", "metric": metric,
             "adhoc_filters": filters or [], "y_axis_format": "SMART_NUMBER",
             "subheader": subheader}, 3, 30)


def topn_bar(name, x_col, metric, limit, filters=None, horizontal=True):
    return (f"{CHART_PREFIX}{name}", "echarts_timeseries_bar",
            {"viz_type": "echarts_timeseries_bar", "x_axis": x_col,
             "x_axis_force_categorical": True, "metrics": [metric], "groupby": [],
             "adhoc_filters": filters or [], "row_limit": limit,
             "orientation": "horizontal" if horizontal else "vertical",
             "order_desc": True, "timeseries_limit_metric": metric,
             "x_axis_sort": metric["label"], "x_axis_sort_asc": False,
             "show_legend": False, "show_value": True,
             "y_axis_format": "SMART_NUMBER"},
            6, 50)


def timeseries_bar(name, x_col, metric):
    return (f"{CHART_PREFIX}{name}", "echarts_timeseries_bar",
            {"viz_type": "echarts_timeseries_bar", "x_axis": x_col,
             "x_axis_force_categorical": True, "metrics": [metric], "groupby": [],
             "adhoc_filters": [], "row_limit": 1000, "orientation": "vertical",
             "x_axis_sort": x_col, "x_axis_sort_asc": True,
             "show_legend": False, "show_value": True,
             "y_axis_format": "SMART_NUMBER"},
            12, 50)


def pie(name, col):
    return (f"{CHART_PREFIX}{name}", "pie",
            {"viz_type": "pie", "groupby": [col], "metric": COUNT_RUNS,
             "adhoc_filters": [], "row_limit": 100, "show_legend": True,
             "label_type": "key_value_percent"},
            6, 50)


def chart_specs():
    specs = [
        # KPI row.
        kpi("Total Job Runs", COUNT_RUNS, "executions, all time"),
        kpi("Job Runs (Last 7 Days)", COUNT_RUNS, "executions in the last 7 days", LAST_7D),
        kpi("Active Projects", DISTINCT_PROJ, "distinct projects running jobs"),
        kpi("Distinct Jobs Run", DISTINCT_JOBS, "distinct jobs executed"),

        # Headline: top 10 projects by runs in the last 7 days.
        topn_bar("Top 10 Projects by Job Runs (Last 7 Days)",
                 "project_name", COUNT_RUNS, 10, LAST_7D),
        # Runs by project, all time (breakdown beyond the 7-day window).
        topn_bar("Job Runs by Project (All Time)",
                 "project_name", COUNT_RUNS, 25),

        # How often jobs run, over time.
        timeseries_bar("Job Runs per Day", "submission_date", COUNT_RUNS),

        # Composition.
        pie("Runs by Final Status", "final_status"),
        pie("Runs by Job Type", "job_type"),

        # Top individual jobs + slowest projects.
        topn_bar("Top Jobs by Run Count", "job_name", COUNT_RUNS, 15),
        topn_bar("Avg Run Duration by Project (seconds)",
                 "project_name", AVG_DURATION, 25),

        # Detail table.
        (f"{CHART_PREFIX}Execution Detail", "table",
         {"viz_type": "table", "query_mode": "raw",
          "all_columns": ["submission_time", "project_name", "job_name",
                          "job_type", "run_by", "state", "final_status",
                          "duration_seconds"],
          "adhoc_filters": [], "row_limit": 1000,
          "order_by_cols": ["[\"submission_time\", false]"],
          "table_timestamp_format": "smart_date"},
         12, 60),
    ]
    return specs


def replace_chart(api, slice_name, viz_type, dataset_id, params):
    for c in list_all(api, "chart"):
        if c.get("slice_name") == slice_name:
            api.delete_chart(c["id"])
    return api.create_chart(
        slice_name=slice_name, viz_type=viz_type, datasource_id=dataset_id,
        params=json.dumps(params))["id"]


# --------------------------------------------------------------------------- #
# Native filters + dashboard layout
# --------------------------------------------------------------------------- #
def native_filter(fid, name, ds_id, column, multi=True):
    return {
        "id": f"NATIVE_FILTER-{fid}", "name": name, "filterType": "filter_select",
        "type": "NATIVE_FILTER",
        "targets": [{"datasetId": ds_id, "column": {"name": column}}],
        "controlValues": {"multiSelect": multi, "enableEmptyFilter": False,
                          "defaultToFirstItem": False, "inverseSelection": False,
                          "searchAllOptions": False},
        "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
        "defaultDataMask": {"filterState": {}, "extraFormData": {}},
        "cascadeParentIds": [],
    }


def build_json_metadata(ds_id):
    return json.dumps({
        "native_filter_configuration": [
            native_filter("project", "Project", ds_id, "project_name"),
            native_filter("jobtype", "Job type", ds_id, "job_type"),
            native_filter("finalstatus", "Final status", ds_id, "final_status"),
        ],
        "cross_filters_enabled": False,
    })


def build_position_json(charts, title):
    layout = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [],
                    "parents": ["ROOT_ID"]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
    }
    row_idx, col_used, row_id = 0, 0, None

    def new_row():
        nonlocal row_idx, col_used, row_id
        row_idx += 1
        col_used = 0
        row_id = f"ROW-{row_idx}"
        layout[row_id] = {"type": "ROW", "id": row_id, "children": [],
                          "parents": ["ROOT_ID", "GRID_ID"],
                          "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        layout["GRID_ID"]["children"].append(row_id)

    new_row()
    for ch in charts:
        w = min(ch["width"], 12)
        if col_used + w > 12:
            new_row()
        nid = f"CHART-{ch['id']}"
        layout[nid] = {"type": "CHART", "id": nid, "children": [],
                       "parents": ["ROOT_ID", "GRID_ID", row_id],
                       "meta": {"width": w, "height": ch["height"],
                                "chartId": ch["id"], "sliceName": ch["name"]}}
        layout[row_id]["children"].append(nid)
        col_used += w
    return json.dumps(layout)


def ensure_dashboard(api, title, charts, json_metadata):
    position_json = build_position_json(charts, title)
    dash_id = next((d["id"] for d in list_all(api, "dashboard")
                    if d.get("dashboard_title") == title), None)
    if dash_id is None:
        dash_id = api.create_dashboard(
            dashboard_title=title, published=True,
            position_json=position_json, json_metadata=json_metadata)["id"]
        print(f"Created dashboard id={dash_id}")
    else:
        api.update_dashboard(dash_id, dashboard_title=title, published=True,
                             position_json=position_json, json_metadata=json_metadata)
        print(f"Updated dashboard id={dash_id}")
    for ch in charts:
        api.update_chart(ch["id"], dashboards=[dash_id])
    return dash_id


def main():
    project = hopsworks.login()
    api = project.get_superset_api()

    db_id, db_name = find_mysql_db_id(api)
    print(f"mysql_hopsworks connection: id={db_id} ({db_name})\n")

    preview = run_sql(api, db_id, DATASET_SQL + " ORDER BY submission_time DESC LIMIT 5")
    print(f"Preview ({len(preview)} of recent runs):")
    for row in preview:
        print("  ", json.dumps(row, default=str))

    ds_id = ensure_dataset(api, db_id, DATASET_NAME, DATASET_SQL)
    host = api._get_superset_url() if hasattr(api, "_get_superset_url") else ""
    print(f"\nDataset '{DATASET_NAME}' ready (id={ds_id}).")

    print("\nCreating charts:")
    charts = []
    for slice_name, viz_type, params, width, height in chart_specs():
        cid = replace_chart(api, slice_name, viz_type, ds_id, params)
        charts.append({"id": cid, "name": slice_name, "width": width, "height": height})
        print(f"  [{viz_type}] {slice_name} -> id={cid}")

    dash_id = ensure_dashboard(api, DASHBOARD_TITLE, charts, build_json_metadata(ds_id))
    print(f"\nDashboard '{DASHBOARD_TITLE}' ready (id={dash_id}).")
    print(f"Open it: {host}/hopsworks-api/superset/superset/dashboard/{dash_id}/")


if __name__ == "__main__":
    main()
