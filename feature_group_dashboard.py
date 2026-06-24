"""
Build the Superset "Feature Group Activity" dashboard over the Hopsworks
metadata DB (reached through the `mysql_hopsworks` JDBC connection, schema
`hopsworks`; NO Trino).

It answers two questions about the feature store:

  * COUNTS  — how many feature groups and how many features exist, and
  * GROWTH  — how many new features (and feature groups) were added per week.

A feature's "creation week" is the week its parent feature group was created
(`feature_group.created`), bucketed to the Monday of that week.

Features live in three tables, all folded into one inventory row-per-feature:
  * cached_feature       — cached / stream feature groups
  * on_demand_feature    — external (on-demand) feature groups
  * embedding_feature    — vector-embedding feature groups
This matches the "features" definition used by the executive OKR dashboard.

GROUP-BY-TAG SELECTION BOX
--------------------------
Each feature row is LEFT JOINed to the tag values of its feature group, adding
the same `tag` / `field` / `tag_field` / `tag_value` / `value_label` columns
exposed by the `feature_store_tags_by_value` virtual dataset. The dashboard
ships two native filter "selection boxes":

  * Tag        (single-select on `tag_field`)  — pick which tag dimension to
                                                  slice/group the feature data by
  * Tag value  (multi-select on `tag_value`)   — narrow to specific values

The per-week growth chart is already grouped (stacked) by `tag_value`, so
picking a tag in the selection box turns it into "new features per week, broken
down by that tag's values". Untagged features fall under '(untagged)'.

All metrics use COUNT(DISTINCT ...) so the feature/feature-group counts stay
correct even though the tag join fans rows out (one row per tag value).

Idempotent (list-then-create/update, replace-charts-by-name) with result
caching disabled, so it reflects current state on every open.

Run:  python feature_group_dashboard.py
"""
import json
import sys

import hopsworks

# Reuse the shared Superset plumbing from the tag-dashboard builder.
from create_tag_dataset import (
    SCHEMA,
    build_position_json,
    ensure_dataset,
    find_mysql_db_id,
    json_value_expr,
    list_all,
    load_tags,
    replace_chart,
    run_sql,
    sql_str,
)

DATASET = "feature_group_inventory"
TITLE = "Feature Group Activity"
PREFIX = "FG · "                                # namespaces charts for idempotency

# COUNT(DISTINCT ...) keeps counts correct under the tag-join fan-out.
FEATURES_METRIC = {
    "expressionType": "SQL", "sqlExpression": "COUNT(DISTINCT feature_id)",
    "label": "features", "optionName": "metric_features", "hasCustomLabel": True,
}
FG_METRIC = {
    "expressionType": "SQL", "sqlExpression": "COUNT(DISTINCT fg_id)",
    "label": "feature groups", "optionName": "metric_fgs", "hasCustomLabel": True,
}


# --------------------------------------------------------------------------- #
# SQL
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
    {tag_cols}
