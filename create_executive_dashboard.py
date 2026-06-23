"""
Build the executive OKR dashboard in Superset.

Pairs each OKR *target* (from the `okrs` feature group) against its live
*actual*, computed from the real Hopsworks metadata tables in the `hopsworks`
MySQL database — reached through the `mysql_hopsworks` Superset JDBC connection.
NO Trino: every actual is a COUNT over a live MySQL table, so the dashboard
reflects current state each time it is opened.

Targets live in the `okrs` feature group (offline feature store, not MySQL), so
they are read once at build time and embedded as constants in the dataset SQL.
Re-run this program after changing your OKR targets to refresh them.

Actuals — one COUNT per OKR, over the live `hopsworks` MySQL tables:
    features          cached_feature + on_demand_feature + embedding_feature
    models            model
    model deployments serving
    agent deployments agent
    dashboards        dashboard
    apps              jobs WHERE type = 'PYTHON_APP'

This program:
  1. reads the OKR targets from the `okrs` feature group,
  2. builds a virtual (SQL) dataset on the mysql_hopsworks connection that
     UNIONs one row per OKR: metric, target, live actual, pct-to-target, status,
  3. creates/updates gauge + bar + table charts and assembles the dashboard.

Run:  python create_executive_dashboard.py
"""
import json
import sys

import hopsworks

SCHEMA = "hopsworks"                       # MySQL schema holding the metadata tables
OKRS_FG = "okrs"
OKRS_FG_VERSION = 1
DATASET_NAME = "okr_progress"
DASHBOARD_TITLE = "Executive OKR Dashboard"
CHART_PREFIX = "OKR · "                     # namespaces charts for idempotency

# Live MySQL actual for each OKR metric: a scalar SQL expression counting the
# real Hopsworks metadata rows. Keyed by the metric name stored in the `okrs`
# FG. `apps` has no table in the metadata DB, so its actual is the literal 0.
ACTUAL_SQL = {
    "features": ("(SELECT COUNT(*) FROM hopsworks.cached_feature)"
                 " + (SELECT COUNT(*) FROM hopsworks.on_demand_feature)"
                 " + (SELECT COUNT(*) FROM hopsworks.embedding_feature)"),
    "models": "(SELECT COUNT(*) FROM hopsworks.model)",
    "model deployments": "(SELECT COUNT(*) FROM hopsworks.serving)",
    "agent deployments": "(SELECT COUNT(*) FROM hopsworks.agent)",
    "dashboards": "(SELECT COUNT(*) FROM hopsworks.dashboard)",
    "apps": "(SELECT COUNT(*) FROM hopsworks.jobs WHERE type = 'PYTHON_APP')",
}

MAX_PCT = {
    "expressionType": "SQL", "sqlExpression": "MAX(pct_to_target)",
    "label": "pct_to_target", "optionName": "metric_pct", "hasCustomLabel": True,
}
MAX_TARGET = {
    "expressionType": "SQL", "sqlExpression": "MAX(target)",
    "label": "target", "optionName": "metric_target", "hasCustomLabel": True,
}
MAX_ACTUAL = {
    "expressionType": "SQL", "sqlExpression": "MAX(actual)",
    "label": "actual", "optionName": "metric_actual", "hasCustomLabel": True,
}


def find_mysql_db_id(api):
    """The mysql_hopsworks connection: the only mysql-backend DB in Superset."""
    for db in api.list_databases()["result"]:
        if (db.get("backend") or "").lower() == "mysql":
            return db["id"], db.get("database_name")
    raise RuntimeError("No mysql backend (mysql_hopsworks) connection in Superset")


def run_sql(api, db_id, sql):
    body = {
        "database_id": db_id, "sql": sql, "schema": SCHEMA,
        "runAsync": False, "select_as_cta": False, "json": True,
    }
    r = api._request("POST", "/api/v1/sqllab/execute/", json_data=body)
    cols = [c["name"] for c in r.get("columns", [])]
    return [dict(zip(cols, [row.get(c) for c in cols])) for row in r.get("data", [])]


def load_targets(project):
    """Read OKR targets from the `okrs` feature group -> {metric: target}."""
    fs = project.get_feature_store()
    df = fs.get_feature_group(OKRS_FG, version=OKRS_FG_VERSION).read()
    targets = {str(r["target"]): int(r["value"]) for _, r in df.iterrows()}
    if not targets:
        sys.exit(f"No targets found in the '{OKRS_FG}' feature group")
    return targets


def build_sql(targets):
    """UNION one row per OKR: metric, embedded target, live MySQL actual."""
    rows = []
    for metric, target in targets.items():
        actual = ACTUAL_SQL.get(metric, "0")
        esc = metric.replace("'", "''")
        rows.append(f"    SELECT '{esc}' AS metric, {int(target)} AS target, "
                    f"({actual}) AS actual")
    union = "\n    UNION ALL\n".join(rows)
    return f"""SELECT
    metric,
    target,
    actual,
    ROUND(100.0 * actual / NULLIF(target, 0), 1)            AS pct_to_target,
    CASE WHEN actual >= target THEN 'On track' ELSE 'Behind' END AS status
FROM (
{union}
) t"""


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

    # Re-introspect columns (Superset does not do this on a virtual dataset's
    # SQL change) and disable result caching so the live counts stay fresh.
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


def metric_filter(metric):
    esc = metric.replace("'", "''")
    return [{"expressionType": "SQL",
             "sqlExpression": f"metric = '{esc}'", "clause": "WHERE"}]


