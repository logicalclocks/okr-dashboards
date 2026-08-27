"""
Build the Superset "Analyst/Data Scientist Dashboard" over the Hopsworks
metadata DB (reached through the `hopsworks_analytics` JDBC connection, schema
`hopsworks`; NO Trino).

This MERGES the two former dashboards into one analyst-facing view:

  * feature_group_dashboard.py  -> "Feature Group Activity"
        feature / feature-group COUNTS and GROWTH (new features per week,
        bucketed to the Monday of the week each feature group was created),
        sliceable by schematized tag values and feature-group kind.

  * feature_usage_dashboard.py  -> "Feature Usage & Reuse"
        feature REUSE across feature views and models, plus the count of
        features in lifecycle-tagged feature groups broken down by status.

Everything reads the underlying `hopsworks.*` MySQL tables (which also happen
to be mounted as feature groups in this project) through the one pre-provisioned
`hopsworks_analytics` Superset connection.

DATASETS
--------
  feature_group_inventory          (virtual) one row per (feature × tag value of
                                    its FG); untagged features appear once.
  feature_group_feature_usage      (physical) per-feature usage summary.
  feature_group_feature_usage_..._view  (physical) one row per (feature, FV).
  feature_group_feature_usage_model     (physical) one row per (feature, model).
  lifecycle_tagged_feature_counts  (virtual) one row per lifecycle-tagged FG.
                                    The tag schema is resolved from the cluster
                                    (--tag overrides); the chart is skipped when
                                    the cluster has none.

NATIVE FILTER SELECTION BOXES
-----------------------------
Three native filters (Tag, Tag value, Feature group kind) are scoped to ONLY
the feature-group-activity charts (they read the `feature_group_inventory`
dataset). The usage / reuse / status charts are excluded from those filters
because their datasets don't carry the tag columns.

Idempotent (list-then-create/update, replace-charts-by-name under the
"Analyst · " prefix) with result caching disabled on the virtual datasets, so
it reflects current state on every open. Charts are namespaced under their own
prefix so re-running never disturbs the standalone dashboards.

Run:  python analyst_dashboard.py
"""
import argparse
import json

import hopsworks

# Reuse the shared Superset plumbing from the tag-dashboard builder.
from superset import resolve_lifecycle_tag
from create_tag_dataset import (
    SCHEMA,
    build_position_json,
    ensure_dataset,            # virtual SQL dataset (refresh + cache-disable)
    find_mysql_db_id,
    json_value_expr,
    list_all,
    load_tags,
    replace_chart,
    run_sql,
    sql_str,
)

TITLE = "Analyst/Data Scientist Dashboard"
PREFIX = "Analyst · "                          # namespaces charts for idempotency

# --- Datasets --------------------------------------------------------------- #
INVENTORY_DATASET = "feature_group_inventory"
T_SUMMARY = "feature_group_feature_usage"
T_FV = "feature_group_feature_usage_feature_view"
T_MODEL = "feature_group_feature_usage_model"
STATUS_DATASET = "lifecycle_tagged_feature_counts"

# Virtual (SQL-backed) dataset: one row per feature group carrying the lifecycle
# tag, exposing the tag's `status` value and the number of features in that feature
# group. The tag name is resolved against the cluster rather than hardcoded: this
# used to read 'sdlc', which exists on no cluster this repo currently targets, so the
# chart rendered empty everywhere with nothing to say why.
def status_dataset_sql(tag_name):
    return f"""
SELECT fg.id AS feature_group_id,
       fg.name AS feature_group_name,
       JSON_UNQUOTE(JSON_EXTRACT(tv.value, '$.status')) AS status,
       (SELECT COUNT(*) FROM {SCHEMA}.cached_feature cf
          WHERE cf.cached_feature_group_id = fg.cached_feature_group_id
             OR cf.stream_feature_group_id = fg.stream_feature_group_id)
     + (SELECT COUNT(*) FROM {SCHEMA}.on_demand_feature odf
          WHERE odf.on_demand_feature_group_id = fg.on_demand_feature_group_id)
         AS n_features
FROM {SCHEMA}.feature_store_tag_value tv
JOIN {SCHEMA}.feature_store_tag t ON t.id = tv.schema_id AND t.name = {sql_str(tag_name)}
JOIN {SCHEMA}.feature_group fg ON fg.id = tv.feature_group_id
WHERE tv.feature_group_id IS NOT NULL
""".strip()


