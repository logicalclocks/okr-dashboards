"""
Create a denormalized Superset dataset over the Hopsworks tag metadata.

The Hopsworks metadata DB has two tag tables (reached through the
`mysql_hopsworks` Superset JDBC connection, schema `hopsworks`):

  feature_store_tag(id, name, tag_schema)
      tag_schema is a JSON-Schema-ish string:
        {"name": ..., "properties": {"<field>": {"type": "string|boolean|
          number|integer", ...}, ...}, "required": [...]}

  feature_store_tag_value(id, schema_id, feature_group_id, feature_view_id,
                          training_dataset_id, value)
      value is a JSON string mapping each schema field -> the tagged value.
      Exactly one of the *_id columns is set (the tagged artifact).

This program:
  1. reads every tag schema and expands its `properties` into field columns,
  2. builds a virtual (SQL) dataset that pivots tag values to ONE ROW PER
     ARTIFACT, exposing every `<tag>_<field>` as its own typed column,
  3. creates/updates that dataset in Superset on the mysql_hopsworks connection.

Run:  python create_tag_dataset.py
"""
import json
import re
import sys

import hopsworks

SCHEMA = "hopsworks"                       # MySQL schema holding the tag tables
DATASET_NAME = "feature_store_tags_denormalized"
DASHBOARD_TITLE = "Feature Store Tags Overview"
CHART_PREFIX = "Tags · "                   # namespaces charts for idempotency

# Identity columns always present in the denormalized dataset.
ID_COLS = ["artifact_type", "artifact_name", "artifact_version",
           "feature_group_id", "feature_view_id", "training_dataset_id"]

COUNT_METRIC = {
    "expressionType": "SQL", "sqlExpression": "COUNT(*)",
    "label": "count", "optionName": "metric_count", "hasCustomLabel": True,
}


def find_mysql_db_id(api):
    """The mysql_hopsworks connection: the only mysql-backend DB in Superset."""
    for db in api.list_databases()["result"]:
        if (db.get("backend") or "").lower() == "mysql":
            return db["id"], db.get("database_name")
    raise RuntimeError("No mysql backend (mysql_hopsworks) connection in Superset")


def run_sql(api, db_id, sql):
    body = {
        "database_id": db_id,
        "sql": sql,
        "schema": SCHEMA,
        "runAsync": False,
        "select_as_cta": False,
        "json": True,
    }
    r = api._request("POST", "/api/v1/sqllab/execute/", json_data=body)
    cols = [c["name"] for c in r.get("columns", [])]
    return [dict(zip(cols, [row.get(c) for c in cols])) for row in r.get("data", [])]


def sanitize(name):
    s = re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower()).strip("_")
    return s or "field"


def extract_expr(field_type):
    """How to read one JSON field from `tv.value`, cast to its schema type."""
    path = "JSON_EXTRACT(tv.value, {path})"
    if field_type in ("number",):
        return lambda p: f"CAST(JSON_EXTRACT(tv.value, {p}) AS DECIMAL(38,10))"
    if field_type in ("integer",):
        return lambda p: f"CAST(JSON_EXTRACT(tv.value, {p}) AS SIGNED)"
    # string, boolean, enum, or anything else -> unquoted scalar text
    return lambda p: f"JSON_UNQUOTE(JSON_EXTRACT(tv.value, {p}))"


def build_columns(tags):
    """Flatten tags into one descriptor per (tag, field) -> dataset column.

    Returns list of dicts: {col, tag, field, type, numeric, schema_id}.
    """
    cols, used = [], set()
    for tag in tags:
        tag_label = sanitize(tag["name"])
        for field_name, field_type in tag["fields"]:
            col = f"{tag_label}_{sanitize(field_name)}"
            base, i = col, 2
            while col in used:                # guarantee uniqueness
                col = f"{base}_{i}"
                i += 1
            used.add(col)
            cols.append({
                "col": col, "tag": tag["name"], "field": field_name,
                "type": field_type, "schema_id": tag["id"],
                "numeric": field_type in ("number", "integer"),
            })
    return cols


