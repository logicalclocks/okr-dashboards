"""Build a Superset dashboard over the Hopsworks metadata DB (via the
`mysql_hopsworks` SQL data source), showing #feature groups, #feature views, and
jobs submitted over time for this project."""
import json
import hopsworks

DATASOURCE = "mysql_hopsworks"  # Hopsworks SQL data source -> Superset DB connection
SCHEMA = "hopsworks"
PROJECT_ID = 119
FEATURESTORE_ID = 67

COUNT_METRIC = {
    "expressionType": "SQL", "sqlExpression": "COUNT(*)",
    "label": "count", "optionName": "metric_count", "hasCustomLabel": True,
}

DATASETS = {
    "metadata_feature_groups": (
        "SELECT id, name, version, created "
        f"FROM feature_group WHERE feature_store_id = {FEATURESTORE_ID}"
    ),
    "metadata_feature_views": (
        "SELECT id, name, version, created "
        f"FROM feature_view WHERE feature_store_id = {FEATURESTORE_ID}"
    ),
    "metadata_job_executions": (
        "SELECT e.id, e.submission_time, e.state, j.name AS job_name "
        "FROM executions e JOIN jobs j ON e.job_id = j.id "
        f"WHERE j.project_id = {PROJECT_ID}"
    ),
}


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


def resolve_db_id(api, datasource_name):
    """Resolve the Superset (mysql-backed) database-connection id to use.

    Superset reaches the Hopsworks metadata DB through the mysql backend, so we
    prefer a connection provisioned for the `mysql_hopsworks` data source
    (`<datasource>__<user>_superset`) and otherwise fall back to whatever
    mysql-backend connection exists for that same metadata DB."""
    dbs = api.list_databases()["result"]
    # 1) A connection provisioned specifically for the data source, if present.
    for db in dbs:
        if db.get("database_name", "").startswith(datasource_name + "__"):
            return db["id"]
    # 2) Fall back to the mysql-backend connection to the metadata DB.
    for db in dbs:
        if db.get("backend") == "mysql":
            print(f"note: '{datasource_name}' not provisioned in Superset; using "
                  f"mysql-backend connection '{db['database_name']}' (id {db['id']})")
            return db["id"]
    raise RuntimeError("No mysql-backed Superset database connection found.")


def ensure_dataset(api, db_id, name, sql):
    for ds in list_all(api, "dataset"):
        if ds.get("table_name") == name and ds.get("schema") == SCHEMA:
            return ds["id"]
    return api.create_dataset(
        database_id=db_id, table_name=name, schema=SCHEMA, sql=sql,
    )["id"]


def replace_chart(api, slice_name, viz_type, datasource_id, params):
    for c in list_all(api, "chart"):
        if c.get("slice_name") == slice_name:
            api.delete_chart(c["id"])
    return api.create_chart(
        slice_name=slice_name, viz_type=viz_type,
        datasource_id=datasource_id, params=params,
    )["id"]


def build_position_json(chart_ids, chart_slices, title):
    layout = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [],
                    "parents": ["ROOT_ID"]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": title}},
    }

    def chart(key, width, height):
        nid = f"CHART-{key}"
        layout[nid] = {"type": "CHART", "id": nid, "children": [],
                       "parents": ["ROOT_ID", "GRID_ID"],
                       "meta": {"width": width, "height": height,
                                "chartId": chart_ids[key],
                                "sliceName": chart_slices[key]}}
        return nid

    def row(row_id, children):
        layout[row_id] = {"type": "ROW", "id": row_id, "children": children,
                          "parents": ["ROOT_ID", "GRID_ID"],
                          "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for c in children:
            layout[c]["parents"] = ["ROOT_ID", "GRID_ID", row_id]
        layout["GRID_ID"]["children"].append(row_id)

    row("ROW-1", [chart("fg", 6, 50), chart("fv", 6, 50)])
    row("ROW-2", [chart("jobs", 12, 60)])
    return json.dumps(layout)


def ensure_dashboard(api, title, chart_ids, chart_slices):
    position_json = build_position_json(chart_ids, chart_slices, title)
    dash_id = next((d["id"] for d in list_all(api, "dashboard")
                    if d.get("dashboard_title") == title), None)
    if dash_id is None:
        dash_id = api.create_dashboard(dashboard_title=title, published=True,
                                       position_json=position_json)["id"]
    else:
        api.update_dashboard(dash_id, dashboard_title=title, published=True,
                             position_json=position_json)
    for cid in chart_ids.values():
        api.update_chart(cid, dashboards=[dash_id])
    return dash_id


def main():
    project = hopsworks.login()
    api = project.get_superset_api()

    db_id = resolve_db_id(api, DATASOURCE)
    print(f"using Superset database id {db_id} (data source {DATASOURCE})")

    ds_ids = {}
    for name, sql in DATASETS.items():
        ds_ids[name] = ensure_dataset(api, db_id, name, sql)
        print(f"dataset {name} -> id {ds_ids[name]}")

    chart_ids = {
        "fg": replace_chart(
            api, "Feature Groups", "big_number_total",
            ds_ids["metadata_feature_groups"],
            json.dumps({"viz_type": "big_number_total", "metric": COUNT_METRIC,
                        "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
                        "subheader": "Total feature groups"})),
        "fv": replace_chart(
            api, "Feature Views", "big_number_total",
            ds_ids["metadata_feature_views"],
            json.dumps({"viz_type": "big_number_total", "metric": COUNT_METRIC,
                        "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
                        "subheader": "Total feature views"})),
        "jobs": replace_chart(
            api, "Jobs Submitted Over Time", "echarts_timeseries_bar",
            ds_ids["metadata_job_executions"],
            json.dumps({"viz_type": "echarts_timeseries_bar",
                        "x_axis": "submission_time", "time_grain_sqla": "P1D",
                        "metrics": [COUNT_METRIC], "groupby": [],
                        "adhoc_filters": [], "row_limit": 10000,
                        "orientation": "vertical", "show_legend": False,
                        "y_axis_format": "SMART_NUMBER"})),
    }
    for k, v in chart_ids.items():
        print(f"chart {k} -> id {v}")

    chart_slices = {"fg": "Feature Groups", "fv": "Feature Views",
                    "jobs": "Jobs Submitted Over Time"}
    dash_id = ensure_dashboard(api, "Hopsworks Metadata Overview",
                               chart_ids, chart_slices)
    host = project._client._host if hasattr(project, "_client") else "<host>"
    print("DASHBOARD_ID", dash_id)


if __name__ == "__main__":
    main()