# Charts this builder used to create under a hardcoded tag name. Deleted on every run so
# renaming the chart does not leave the old one behind in Superset.
RETIRED_CHARTS = ["Features in sdlc-tagged feature groups by status"]


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def sql_metric(expr, label):
    return {
        "expressionType": "SQL", "sqlExpression": expr, "label": label,
        "optionName": f"metric_{label}", "hasCustomLabel": True,
    }


# COUNT(DISTINCT ...) keeps the inventory counts correct under the tag-join fan-out.
FEATURES_METRIC = sql_metric("COUNT(DISTINCT feature_id)", "features")
FG_METRIC = sql_metric("COUNT(DISTINCT fg_id)", "feature_groups")
# Usage / reuse metrics over the physical usage tables.
COUNT = sql_metric("COUNT(*)", "count")
DISTINCT_FEATURES = sql_metric("COUNT(DISTINCT feature_name)", "distinct_features")
DISTINCT_MODELS = sql_metric("COUNT(DISTINCT model_version_id)", "distinct_models")
SUM_FEATURES = sql_metric("SUM(n_features)", "total_features")


# --------------------------------------------------------------------------- #
# Feature-group inventory SQL (Feature Group Activity)
# --------------------------------------------------------------------------- #
def build_fg_tag_expansion(tags):
    """One row per (feature_group_id × tag-field value). None if no tags.

    Mirrors the columns of the `feature_store_tags_by_value` dataset, but keyed
    by feature_group_id so it can be joined onto each feature's parent FG.
    """
    blocks = []
    for tag in tags:
        for field_name, _ftype in tag["fields"]:
            val = json_value_expr(field_name)
            tag_field = f"{tag['name']} · {field_name}"
            blocks.append(f"""        SELECT
            tv.feature_group_id                              AS feature_group_id,
            {sql_str(tag['name'])}                           AS tag,
            {sql_str(field_name)}                            AS field,
            {sql_str(tag_field)}                             AS tag_field,
            {val}                                            AS tag_value,
            CONCAT({sql_str(tag_field + ' = ')}, {val})      AS value_label
        FROM {SCHEMA}.feature_store_tag_value tv
        WHERE tv.schema_id = {tag['id']}
          AND tv.feature_group_id IS NOT NULL
          AND {val} IS NOT NULL""")
    if not blocks:
        return None
    return "\n        UNION ALL\n".join(blocks)


def build_inventory_sql(tags):
    """One row per (feature × tag value of its FG); untagged features once."""
    feat = f"""    SELECT CONCAT('cf-', cf.id)                  AS feature_id,
           cf.name                               AS feature_name,
           fg.id AS fg_id, fg.name AS fg_name, fg.version AS fg_version,
           fg.created AS created, fg.online_enabled AS online_enabled,
           fg.feature_store_id AS feature_store_id, 'Cached/Stream' AS fg_kind
    FROM {SCHEMA}.cached_feature cf
    JOIN {SCHEMA}.feature_group fg
      ON (cf.cached_feature_group_id IS NOT NULL
          AND fg.cached_feature_group_id = cf.cached_feature_group_id)
      OR (cf.stream_feature_group_id IS NOT NULL
          AND fg.stream_feature_group_id = cf.stream_feature_group_id)
    UNION ALL
    SELECT CONCAT('odf-', odf.id), odf.name,
           fg.id, fg.name, fg.version, fg.created, fg.online_enabled,
           fg.feature_store_id, 'External'
    FROM {SCHEMA}.on_demand_feature odf
    JOIN {SCHEMA}.feature_group fg
      ON fg.on_demand_feature_group_id = odf.on_demand_feature_group_id
    UNION ALL
    SELECT CONCAT('ef-', ef.id), ef.name,
           fg.id, fg.name, fg.version, fg.created, fg.online_enabled,
           fg.feature_store_id, 'Embedding'
    FROM {SCHEMA}.embedding_feature ef
    JOIN {SCHEMA}.embedding e ON e.id = ef.embedding_id
    JOIN {SCHEMA}.feature_group fg ON fg.id = e.feature_group_id"""

    expansion = build_fg_tag_expansion(tags)
    if expansion:
        tag_cols = """COALESCE(t.tag, '(untagged)')          AS tag,
    COALESCE(t.field, '(untagged)')        AS field,
    COALESCE(t.tag_field, '(untagged)')    AS tag_field,
    COALESCE(t.tag_value, '(untagged)')    AS tag_value,
    COALESCE(t.value_label, '(untagged)')  AS value_label"""
        tag_join = f"""LEFT JOIN (
{expansion}
    ) t ON t.feature_group_id = feat.fg_id"""
    else:
        tag_cols = """'(untagged)' AS tag, '(untagged)' AS field,
    '(untagged)' AS tag_field, '(untagged)' AS tag_value, '(untagged)' AS value_label"""
        tag_join = ""

    return f"""SELECT
    feat.feature_id, feat.feature_name,
    feat.fg_id, feat.fg_name, feat.fg_version,
    feat.created,
    CAST(DATE_SUB(DATE(feat.created), INTERVAL WEEKDAY(feat.created) DAY) AS DATE) AS created_week,
    CASE WHEN feat.online_enabled = 1 THEN 'Online' ELSE 'Offline' END AS availability,
    feat.fg_kind,
    fs.name AS feature_store_name,
    -- The owning project, so every chart on this dashboard can be sliced by it.
    p.projectname AS project_name,
    {tag_cols}
FROM (
{feat}
) feat
LEFT JOIN {SCHEMA}.feature_store fs ON fs.id = feat.feature_store_id
LEFT JOIN {SCHEMA}.project p ON p.id = fs.project_id
{tag_join}"""