def build_sql(columns):
    selects = []
    for c in columns:
        esc = c["field"].replace("\\", "\\\\").replace('"', '\\"')
        jpath = f"'$.\"{esc}\"'"
        val = extract_expr(c["type"])(jpath)
        selects.append(
            f"    MAX(CASE WHEN tv.schema_id = {c['schema_id']} "
            f"THEN {val} END) AS `{c['col']}`"
        )

    if not selects:
        raise RuntimeError("No tag schema fields found — nothing to denormalize")

    sql = f"""SELECT
    CASE
        WHEN tv.feature_group_id    IS NOT NULL THEN 'FEATURE_GROUP'
        WHEN tv.feature_view_id     IS NOT NULL THEN 'FEATURE_VIEW'
        WHEN tv.training_dataset_id IS NOT NULL THEN 'TRAINING_DATASET'
    END                                                     AS artifact_type,
    COALESCE(fg.name, fv.name, td.name)                     AS artifact_name,
    COALESCE(fg.version, fv.version, td.version)            AS artifact_version,
    tv.feature_group_id                                     AS feature_group_id,
    tv.feature_view_id                                      AS feature_view_id,
    tv.training_dataset_id                                  AS training_dataset_id,
{",\n".join(selects)}
FROM {SCHEMA}.feature_store_tag_value tv
LEFT JOIN {SCHEMA}.feature_group    fg ON fg.id = tv.feature_group_id
LEFT JOIN {SCHEMA}.feature_view     fv ON fv.id = tv.feature_view_id
LEFT JOIN {SCHEMA}.training_dataset td ON td.id = tv.training_dataset_id
GROUP BY
    artifact_type, artifact_name, artifact_version,
    tv.feature_group_id, tv.feature_view_id, tv.training_dataset_id"""
    return sql


def load_tags(api, db_id):
    rows = run_sql(api, db_id,
                   "SELECT id, name, tag_schema FROM feature_store_tag ORDER BY id")
    tags = []
    for r in rows:
        try:
            schema = json.loads(r["tag_schema"])
        except (TypeError, ValueError) as e:
            print(f"  ! skipping tag {r.get('name')!r}: bad schema JSON ({e})")
            continue
        props = schema.get("properties") or {}
        fields = [(fname, (fspec or {}).get("type", "string"))
                  for fname, fspec in props.items()]
        tags.append({"id": int(r["id"]), "name": r["name"], "fields": fields})
        flist = ", ".join(f"{n}:{t}" for n, t in fields)
        print(f"  tag #{r['id']} {r['name']!r} -> [{flist}]")
    return tags


def ensure_dataset(api, db_id, name, sql):
    # list-then-create/update (create fails if (schema, table_name) exists)
    page, existing = 0, None
    while True:
        j = api._request(
            "GET", f"/api/v1/dataset/?q=(page:{page},page_size:100)")
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

    # Superset does NOT re-introspect a virtual dataset's columns when its SQL
    # changes — the column list is persisted and goes stale. Force a re-sync so
    # newly added tag columns get registered, and disable result caching so
    # charts always reflect freshly added tag values.
    api._request("PUT", f"/api/v1/dataset/{ds_id}/refresh")
    api.update_dataset(ds_id, cache_timeout=0)
    cols = api.get_dataset(ds_id).get("result", {}).get("columns", [])
    print(f"  synced {len(cols)} columns; cache disabled (cache_timeout=0)")
    return ds_id


def list_all(api, resource):
    items, page = [], 0
    while True:
        j = api._request(
            "GET", f"/api/v1/{resource}/?q=(page:{page},page_size:100)")
        batch = j.get("result", [])
        items.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return items


def not_null_filter(col):
    return [{"expressionType": "SQL",
             "sqlExpression": f"`{col}` IS NOT NULL", "clause": "WHERE"}]


