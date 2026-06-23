#!/usr/bin/env python
"""Build a Superset dashboard on the feature-usage MySQL tables.

The dashboard charts feature reuse across feature views and models. It reads the
JDBC connection details from the `mysql_hopsworks` data source and registers a
DIRECT MySQL connection in Superset (NOT the feature groups / Trino) — the
charts read the underlying `hopsworks.*` MySQL tables, which also happen to be
mounted as feature groups in this project.

Tables used (all in the `hopsworks` MySQL database):
  * feature_group_feature_usage              - per-feature summary with counts
  * feature_group_feature_usage_feature_view - one row per (feature, feature view)
  * feature_group_feature_usage_model        - one row per (feature, model version)

Idempotent: re-running reuses the DB connection / datasets / dashboard and
replaces the charts in place.

Usage: python feature_usage_dashboard.py
"""
from __future__ import annotations

import json
import warnings
from urllib.parse import quote_plus

import hopsworks

warnings.filterwarnings("ignore")

DB_NAME = "mysql_hopsworks"
SCHEMA = "hopsworks"
DASHBOARD_TITLE = "Feature Usage & Reuse (MySQL)"

T_SUMMARY = "feature_group_feature_usage"
T_FV = "feature_group_feature_usage_feature_view"
T_MODEL = "feature_group_feature_usage_model"

# Virtual (SQL-backed) dataset: one row per feature group that carries the
# `sdlc` schematized tag, exposing the tag's `status` value and the number of
# features in that feature group. Feature counts come from `cached_feature`
# (cached + stream feature groups) and `on_demand_feature` (on-demand FGs).
# The `sdlc` tag is located via feature_store_tag.name -> schema_id, and its
# JSON value's `status` element is extracted with JSON_EXTRACT.
SDLC_DATASET = "sdlc_tagged_feature_counts"
SDLC_DATASET_SQL = """
SELECT fg.id AS feature_group_id,
       fg.name AS feature_group_name,
       JSON_UNQUOTE(JSON_EXTRACT(tv.value, '$.status')) AS sdlc_status,
       (SELECT COUNT(*) FROM hopsworks.cached_feature cf
          WHERE cf.cached_feature_group_id = fg.cached_feature_group_id
             OR cf.stream_feature_group_id = fg.stream_feature_group_id)
     + (SELECT COUNT(*) FROM hopsworks.on_demand_feature odf
          WHERE odf.on_demand_feature_group_id = fg.on_demand_feature_group_id)
         AS n_features
FROM hopsworks.feature_store_tag_value tv
JOIN hopsworks.feature_store_tag t ON t.id = tv.schema_id AND t.name = 'sdlc'
JOIN hopsworks.feature_group fg ON fg.id = tv.feature_group_id
WHERE tv.feature_group_id IS NOT NULL
""".strip()


# --------------------------------------------------------------------------- #
# Metrics (adhoc SQL) and pagination helpers
# --------------------------------------------------------------------------- #
def sql_metric(expr: str, label: str) -> dict:
    return {
        "expressionType": "SQL",
        "sqlExpression": expr,
        "label": label,
        "optionName": f"metric_{label}",
        "hasCustomLabel": True,
    }


COUNT = sql_metric("COUNT(*)", "count")
DISTINCT_FEATURES = sql_metric("COUNT(DISTINCT feature_name)", "distinct_features")
DISTINCT_MODELS = sql_metric("COUNT(DISTINCT model_version_id)", "distinct_models")
SUM_FEATURES = sql_metric("SUM(n_features)", "total_features")


def list_all(api, resource: str) -> list[dict]:
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
# Database connection (direct MySQL, from the mysql_hopsworks connector)
# --------------------------------------------------------------------------- #
def _db_sees_schema(api, db_id: int, schema: str) -> bool:
    try:
        schemas = api._request("GET", f"/api/v1/database/{db_id}/schemas/").get("result", [])
        return schema in schemas
    except Exception:
        return False