# --------------------------------------------------------------------------- #
# Chart param builders
# --------------------------------------------------------------------------- #
def weekly_bar(metric, groupby):
    return {
        "viz_type": "echarts_timeseries_bar", "x_axis": "created_week",
        "x_axis_force_categorical": False, "metrics": [metric],
        "groupby": groupby, "adhoc_filters": [], "row_limit": 10000,
        "orientation": "vertical", "stack": bool(groupby), "show_legend": True,
        "x_axis_title": "week (Mon)", "y_axis_format": "SMART_NUMBER",
        "x_axis_time_format": "smart_date",
    }


def categorical_bar(x_axis, metric, groupby=None):
    return {
        "viz_type": "echarts_timeseries_bar", "x_axis": x_axis,
        "x_axis_force_categorical": True, "metrics": [metric],
        "groupby": groupby or [], "adhoc_filters": [], "row_limit": 1000,
        "orientation": "horizontal" if x_axis != "fg_kind" else "vertical",
        "order_desc": True, "stack": bool(groupby), "show_legend": bool(groupby),
        "timeseries_limit_metric": metric, "x_axis_sort": "count",
        "x_axis_sort_asc": False, "y_axis_format": "SMART_NUMBER",
    }


def bar(x_col, metric, limit=25):
    """Horizontal ranked bar over a physical usage table (top-N by metric)."""
    return {
        "viz_type": "echarts_timeseries_bar", "x_axis": x_col,
        "x_axis_force_categorical": True, "metrics": [metric], "groupby": [],
        "adhoc_filters": [], "row_limit": limit, "orientation": "horizontal",
        "order_desc": True, "timeseries_limit_metric": metric,
        "x_axis_sort": metric["label"], "x_axis_sort_asc": False,
        "show_legend": False, "y_axis_format": "SMART_NUMBER",
    }


# --------------------------------------------------------------------------- #
# Datasets
# --------------------------------------------------------------------------- #
def ensure_physical_dataset(api, db_id, table):
    """Register a physical MySQL table as a Superset dataset (idempotent)."""
    for ds in list_all(api, "dataset"):
        if ds.get("table_name") == table and ds.get("schema") == SCHEMA:
            return ds["id"]
    return api.create_dataset(database_id=db_id, table_name=table, schema=SCHEMA)["id"]


# --------------------------------------------------------------------------- #
# Native filter selection boxes (scoped to the inventory charts only)
# --------------------------------------------------------------------------- #
def native_filter(fid, name, ds_id, column, multi, excluded, default_first=False):
    return {
        "id": f"NATIVE_FILTER-{fid}", "name": name, "filterType": "filter_select",
        "type": "NATIVE_FILTER",
        "targets": [{"datasetId": ds_id, "column": {"name": column}}],
        "controlValues": {
            "multiSelect": multi, "enableEmptyFilter": False,
            "defaultToFirstItem": default_first, "inverseSelection": False,
            "searchAllOptions": False,
        },
        # excluded: chart ids whose datasets lack the tag columns (usage/status).
        "scope": {"rootPath": ["ROOT_ID"], "excluded": excluded},
        "defaultDataMask": {"filterState": {}, "extraFormData": {}},
        "cascadeParentIds": [],
    }