def chart_specs(dataset_id, columns):
    """Build the (slice_name, viz_type, params, width) list for the dashboard."""
    specs = []

    # 1. KPI: total tagged artifacts
    specs.append((
        f"{CHART_PREFIX}Total Tagged Artifacts", "big_number_total",
        {"viz_type": "big_number_total", "metric": COUNT_METRIC,
         "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "artifacts with at least one tag"},
        4, 30,
    ))

    # 2. Bar: artifacts by type (feature group / feature view / training dataset)
    specs.append((
        f"{CHART_PREFIX}Artifacts by Type", "echarts_timeseries_bar",
        {"viz_type": "echarts_timeseries_bar", "x_axis": "artifact_type",
         "x_axis_force_categorical": True, "metrics": [COUNT_METRIC],
         "groupby": [], "adhoc_filters": [], "row_limit": 100,
         "orientation": "vertical", "order_desc": True,
         "timeseries_limit_metric": COUNT_METRIC, "x_axis_sort": "count",
         "x_axis_sort_asc": False, "show_legend": False,
         "y_axis_format": "SMART_NUMBER"},
        8, 30,
    ))

    # 3. One chart per tag field: pie for categorical, histogram for numeric
    for c in columns:
        title = f"{CHART_PREFIX}{c['tag']} · {c['field']}"
        if c["numeric"]:
            specs.append((
                title, "histogram_v2",
                {"viz_type": "histogram_v2", "column": c["col"], "groupby": [],
                 "adhoc_filters": not_null_filter(c["col"]), "row_limit": 50000,
                 "bins": 20, "x_axis_title": c["field"], "y_axis_title": "Artifacts",
                 "x_axis_format": "SMART_NUMBER", "y_axis_format": "SMART_NUMBER"},
                6, 50,
            ))
        else:
            specs.append((
                title, "pie",
                {"viz_type": "pie", "groupby": [c["col"]], "metric": COUNT_METRIC,
                 "adhoc_filters": not_null_filter(c["col"]), "row_limit": 100,
                 "sort_by_metric": True, "show_legend": True,
                 "label_type": "key_value_percent", "donut": True,
                 "innerRadius": 35, "outerRadius": 75},
                6, 50,
            ))

    # 4. Detail table: every artifact + all its tag values
    all_cols = ID_COLS + [c["col"] for c in columns]
    specs.append((
        f"{CHART_PREFIX}Tagged Artifacts Detail", "table",
        {"viz_type": "table", "query_mode": "raw", "all_columns": all_cols,
         "adhoc_filters": [], "row_limit": 1000,
         "order_by_cols": [], "table_timestamp_format": "smart_date"},
        12, 60,
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
    for ch in charts:                          # persist chart -> dashboard link
        api.update_chart(ch["id"], dashboards=[dash_id])
    return dash_id


def main():
    project = hopsworks.login()
    api = project.get_superset_api()

    db_id, db_name = find_mysql_db_id(api)
    print(f"mysql_hopsworks connection: id={db_id} ({db_name})\n")

    print("Expanding tag schemas:")
    tags = load_tags(api, db_id)
    if not tags:
        sys.exit("No tags found in feature_store_tag")

    columns = build_columns(tags)
    sql = build_sql(columns)
    print("\nGenerated denormalized SQL:\n")
    print(sql)
    print()

    # sanity-check the SQL actually runs before registering it as a dataset
    preview = run_sql(api, db_id, sql + "\nLIMIT 5")
    print(f"Preview returned {len(preview)} row(s). Sample:")
    for row in preview:
        print("  ", json.dumps(row, default=str))

    ds_id = ensure_dataset(api, db_id, DATASET_NAME, sql)
    host = api._get_superset_url() if hasattr(api, "_get_superset_url") else ""
    print(f"\nDataset '{DATASET_NAME}' ready (id={ds_id}).")
    print(f"Explore it: {host}/hopsworks-api/superset/explore/"
          f"?datasource_type=table&datasource_id={ds_id}")

    # ---- charts + dashboard ----
    print("\nCreating charts:")
    charts = []
    for slice_name, viz_type, params, width, height in chart_specs(ds_id, columns):
        cid = replace_chart(api, slice_name, viz_type, ds_id, params)
        charts.append({"id": cid, "name": slice_name,
                       "width": width, "height": height})
        print(f"  [{viz_type}] {slice_name} -> id={cid}")

    dash_id = ensure_dashboard(api, DASHBOARD_TITLE, charts)
    print(f"\nDashboard '{DASHBOARD_TITLE}' ready (id={dash_id}).")
    print(f"Open it: {host}/hopsworks-api/superset/superset/dashboard/{dash_id}/")


if __name__ == "__main__":
    main()