def ensure_database(api, sc) -> int:
    """Return a Superset MySQL database id that can read the `hopsworks` schema.

    Prefers an existing MySQL connection (Hopsworks pre-provisions one, and
    creating new DB connections is forbidden for project users -> HTTP 403).
    Falls back to creating one from the connector details on clusters that allow
    it. Either way the dashboard reads the underlying MySQL tables, not the FGs.
    """
    mysql_dbs = [db for db in list_all(api, "database")
                 if (db.get("backend") or "").lower() == "mysql"]
    # Reuse a MySQL connection that can actually see the hopsworks schema.
    for db in mysql_dbs:
        if _db_sees_schema(api, db["id"], SCHEMA):
            print(f"  reusing existing MySQL connection '{db.get('database_name')}' "
                  f"(id={db['id']}) — it already reaches the `{SCHEMA}` database")
            return db["id"]

    print(f"  no reusable MySQL connection sees `{SCHEMA}`; creating one from "
          f"{sc.user}@{sc.host}:{sc.port}/{sc.database}")
    user = quote_plus(str(sc.user))
    pw = quote_plus(str(sc.password))
    base = f"{user}:{pw}@{sc.host}:{sc.port}/{sc.database}"
    # Different Superset images ship different MySQL drivers; pick one that the
    # server can actually import/connect with via the test_connection endpoint.
    candidates = [f"mysql+pymysql://{base}", f"mysql://{base}",
                  f"mysql+mysqldb://{base}", f"mysql+mysqlconnector://{base}"]

    chosen = None
    for uri in candidates:
        try:
            api._request("POST", "/api/v1/database/test_connection",
                         json_data={"sqlalchemy_uri": uri, "database_name": DB_NAME})
            chosen = uri
            print(f"  test_connection OK with driver: {uri.split('://')[0]}")
            break
        except Exception as e:
            print(f"  test_connection failed for {uri.split('://')[0]}: {str(e)[:120]}")
    if chosen is None:
        chosen = candidates[0]
        print(f"  no driver passed test_connection; creating with {chosen.split('://')[0]} anyway")

    created = api._request("POST", "/api/v1/database/", json_data={
        "database_name": DB_NAME,
        "sqlalchemy_uri": chosen,
        "expose_in_sqllab": True,
    })
    db_id = created["id"] if "id" in created else created["result"]["id"]
    print(f"  created Superset DB connection '{DB_NAME}' (id={db_id})")
    return db_id


# --------------------------------------------------------------------------- #
# Datasets (physical MySQL tables) and charts
# --------------------------------------------------------------------------- #
def ensure_dataset(api, database_id: int, table: str) -> int:
    for ds in list_all(api, "dataset"):
        if ds.get("table_name") == table and ds.get("schema") == SCHEMA:
            return ds["id"]
    return api.create_dataset(database_id=database_id, table_name=table, schema=SCHEMA)["id"]


def ensure_sql_dataset(api, database_id: int, name: str, sql: str) -> int:
    """Create (or refresh) a virtual dataset backed by a SQL query."""
    existing = next((ds for ds in list_all(api, "dataset")
                     if ds.get("table_name") == name), None)
    if existing:
        api.update_dataset(existing["id"], sql=sql)
        return existing["id"]
    return api.create_dataset(database_id=database_id, table_name=name,
                              schema=SCHEMA, sql=sql)["id"]


def replace_chart(api, slice_name: str, viz_type: str, datasource_id: int, params: dict) -> int:
    """Create the chart, or update its params if a chart with this name exists."""
    params = {**params, "viz_type": viz_type, "datasource": f"{datasource_id}__table"}
    existing = next((c for c in list_all(api, "chart") if c.get("slice_name") == slice_name), None)
    if existing:
        api.update_chart(existing["id"], params=json.dumps(params), viz_type=viz_type)
        return existing["id"]
    return api.create_chart(slice_name=slice_name, viz_type=viz_type,
                            datasource_id=datasource_id, datasource_type="table",
                            params=json.dumps(params))["id"]


def bar(x_col: str, metric: dict, limit: int = 25) -> dict:
    return {
        "x_axis": x_col,
        "x_axis_force_categorical": True,
        "metrics": [metric],
        "groupby": [],
        "adhoc_filters": [],
        "row_limit": limit,
        "orientation": "horizontal",
        "order_desc": True,
        "timeseries_limit_metric": metric,
        "x_axis_sort": metric["label"],
        "x_axis_sort_asc": False,
        "show_legend": False,
        "y_axis_format": "SMART_NUMBER",
    }


# --------------------------------------------------------------------------- #
# Dashboard layout
# --------------------------------------------------------------------------- #
def build_position_json(charts: dict[str, dict]) -> str:
    """charts[key] = {"id", "name", "w", "h"}; rows defined below."""
    layout = {
        "DASHBOARD_VERSION_KEY": "v2",
        "ROOT_ID": {"type": "ROOT", "id": "ROOT_ID", "children": ["GRID_ID"]},
        "GRID_ID": {"type": "GRID", "id": "GRID_ID", "children": [], "parents": ["ROOT_ID"]},
        "HEADER_ID": {"id": "HEADER_ID", "type": "HEADER", "meta": {"text": DASHBOARD_TITLE}},
    }

    def chart_node(key):
        c = charts[key]
        nid = f"CHART-{key}"
        layout[nid] = {
            "type": "CHART", "id": nid, "children": [], "parents": ["ROOT_ID", "GRID_ID"],
            "meta": {"width": c["w"], "height": c["h"], "chartId": c["id"], "sliceName": c["name"]},
        }
        return nid

    def row(rid, keys):
        kids = [chart_node(k) for k in keys]
        layout[rid] = {"type": "ROW", "id": rid, "children": kids,
                       "parents": ["ROOT_ID", "GRID_ID"], "meta": {"background": "BACKGROUND_TRANSPARENT"}}
        for k in kids:
            layout[k]["parents"] = ["ROOT_ID", "GRID_ID", rid]
        layout["GRID_ID"]["children"].append(rid)

    row("ROW-1", ["k_fv", "k_model"])
    row("ROW-2", ["fv_usage"])
    row("ROW-3", ["per_model", "reuse_models"])
    row("ROW-4", ["reuse_hist"])
    row("ROW-5", ["sdlc_features"])
    return json.dumps(layout)