FROM (
{feat}
) feat
LEFT JOIN {SCHEMA}.feature_store fs ON fs.id = feat.feature_store_id
{tag_join}"""


# --------------------------------------------------------------------------- #
# Charts
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


def chart_specs():
    """(slice_name, viz_type, params, width, height)."""
    return [
        # KPIs ------------------------------------------------------------- #
        (f"{PREFIX}Total Features", "big_number_total",
         {"viz_type": "big_number_total", "metric": FEATURES_METRIC,
          "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
          "subheader": "features across all feature groups"}, 6, 30),
        (f"{PREFIX}Total Feature Groups", "big_number_total",
         {"viz_type": "big_number_total", "metric": FG_METRIC,
          "adhoc_filters": [], "y_axis_format": "SMART_NUMBER",
          "subheader": "feature groups in the feature store"}, 6, 30),

        # Growth ----------------------------------------------------------- #
        (f"{PREFIX}New Features per Week (by tag value)", "echarts_timeseries_bar",
         weekly_bar(FEATURES_METRIC, ["tag_value"]), 12, 52),
        (f"{PREFIX}New Feature Groups per Week", "echarts_timeseries_bar",
         weekly_bar(FG_METRIC, []), 6, 50),
        (f"{PREFIX}New Features per Week (by kind)", "echarts_timeseries_bar",
         weekly_bar(FEATURES_METRIC, ["fg_kind"]), 6, 50),

        # Distributions ---------------------------------------------------- #
        (f"{PREFIX}Features by Tag Value", "echarts_timeseries_bar",
         categorical_bar("tag_value", FEATURES_METRIC), 6, 50),
        (f"{PREFIX}Feature Groups by Kind", "pie",
         {"viz_type": "pie", "groupby": ["fg_kind"], "metric": FG_METRIC,
          "adhoc_filters": [], "row_limit": 100, "sort_by_metric": True,
          "show_legend": True, "label_type": "key_value_percent", "donut": True,
          "innerRadius": 35, "outerRadius": 75}, 6, 50),

        # Detail ----------------------------------------------------------- #
        (f"{PREFIX}Feature Inventory", "table",
         {"viz_type": "table", "query_mode": "raw",
          "all_columns": ["fg_name", "fg_version", "fg_kind", "feature_name",
                          "availability", "feature_store_name", "tag_field",
                          "tag_value", "created"],
          "adhoc_filters": [], "row_limit": 2000, "order_by_cols": [],
          "table_timestamp_format": "smart_date"}, 12, 60),
    ]


# --------------------------------------------------------------------------- #
# Native filter selection boxes
# --------------------------------------------------------------------------- #
def native_filter(fid, name, ds_id, column, multi, default_first=False):
    return {
        "id": f"NATIVE_FILTER-{fid}", "name": name, "filterType": "filter_select",
        "type": "NATIVE_FILTER",
        "targets": [{"datasetId": ds_id, "column": {"name": column}}],
        "controlValues": {
            "multiSelect": multi, "enableEmptyFilter": False,
            "defaultToFirstItem": default_first, "inverseSelection": False,
            "searchAllOptions": False,
        },
        "scope": {"rootPath": ["ROOT_ID"], "excluded": []},
        "defaultDataMask": {"filterState": {}, "extraFormData": {}},
        "cascadeParentIds": [],
    }


def build_json_metadata(ds_id):
    return json.dumps({
        "native_filter_configuration": [
            # Single-select: pick the tag dimension to group/slice by.
            native_filter("tagfield", "Tag", ds_id, "tag_field",
                          multi=False, default_first=False),
            # Multi-select: narrow to specific tag values.
            native_filter("tagvalue", "Tag value", ds_id, "tag_value", multi=True),
            # Bonus: filter by feature kind.
            native_filter("fgkind", "Feature group kind", ds_id, "fg_kind", multi=True),
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
    project = hopsworks.login()
    api = project.get_superset_api()

    db_id, db_name = find_mysql_db_id(api)
    print(f"mysql_hopsworks connection: id={db_id} ({db_name})\n")

    tags = load_tags(api, db_id)
    if not tags:
        print("No tags found — 'group by tag' will only offer '(untagged)'.")

    sql = build_inventory_sql(tags)
    print(f"Generated SQL for '{DATASET}':\n\n{sql}\n")

    preview = run_sql(api, db_id, f"SELECT * FROM (\n{sql}\n) _p LIMIT 5")
    print(f"Preview returned {len(preview)} row(s). Sample:")
    for row in preview:
        print("  ", json.dumps(row, default=str))

    ds_id = ensure_dataset(api, db_id, DATASET, sql)
    print(f"Dataset '{DATASET}' ready (id={ds_id}).\n")

    print("Creating charts:")
    charts = []
    for slice_name, viz_type, params, width, height in chart_specs():
        cid = replace_chart(api, slice_name, viz_type, ds_id, params)
        charts.append({"id": cid, "name": slice_name, "width": width, "height": height})
        print(f"  [{viz_type}] {slice_name} -> id={cid}")

    dash_id = ensure_dashboard(api, TITLE, charts, build_json_metadata(ds_id))
    host = api._get_superset_url() if hasattr(api, "_get_superset_url") else ""
    print(f"\nDashboard '{TITLE}' ready (id={dash_id}).")
    print(f"Open it: {host}/hopsworks-api/superset/superset/dashboard/{dash_id}/")


if __name__ == "__main__":
    main()