def chart_specs(targets):
    """(slice_name, viz_type, params, width, height) list for the dashboard."""
    specs = []

    # 1. Overall progress KPI: average pct-to-target across all OKRs.
    specs.append((
        f"{CHART_PREFIX}Overall Progress", "big_number_total",
        {"viz_type": "big_number_total",
         "metric": {"expressionType": "SQL",
                    "sqlExpression": "ROUND(AVG(pct_to_target), 1)",
                    "label": "avg_pct", "optionName": "metric_avg_pct",
                    "hasCustomLabel": True},
         "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "average % of target across all OKRs"},
        4, 30,
    ))

    # 2. OKRs on track KPI.
    specs.append((
        f"{CHART_PREFIX}OKRs On Track", "big_number_total",
        {"viz_type": "big_number_total",
         "metric": {"expressionType": "SQL",
                    "sqlExpression": "SUM(CASE WHEN actual >= target THEN 1 ELSE 0 END)",
                    "label": "on_track", "optionName": "metric_on_track",
                    "hasCustomLabel": True},
         "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": f"of {len(targets)} OKRs meeting target"},
        4, 30,
    ))

    # 3. Target vs Actual grouped bar across all OKRs.
    specs.append((
        f"{CHART_PREFIX}Target vs Actual", "echarts_timeseries_bar",
        {"viz_type": "echarts_timeseries_bar", "x_axis": "metric",
         "x_axis_force_categorical": True, "metrics": [MAX_TARGET, MAX_ACTUAL],
         "groupby": [], "adhoc_filters": [], "row_limit": 100,
         "orientation": "vertical", "show_legend": True,
         "logAxis": True, "truncateYAxis": False,
         "y_axis_format": "SMART_NUMBER"},
        12, 50,
    ))

    # 4. One progress-to-target gauge per OKR.
    for metric in targets:
        specs.append((
            f"{CHART_PREFIX}{metric} — % to target", "gauge_chart",
            {"viz_type": "gauge_chart", "metric": MAX_PCT,
             "groupby": [], "adhoc_filters": metric_filter(metric),
             "row_limit": 10, "min_val": 0, "max_val": 100,
             "start_angle": 225, "end_angle": -45, "show_pointer": True,
             "show_axis_tick": True, "show_split_line": True,
             "value_formatter": "{value}%"},
            4, 40,
        ))

    # 5. Detail table.
    specs.append((
        f"{CHART_PREFIX}OKR Detail", "table",
        {"viz_type": "table", "query_mode": "raw",
         "all_columns": ["metric", "target", "actual", "pct_to_target", "status"],
         "adhoc_filters": [], "row_limit": 100,
         "order_by_cols": [], "show_cell_bars": True,
         "table_timestamp_format": "smart_date"},
        12, 50,
    ))
    return specs


def replace_chart(api, slice_name, viz_type, dataset_id, params):
    for c in list_all(api, "chart"):
        if c.get("slice_name") == slice_name:
            api.delete_chart(c["id"])
    return api.create_chart(
        slice_name=slice_name, viz_type=viz_type, datasource_id=dataset_id,
        params=json.dumps(params))["id"]


def build_position_json(charts, title):
    """charts: list of {id, name, width, height}. Greedily pack rows to 12 cols."""
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


def ensure_dashboard(api, title, charts):
    position_json = build_position_json(charts, title)
    dash_id = next((d["id"] for d in list_all(api, "dashboard")
                    if d.get("dashboard_title") == title), None)
    if dash_id is None:
        dash_id = api.create_dashboard(
            dashboard_title=title, published=True,
            position_json=position_json)["id"]
        print(f"Created dashboard id={dash_id}")
    else:
        api.update_dashboard(dash_id, dashboard_title=title, published=True,
                             position_json=position_json)
        print(f"Updated dashboard id={dash_id}")
    for ch in charts:
        api.update_chart(ch["id"], dashboards=[dash_id])
    return dash_id


def main():
    project = hopsworks.login()
    api = project.get_superset_api()

    db_id, db_name = find_mysql_db_id(api)
    print(f"mysql_hopsworks connection: id={db_id} ({db_name})\n")

    targets = load_targets(project)
    print("OKR targets (from the okrs feature group):")
    for metric, target in targets.items():
        print(f"  {metric}: {target}")

    sql = build_sql(targets)
    print("\nGenerated OKR-progress SQL:\n")
    print(sql)

    preview = run_sql(api, db_id, sql)
    print("\nLive OKR progress:")
    for row in preview:
        print("  ", json.dumps(row, default=str))

    ds_id = ensure_dataset(api, db_id, DATASET_NAME, sql)
    host = api._get_superset_url() if hasattr(api, "_get_superset_url") else ""
    print(f"\nDataset '{DATASET_NAME}' ready (id={ds_id}).")

    print("\nCreating charts:")
    charts = []
    for slice_name, viz_type, params, width, height in chart_specs(targets):
        cid = replace_chart(api, slice_name, viz_type, ds_id, params)
        charts.append({"id": cid, "name": slice_name,
                       "width": width, "height": height})
        print(f"  [{viz_type}] {slice_name} -> id={cid}")

    dash_id = ensure_dashboard(api, DASHBOARD_TITLE, charts)
    print(f"\nDashboard '{DASHBOARD_TITLE}' ready (id={dash_id}).")
    print(f"Open it: {host}/hopsworks-api/superset/superset/dashboard/{dash_id}/")


if __name__ == "__main__":
    main()