def ensure_dashboard(api, charts: dict[str, dict]) -> int:
    position_json = build_position_json(charts)
    dash = next((d for d in list_all(api, "dashboard")
                 if d.get("dashboard_title") == DASHBOARD_TITLE), None)
    if dash is None:
        did = api.create_dashboard(dashboard_title=DASHBOARD_TITLE, published=True,
                                   position_json=position_json)["id"]
    else:
        did = dash["id"]
        api.update_dashboard(did, dashboard_title=DASHBOARD_TITLE, published=True,
                             position_json=position_json)
    for c in charts.values():
        api.update_chart(c["id"], dashboards=[did])
    return did


# --------------------------------------------------------------------------- #
def main() -> None:
    project = hopsworks.login()
    fs = project.get_feature_store()
    sc = fs.get_data_source(DB_NAME).storage_connector
    api = project.get_superset_api()

    print("Setting up MySQL database connection in Superset...")
    db_id = ensure_database(api, sc)

    print("Registering datasets (underlying MySQL tables)...")
    ds_summary = ensure_dataset(api, db_id, T_SUMMARY)
    ds_fv = ensure_dataset(api, db_id, T_FV)
    ds_model = ensure_dataset(api, db_id, T_MODEL)
    ds_sdlc = ensure_sql_dataset(api, db_id, SDLC_DATASET, SDLC_DATASET_SQL)

    print("Building charts...")
    charts: dict[str, dict] = {}

    # KPI: total feature->feature-view usages
    charts["k_fv"] = {"name": "Feature→FeatureView usages", "w": 6, "h": 25, "id": replace_chart(
        api, "Feature→FeatureView usages", "big_number_total", ds_fv,
        {"metric": COUNT, "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "feature uses across all feature views"})}

    # KPI: total feature->model usages
    charts["k_model"] = {"name": "Feature→Model usages", "w": 6, "h": 25, "id": replace_chart(
        api, "Feature→Model usages", "big_number_total", ds_model,
        {"metric": COUNT, "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "feature uses across all model versions"})}

    # Feature usage histogram: how many times each feature is used in a feature view
    charts["fv_usage"] = {"name": "Feature usage in feature views (top 25)", "w": 12, "h": 55, "id": replace_chart(
        api, "Feature usage in feature views (top 25)", "echarts_timeseries_bar", ds_fv,
        bar("feature_name", COUNT, limit=25))}

    # How many features are used in different models
    charts["per_model"] = {"name": "Features used per model", "w": 6, "h": 55, "id": replace_chart(
        api, "Features used per model", "echarts_timeseries_bar", ds_model,
        bar("model_name", DISTINCT_FEATURES, limit=25))}

    # Feature reuse across models: how many model versions reuse each feature
    charts["reuse_models"] = {"name": "Feature reuse across models (top 25)", "w": 6, "h": 55, "id": replace_chart(
        api, "Feature reuse across models (top 25)", "echarts_timeseries_bar", ds_model,
        bar("feature_name", DISTINCT_MODELS, limit=25))}

    # Reuse distribution: histogram of models_count per feature (reused features only)
    charts["reuse_hist"] = {"name": "Feature reuse distribution (models per feature)", "w": 12, "h": 55, "id": replace_chart(
        api, "Feature reuse distribution (models per feature)", "histogram_v2", ds_summary,
        {"column": "models_count", "groupby": [], "bins": 15, "row_limit": 50000,
         "normalize": False, "cumulative": False,
         "adhoc_filters": [{"expressionType": "SQL", "sqlExpression": "models_count > 0", "clause": "WHERE"}],
         "x_axis_title": "# models using a feature", "y_axis_title": "# features",
         "x_axis_format": "SMART_NUMBER", "y_axis_format": "SMART_NUMBER"})}

    # Number of features across feature groups tagged with the `sdlc` schematized
    # tag, broken down by the tag's `status` value (dev / staging / prod).
    charts["sdlc_features"] = {"name": "Features in sdlc-tagged feature groups by status", "w": 12, "h": 55, "id": replace_chart(
        api, "Features in sdlc-tagged feature groups by status", "echarts_timeseries_bar", ds_sdlc,
        {**bar("sdlc_status", SUM_FEATURES, limit=25),
         "x_axis_title": "sdlc status", "y_axis_title": "# features"})}

    print("Assembling dashboard...")
    did = ensure_dashboard(api, charts)

    try:
        base = api._get_superset_url()
    except Exception:
        base = ""
    print(f"\nDashboard id: {did}")
    print(f"URL: {base}/superset/dashboard/{did}/")


if __name__ == "__main__":
    main()