def build_json_metadata(ds_id, excluded):
    return json.dumps({
        "native_filter_configuration": [
            # Single-select: pick the tag dimension to group/slice by.
            native_filter("tagfield", "Tag", ds_id, "tag_field",
                          multi=False, excluded=excluded, default_first=False),
            # Multi-select: narrow to specific tag values.
            native_filter("tagvalue", "Tag value", ds_id, "tag_value",
                          multi=True, excluded=excluded),
            # Bonus: filter by feature kind.
            native_filter("fgkind", "Feature group kind", ds_id, "fg_kind",
                          multi=True, excluded=excluded),
            # Slice the whole dashboard by project. Same exclusions as the others: the usage
            # and status datasets do not carry the column, and a filter over a column a chart
            # cannot see makes that chart error rather than ignore it.
            native_filter("project", "Project", ds_id, "project_name",
                          multi=True, excluded=excluded),
        ],
        "cross_filters_enabled": False,
    })


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
                             position_json=position_json,
                             json_metadata=json_metadata)
        print(f"Updated dashboard id={dash_id}")
    for ch in charts:                              # persist chart -> dashboard link
        api.update_chart(ch["id"], dashboards=[dash_id])
    return dash_id


# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--tag",
        help="lifecycle tag schema for the status chart; resolved from the cluster when "
             "omitted (see superset.LIFECYCLE_TAG_ORDER)",
    )
    args = parser.parse_args()

    project = hopsworks.login()
    api = project.get_superset_api()

    db_id, db_name = find_mysql_db_id(api)
    print(f"hopsworks_analytics connection: id={db_id} ({db_name})\n")

    tags = load_tags(api, db_id)
    if not tags:
        print("No tags found — 'group by tag' will only offer '(untagged)'.")

    # --- Datasets ----------------------------------------------------------- #
    inv_sql = build_inventory_sql(tags)
    preview = run_sql(api, db_id, f"SELECT * FROM (\n{inv_sql}\n) _p LIMIT 5")
    print(f"\nInventory preview returned {len(preview)} row(s). Sample:")
    for row in preview:
        print("  ", json.dumps(row, default=str))

    ds_inv = ensure_dataset(api, db_id, INVENTORY_DATASET, inv_sql)
    print(f"Dataset '{INVENTORY_DATASET}' ready (id={ds_inv}).")

    ds_summary = ensure_physical_dataset(api, db_id, T_SUMMARY)
    ds_fv = ensure_physical_dataset(api, db_id, T_FV)
    ds_model = ensure_physical_dataset(api, db_id, T_MODEL)
    status_tag = resolve_lifecycle_tag(lambda sql: run_sql(api, db_id, sql), args.tag)
    if status_tag:
        ds_status = ensure_dataset(api, db_id, STATUS_DATASET,
                                   status_dataset_sql(status_tag))
        print(f"Lifecycle tag: {status_tag!r} (dataset {STATUS_DATASET} id={ds_status})")
    else:
        # No lifecycle tag on this cluster. The chart is dropped rather than built over a
        # join that matches nothing, which is what an empty chart with no explanation is.
        ds_status = None
        print("No lifecycle tag schema on this cluster; the status chart is skipped. "
              "Run mount_hopsworks_db.py to create 'asset_lifecycle'.")
    print(f"Usage datasets ready: summary={ds_summary} fv={ds_fv} model={ds_model}\n")

    for retired in RETIRED_CHARTS:
        for chart in list_all(api, "chart"):
            if chart.get("slice_name") == f"{PREFIX}{retired}":
                api.delete_chart(chart["id"])
                print(f"Deleted retired chart {chart['slice_name']!r}")

    # --- Charts ------------------------------------------------------------- #
    charts = []           # ordered list of {id, name, width, height} for layout
    inventory_cids = []   # chart ids the tag/kind native filters should target

    def add(slice_name, viz_type, ds_id, params, width, height, inventory=False):
        full_name = f"{PREFIX}{slice_name}"
        cid = replace_chart(api, full_name, viz_type, ds_id,
                            {**params, "viz_type": viz_type})
        charts.append({"id": cid, "name": full_name, "width": width, "height": height})
        if inventory:
            inventory_cids.append(cid)
        print(f"  [{viz_type}] {full_name} -> id={cid}")
        return cid

    print("Creating charts:")

    # ===== Feature Group Activity (inventory dataset; tag-filterable) ======= #
    add("Total Features", "big_number_total", ds_inv,
        {"metric": FEATURES_METRIC, "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "features across all feature groups"}, 6, 30, inventory=True)
    add("Total Feature Groups", "big_number_total", ds_inv,
        {"metric": FG_METRIC, "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "feature groups in the feature store"}, 6, 30, inventory=True)
    add("New Features per Week (by tag value)", "echarts_timeseries_bar", ds_inv,
        weekly_bar(FEATURES_METRIC, ["tag_value"]), 12, 52, inventory=True)
    add("New Feature Groups per Week", "echarts_timeseries_bar", ds_inv,
        weekly_bar(FG_METRIC, []), 6, 50, inventory=True)
    add("New Features per Week (by kind)", "echarts_timeseries_bar", ds_inv,
        weekly_bar(FEATURES_METRIC, ["fg_kind"]), 6, 50, inventory=True)
    add("Features by Tag Value", "echarts_timeseries_bar", ds_inv,
        categorical_bar("tag_value", FEATURES_METRIC), 6, 50, inventory=True)
    add("Feature Groups by Kind", "pie", ds_inv,
        {"groupby": ["fg_kind"], "metric": FG_METRIC, "adhoc_filters": [],
         "row_limit": 100, "sort_by_metric": True, "show_legend": True,
         "label_type": "key_value_percent", "donut": True,
         "innerRadius": 35, "outerRadius": 75}, 6, 50, inventory=True)
    add("Feature Inventory", "table", ds_inv,
        {"query_mode": "raw",
         "all_columns": ["fg_name", "fg_version", "fg_kind", "feature_name",
                         "availability", "feature_store_name", "tag_field",
                         "tag_value", "created"],
         "adhoc_filters": [], "row_limit": 2000, "order_by_cols": [],
         "table_timestamp_format": "smart_date"}, 12, 60, inventory=True)

    # ===== Feature Usage & Reuse (physical usage tables + lifecycle tag) ==== #
    add("Feature→FeatureView usages", "big_number_total", ds_fv,
        {"metric": COUNT, "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "feature uses across all feature views"}, 6, 30)
    add("Feature→Model usages", "big_number_total", ds_model,
        {"metric": COUNT, "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
         "subheader": "feature uses across all model versions"}, 6, 30)
    add("Feature usage in feature views (top 25)", "echarts_timeseries_bar", ds_fv,
        bar("feature_name", COUNT, limit=25), 12, 55)
    add("Features used per model", "echarts_timeseries_bar", ds_model,
        bar("model_name", DISTINCT_FEATURES, limit=25), 6, 55)
    add("Feature reuse across models (top 25)", "echarts_timeseries_bar", ds_model,
        bar("feature_name", DISTINCT_MODELS, limit=25), 6, 55)
    add("Feature reuse distribution (models per feature)", "histogram_v2", ds_summary,
        {"column": "models_count", "groupby": [], "bins": 15, "row_limit": 50000,
         "normalize": False, "cumulative": False,
         "adhoc_filters": [{"expressionType": "SQL",
                            "sqlExpression": "models_count > 0", "clause": "WHERE"}],
         "x_axis_title": "# models using a feature", "y_axis_title": "# features",
         "x_axis_format": "SMART_NUMBER", "y_axis_format": "SMART_NUMBER"}, 12, 55)
    if ds_status is not None:
        add(f"Features by {status_tag} status", "echarts_timeseries_bar",
            ds_status, {**bar("status", SUM_FEATURES, limit=25),
                        "x_axis_title": f"{status_tag} status",
                        "y_axis_title": "# features"}, 12, 55)

    # --- Dashboard ---------------------------------------------------------- #
    # Tag/kind filters target the inventory dataset; exclude the usage/status
    # charts (their datasets don't carry tag_field / tag_value / fg_kind).
    excluded = [c["id"] for c in charts if c["id"] not in inventory_cids]
    json_metadata = build_json_metadata(ds_inv, excluded)

    dash_id = ensure_dashboard(api, TITLE, charts, json_metadata)
    host = api._get_superset_url() if hasattr(api, "_get_superset_url") else ""
    print(f"\nDashboard '{TITLE}' ready (id={dash_id}).")
    print(f"Open it: {host}/hopsworks-api/superset/superset/dashboard/{dash_id}/")


if __name__ == "__main__":
    main()
