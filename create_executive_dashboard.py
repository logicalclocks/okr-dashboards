"""
Build the executive OKR dashboard in Superset.

Pairs each OKR *target* (from the `okrs` feature group) against its live
*actual*, computed from the real Hopsworks metadata tables in the `hopsworks`
MySQL database — reached through the `hopsworks_analytics` Superset JDBC connection.
NO Trino: every actual is a COUNT over a live MySQL table, so the dashboard
reflects current state each time it is opened.

Targets live in the `okrs` feature group (offline feature store, not MySQL), so
they are read once at build time and embedded as constants in the dataset SQL.
Re-run this program after changing your OKR targets to refresh them.

Actuals — one COUNT per OKR, over the live `hopsworks` MySQL tables:
    features          cached_feature + on_demand_feature + embedding_feature
    models            model
    model deployments serving
    dashboards        dashboard
    apps              jobs WHERE type = 'PYTHON_APP'

Any other OKR metric (including "agent deployments", whose table was removed
from the schema) shows its target with an actual of 0.

This program:
  1. reads the OKR targets from the `okrs` feature group,
  2. builds a virtual (SQL) dataset on the hopsworks_analytics connection that
     UNIONs one row per OKR: metric, target, live actual, pct-to-target, status,
  3. creates/updates KPI + bar + per-OKR progress-bar + table charts and
     assembles the dashboard.

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

# Charts that used to be on the dashboard but have been removed. Deleted from
# Superset on every run so re-running this program cleans them up.
RETIRED_CHARTS = [
    f"{CHART_PREFIX}Target vs Actual",
    f"{CHART_PREFIX}features — % to target",
    f"{CHART_PREFIX}Feature OKR Progression",          # renamed -> (current/target)
    f"{CHART_PREFIX}Models OKR Progression (current/target)",  # -> Feature Views KPI
    f"{CHART_PREFIX}Features vs Target (stacked)",      # renamed -> Feature Counts (stacked)
    f"{CHART_PREFIX}Feature Views vs Target (stacked)",  # renamed -> Features used in Models
    f"{CHART_PREFIX}Feature Views OKR Progression (current/target)",   # removed
    f"{CHART_PREFIX}Model Deployments OKR Progression (current/target)",   # removed
    f"{CHART_PREFIX}Agent Deployments OKR Progression (current/target)",   # removed
    f"{CHART_PREFIX}Overall Progress",   # removed
    f"{CHART_PREFIX}OKRs On Track",   # removed
    f"{CHART_PREFIX}Active Feature OKR Progression (current/target)",  # -> Active Feature Count
    f"{CHART_PREFIX}Feature OKR Progression (current/target)",  # -> Prod Feature Count
    f"{CHART_PREFIX}Feature Counts (stacked)",  # -> Feature Count Details
    f"{CHART_PREFIX}Features used in Models (feature views)",  # earlier rename
    f"{CHART_PREFIX}Features used in Models",  # removed
    f"{CHART_PREFIX}Pipeline Lifecycle Funnel — Feature Groups",  # -> for Features
]

# Live MySQL actual for each OKR metric: a scalar SQL expression counting the
# real Hopsworks metadata rows. Keyed by the metric name stored in the `okrs`
# FG. A metric with no entry here falls back to the literal 0, so an OKR can
# carry a target without a table behind it.
#
# "agent deployments" is one of those: it used to count hopsworks.agent, but
# that table was dropped by hopsworks-ee migration V82 ([HWORKS-2789], remove
# brewer) and no longer exists in the schema, so the query failed with
# "Table 'hopsworks.agent' doesn't exist". Adding the table to the read-only
# grant list is not the fix — a GRANT on a table that does not exist fails too,
# and it would fail after the REVOKE that precedes it, leaving the read-only
# user with no SELECT grants at all. Restore an entry here only against a table
# that exists and is in $roTables in hopsworks-helm's grants.sql.template.
ACTUAL_SQL = {
    "features": ("(SELECT COUNT(*) FROM hopsworks.cached_feature)"
                 " + (SELECT COUNT(*) FROM hopsworks.on_demand_feature)"
                 " + (SELECT COUNT(*) FROM hopsworks.embedding_feature)"),
    "models": "(SELECT COUNT(*) FROM hopsworks.model)",
    "model deployments": "(SELECT COUNT(*) FROM hopsworks.serving)",
    "dashboards": "(SELECT COUNT(*) FROM hopsworks.dashboard)",
    "apps": "(SELECT COUNT(*) FROM hopsworks.jobs WHERE type = 'PYTHON_APP')",
}

# The `asset` tag carries one lifecycle value per feature group in
# its `status` field (enum: deprecated / prod / rnd / uat), e.g.
# {"status":"prod"}. fg_status is NULL for feature groups with no such tag.
# Joined by tag name (not a hard-coded schema id) so it survives re-registration.
FG_STATUS_SQL = (
    "SELECT tv.feature_group_id,"
    " JSON_UNQUOTE(JSON_EXTRACT(tv.value, '$.status')) AS fg_status"
    " FROM hopsworks.feature_store_tag_value tv"
    " JOIN hopsworks.feature_store_tag t"
    "   ON t.id = tv.schema_id AND t.name = 'asset'"
    " WHERE tv.feature_group_id IS NOT NULL"
)

# One row per feature, carrying its feature group's `asset` value
# (fg_status). Drives the feature KPI panels by COUNTing rows filtered on
# fg_status: 'prod' for the prod-feature KPI, "not deprecated" for the
# active-feature KPI.
#
# IMPORTANT: the per-feature link columns hold the *subtype* id, not
# feature_group.id — cached_feature.cached_feature_group_id matches
# feature_group.cached_feature_group_id (and likewise stream_feature_group_id),
# on_demand_feature.on_demand_feature_group_id matches
# feature_group.on_demand_feature_group_id. We resolve each feature to its
# feature_group.id (fg_id) through those subtype links, then LEFT JOIN the tag
# value on fg_id. (Tags live on feature_group.id, so joining the subtype id
# directly silently matches nothing.)
FEATURE_STATUS_DATASET = "feature_okr_status"
FEATURE_STATUS_SQL = f"""SELECT feature_id, feature_kind, s.fg_status FROM (
    SELECT cf.id AS feature_id, 'cached' AS feature_kind, fg.id AS fg_id
    FROM hopsworks.cached_feature cf
    JOIN hopsworks.feature_group fg
      ON (cf.cached_feature_group_id IS NOT NULL
          AND fg.cached_feature_group_id = cf.cached_feature_group_id)
      OR (cf.stream_feature_group_id IS NOT NULL
          AND fg.stream_feature_group_id = cf.stream_feature_group_id)
    UNION ALL
    SELECT odf.id, 'on_demand', fg.id
    FROM hopsworks.on_demand_feature odf
    JOIN hopsworks.feature_group fg
      ON fg.on_demand_feature_group_id = odf.on_demand_feature_group_id
    UNION ALL
    SELECT ef.id, 'embedding', e.feature_group_id
    FROM hopsworks.embedding_feature ef
    JOIN hopsworks.embedding e ON e.id = ef.embedding_id
) feats
LEFT JOIN ({FG_STATUS_SQL}) s ON s.feature_group_id = feats.fg_id"""


# One row per feature view + its own asset tag value (fv_status,
# NULL when the FV carries no such tag). The tag can be attached to a feature
# view via feature_store_tag_value.feature_view_id, mirroring the FG case.
FV_STATUS_SQL = """SELECT fv.id AS fv_id, s.fv_status
FROM hopsworks.feature_view fv
LEFT JOIN (
    SELECT tv.feature_view_id,
           JSON_UNQUOTE(JSON_EXTRACT(tv.value, '$.status')) AS fv_status
    FROM hopsworks.feature_store_tag_value tv
    JOIN hopsworks.feature_store_tag t
      ON t.id = tv.schema_id AND t.name = 'asset'
    WHERE tv.feature_view_id IS NOT NULL
) s ON s.feature_view_id = fv.id"""
# Backs the "Production Model Progression" KPI: COUNT(*) WHERE fv_status='prod'
# is the number of feature views tagged asset='prod'.
FV_STATUS_DATASET = "fv_status"

# One row per FEATURE that appears in a feature view, carrying that feature
# view's asset value (fv_status). training_dataset_feature lists
# each feature column of a feature view (feature_view_id set); we attach the
# view's status. Used to count features-in-feature-views by status (not the
# number of feature views).
FV_FEATURE_STATUS_SQL = f"""SELECT tdf.id AS feature_id, fvs.fv_status
FROM hopsworks.training_dataset_feature tdf
JOIN ({FV_STATUS_SQL}) fvs ON fvs.fv_id = tdf.feature_view_id
WHERE tdf.feature_view_id IS NOT NULL"""

# Prod-tagged feature views: the set of feature_view ids carrying the `asset`
# tag with status='prod'. Reused below for both the reuse count and the
# reuse-percentage series.
_PROD_FV_IDS = """
        SELECT tv.feature_view_id FROM hopsworks.feature_store_tag_value tv
        JOIN hopsworks.feature_store_tag t
          ON t.id = tv.schema_id AND t.name = 'asset'
        WHERE tv.feature_view_id IS NOT NULL
          AND JSON_UNQUOTE(JSON_EXTRACT(tv.value, '$.status')) = 'prod'""".strip()

# Total number of features in the feature store (the percentage denominator),
# matching the `features` OKR actual: cached + on-demand + embedding features.
_TOTAL_FEATURES = ("(SELECT COUNT(*) FROM hopsworks.cached_feature)"
                   " + (SELECT COUNT(*) FROM hopsworks.on_demand_feature)"
                   " + (SELECT COUNT(*) FROM hopsworks.embedding_feature)")

# Time series of feature reuse in feature views tagged asset='prod', binned by
# feature_view.created. Columns:
#   day               — the FV creation day.
#   cumulative_count  — running total of feature usages (feature × prod FV pairs)
#                       up to that day; backs the "Feature reuse count
#                       (cumulative)" line.
#   reuse_pct         — fraction (0-1) of ALL features in the store that have
#                       been reused in a prod FV as of that day. Numerator is the
#                       running count of DISTINCT reused features (a feature is a
#                       (name, feature_group) pair, counted on the first day it
#                       appears in any prod FV); denominator is the current total
#                       feature count. Backs the secondary-axis percentage line.
FEATURE_REUSE_DATASET = "feature_reuse_daily"
FEATURE_REUSE_SQL = f"""SELECT d.day,
       SUM(d.daily_count) OVER (ORDER BY d.day) AS cumulative_count,
       SUM(COALESCE(nf.new_features, 0)) OVER (ORDER BY d.day)
         / NULLIF({_TOTAL_FEATURES}, 0) AS reuse_pct
FROM (
    SELECT DATE(fv.created) AS day, COUNT(*) AS daily_count
    FROM hopsworks.training_dataset_feature tdf
    JOIN hopsworks.feature_view fv ON fv.id = tdf.feature_view_id
    WHERE tdf.feature_view_id IS NOT NULL
      AND fv.id IN ({_PROD_FV_IDS})
    GROUP BY DATE(fv.created)
) d
LEFT JOIN (
    SELECT first_day, COUNT(*) AS new_features FROM (
        SELECT tdf.name, tdf.feature_group, MIN(DATE(fv.created)) AS first_day
        FROM hopsworks.training_dataset_feature tdf
        JOIN hopsworks.feature_view fv ON fv.id = tdf.feature_view_id
        WHERE tdf.feature_view_id IS NOT NULL
          AND fv.id IN ({_PROD_FV_IDS})
        GROUP BY tdf.name, tdf.feature_group
    ) ff
    GROUP BY first_day
) nf ON nf.first_day = d.day
ORDER BY d.day"""

# Feature counts grouped by their feature group's asset `status`, arranged in
# lifecycle order untagged -> rnd -> uat -> qa -> prod for a funnel chart. Stage
# labels are numbered so the funnel keeps this exact order (funnels otherwise
# re-sort by count); 'qa' is included even though it is not in the asset enum
# (shows 0 until used), and 'deprecated' is intentionally excluded.
FG_FUNNEL_DATASET = "fg_lifecycle_funnel"
FG_FUNNEL_STAGES = [
    ("untagged", "fs.fg_status IS NULL"),
    ("rnd", "fs.fg_status = 'rnd'"),
    ("uat", "fs.fg_status = 'uat'"),
    ("qa", "fs.fg_status = 'qa'"),
    ("prod", "fs.fg_status = 'prod'"),
]


def build_fg_funnel_sql():
    rows = []
    for i, (name, cond) in enumerate(FG_FUNNEL_STAGES, 1):
        cnt = f"(SELECT COUNT(*) FROM ({FEATURE_STATUS_SQL}) fs WHERE {cond})"
        rows.append(f"    SELECT {i} AS sort_order, '{i}. {name}' AS stage, "
                    f"{cnt} AS cnt")
    union = "\n    UNION ALL\n".join(rows)
    return f"SELECT sort_order, stage, cnt FROM (\n{union}\n) t ORDER BY sort_order"


# Feature popularity: how many distinct feature views each feature is used in.
# A feature is identified by (name, source feature group) so identically named
# features from different FGs stay separate; the label carries both. fv_count is
# an integer (COUNT DISTINCT feature views). Backs the "Most Popular Features"
# top-20 bar chart.
FEATURE_POPULARITY_DATASET = "feature_popularity"
FEATURE_POPULARITY_SQL = """SELECT feature, fv_count FROM (
    SELECT CONCAT(tdf.name, ' (', COALESCE(fg.name, '?'), ')') AS feature,
           COUNT(DISTINCT tdf.feature_view_id) AS fv_count
    FROM hopsworks.training_dataset_feature tdf
    LEFT JOIN hopsworks.feature_group fg ON fg.id = tdf.feature_group
    WHERE tdf.feature_view_id IS NOT NULL
    GROUP BY tdf.name, fg.name
) t ORDER BY fv_count DESC"""

# Model time-to-market velocity: per model version, the number of days between
# its source feature view being created (fv.created — data first made available)
# and the model version being created (mv.created — model delivered). A model
# version links to its feature view(s) via model_link.parent_feature_view_name /
# _version; a version can reference more than one FV, so we take the EARLIEST
# (MIN) feature-view creation as the start of the clock and aggregate to one row
# per model version. ttm_days backs the TTM Velocity histogram. Negative spans
# (model older than its FV — clock skew / re-registration) are dropped.
MODEL_TTM_DATASET = "model_ttm_velocity"
MODEL_TTM_SQL = """SELECT model_name, model_version, model_created, fv_created,
       ttm_days
FROM (
    SELECT m.name AS model_name,
           mv.version AS model_version,
           mv.created AS model_created,
           MIN(fv.created) AS fv_created,
           DATEDIFF(mv.created, MIN(fv.created)) AS ttm_days
    FROM hopsworks.model_version mv
    JOIN hopsworks.model m ON m.id = mv.model_id
    JOIN hopsworks.model_link ml ON ml.model_version_id = mv.id
    JOIN hopsworks.feature_view fv
          ON fv.name = ml.parent_feature_view_name
         AND fv.version = ml.parent_feature_view_version
    WHERE mv.created IS NOT NULL
    GROUP BY mv.id, m.name, mv.version, mv.created
) t
WHERE fv_created IS NOT NULL AND ttm_days >= 0
ORDER BY ttm_days"""

# Dataset feeding the stacked bar charts. For each population (features, Feature
# Views) there are two x-axis bars:
#   bar='actual' — split into non-overlapping segments by asset
#                  value (deprecated / prod / rnd / uat / untagged, where
#                  untagged = no such tag). These sum to the live total.
#   bar='target' — a single separate bar holding that population's OKR target
#                  (features OKR target for features; models OKR target for
#                  Feature Views).
# So the actual breakdown and the target stand side by side rather than the
# target being a synthetic segment of the actual bar. Counts are live scalar
# subqueries; targets are embedded at build time. The deprecated segment is
# hidden by default via a native filter.
FEATURE_STACK_DATASET = "feature_target_stack"
STATUS_ENUM = ["deprecated", "prod", "rnd", "uat"]


def build_feature_stack_sql(feat_target, fv_target):
    """Per population: an 'actual' bar stacked by asset value, plus
    a separate 'target' bar."""
    def rows(status_sql, alias, metric, target):
        def cnt(cond):
            return f"(SELECT COUNT(*) FROM ({status_sql}) {alias} WHERE {cond})"
        col = f"{alias}.{'fg_status' if alias == 'fs' else 'fv_status'}"
        out = [(metric, "actual", s, cnt(f"{col} = '{s}'")) for s in STATUS_ENUM]
        # untagged: no asset tag at all (LEFT JOIN leaves it NULL).
        out.append((metric, "actual", "untagged", cnt(f"{col} IS NULL")))
        out.append((metric, "target", "target", str(int(target))))
        return out

    all_rows = (rows(FEATURE_STATUS_SQL, "fs", "features", feat_target)
                + rows(FV_FEATURE_STATUS_SQL, "vs", "Feature Views", fv_target))
    union = "\n    UNION ALL\n".join(
        f"    SELECT '{metric}' AS metric, '{bar}' AS bar,"
        f" '{seg}' AS segment, ({val}) AS value"
        for metric, bar, seg, val in all_rows)
    return f"SELECT metric, bar, segment, value FROM (\n{union}\n) s"


# Superset names the analytics connection "<connector>__<superset user>", where <connector> matches
# HopsworksAnalyticsController.RO_CONNECTOR_NAME in hopsworks-ee.
ANALYTICS_CONNECTION = "hopsworks_analytics"


def find_mysql_db_id(api):
    """The analytics connection, which the backend names '<connector>__<superset user>'.

    Selecting on the mysql backend alone is not enough: a project with the online feature store also has a
    MySQL connection, so the first match can silently be the wrong database and every chart then reads it.
    """
    mysql_dbs = [db for db in api.list_databases()["result"]
                 if (db.get("backend") or "").lower() == "mysql"]
    for db in mysql_dbs:
        if (db.get("database_name") or "").startswith(ANALYTICS_CONNECTION):
            return db["id"], db.get("database_name")
    raise RuntimeError(
        f"No Superset connection named {ANALYTICS_CONNECTION}* found. "
        f"MySQL connections present: {[db.get('database_name') for db in mysql_dbs]}"
    )


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


def build_dup_features_counts_sql(project):
    """feature_name -> times-suspected-as-duplicate, embedded as a literal SELECT.

    Read the `suspected_duplicate_features` feature group via the Hopsworks
    Feature Query Service (reliable in-process) and embed the per-feature counts.
    The FG's offline DELTA table is NOT reliably queryable through Superset's
    Trino connection — Trino caches the table's split/file manifest for the
    reused path and keeps referencing parquet files that each rewrite deletes
    (COUNT works off delta stats, but a GROUP BY scan hits the dead file). So we
    aggregate here at build time; re-run this builder to refresh the chart after
    the nightly detection job updates the FG. Only features actually flagged
    appear, so features with zero suspected duplicates are naturally excluded.
    """
    fs = project.get_feature_store()
    counts = {}
    try:
        df = fs.get_feature_group(DUP_FEATURES_FG, version=1) \
               .select(["feature_name"]).read()
        for name, c in df["feature_name"].value_counts().items():
            label = "" if name is None else str(name)
            if label:
                counts[label] = int(c)
    except Exception as e:
        print(f"  ! could not read '{DUP_FEATURES_FG}' ({e}); chart left empty.")

    if not counts:
        return ("SELECT CAST(NULL AS CHAR) AS feature_name, "
                "0 AS times_suspected WHERE 1 = 0")

    def esc(s):
        return "'" + s.replace("'", "''") + "'"

    rows = [f"    SELECT {esc(name)} AS feature_name, {c} AS times_suspected"
            for name, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
    union = "\n    UNION ALL\n".join(rows)
    return (f"SELECT feature_name, times_suspected FROM (\n{union}\n) t "
            "WHERE times_suspected > 0 ORDER BY times_suspected DESC")


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


def sql_filter(expr):
    """A single ad-hoc WHERE filter from a raw SQL boolean expression."""
    return [{"expressionType": "SQL", "sqlExpression": expr, "clause": "WHERE"}]


def _tbl_metric(sql, label, opt):
    return {"expressionType": "SQL", "sqlExpression": sql, "label": label,
            "optionName": opt, "hasCustomLabel": True}


def attainment_table(slice_name, current_sql, target_sql, filters, width=3, height=40):
    """A one-row KPI table: Current | Target | % attainment, all live.

    % attainment is current/target as a fraction, rendered as a percent by
    column_config. current_sql/target_sql are aggregate SQL expressions evaluated
    over the (filtered) dataset, so every cell refreshes with the live data.
    """
    pct_sql = f"({current_sql}) / NULLIF(({target_sql}), 0)"
    params = {
        "viz_type": "table", "query_mode": "aggregate", "groupby": [],
        "metrics": [
            _tbl_metric(current_sql, "Current", "m_current"),
            _tbl_metric(target_sql, "Target", "m_target"),
            _tbl_metric(pct_sql, "% attainment", "m_pct"),
        ],
        "adhoc_filters": filters, "row_limit": 1, "show_cell_bars": False,
        "column_config": {
            "Current": {"d3NumberFormat": ",d"},
            "Target": {"d3NumberFormat": ",d"},
            "% attainment": {"d3NumberFormat": ".1%"},
        },
        "conditional_formatting": [
            {"column": "% attainment", "operator": ">", "targetValue": 0,
             "colorScheme": "#5AC189"},
        ],
        "table_timestamp_format": "smart_date",
    }
    return (slice_name, "table", params, width, height)


# Stacked-bar metric: total of the segment values for each (bucket, segment).
SUM_VALUE = {
    "expressionType": "SQL", "sqlExpression": "SUM(value)",
    "label": "features", "optionName": "metric_value", "hasCustomLabel": True,
}
ACTIVE_FEATURE_CHART = f"{CHART_PREFIX}Active Feature Count"
PROD_FEATURE_CHART = f"{CHART_PREFIX}Prod Feature Count"
PROD_MODEL_CHART = f"{CHART_PREFIX}Production Model Progression"
FEATURE_STACK_CHART = f"{CHART_PREFIX}Feature Count Details"
FEATURE_REUSE_CHART = f"{CHART_PREFIX}Historical Feature Reuse"
FG_FUNNEL_CHART = f"{CHART_PREFIX}Pipeline Lifecycle Funnel for Features"
MOST_POPULAR_CHART = f"{CHART_PREFIX}Most Popular Features"
MODEL_TTM_CHART = f"{CHART_PREFIX}Model Time-to-Market (TTM) Velocity"
DUP_FEATURES_CHART = f"{CHART_PREFIX}Suspected Duplicate Features"

# The feature group the nightly duplicate-detection job (de)populates.
DUP_FEATURES_FG = "suspected_duplicate_features"
DUP_FEATURES_DATASET = "suspected_duplicate_feature_counts"


def chart_specs(targets):
    """(slice_name, viz_type, params, width, height) list for the dashboard."""
    specs = []

    # Two feature KPI tables, both backed by the feature-grain feature_okr_status
    #     dataset (handled in main) and counting features against the features OKR
    #     target. Each shows Current | Target | % attainment (all live). They differ
    #     only in the fg_status filter on each feature's asset value:
    #       a) Active  — features in FGs NOT tagged 'deprecated' (incl. untagged).
    #       b) Feature — features in FGs tagged 'prod'.
    #     (a) is listed first so it leads the KPI panels.
    feat_target = int(targets.get("features", 0))
    specs.append(attainment_table(
        ACTIVE_FEATURE_CHART, "COUNT(*)", str(feat_target),
        sql_filter("COALESCE(fg_status, '') <> 'deprecated'")))
    specs.append(attainment_table(
        PROD_FEATURE_CHART, "COUNT(*)", str(feat_target),
        sql_filter("fg_status = 'prod'")))

    # Position 3: Production Model Progression — number of feature views tagged
    #     asset='prod', against the models OKR target. From the fv_status dataset.
    specs.append(attainment_table(
        PROD_MODEL_CHART, "COUNT(*)", str(int(targets.get("models", 0))),
        sql_filter("fv_status = 'prod'")))

    # 3c. Two separate stacked charts (kept apart so the very different target
    #     scales — features vs feature views — stay readable). Each shows an
    #     'actual' bar split into non-overlapping asset segments
    #     (deprecated / prod / rnd / uat / untagged). When target_line is given the
    #     'target' bar is dropped and the target is drawn as a dotted reference
    #     line instead; otherwise a separate 'target' bar is shown.
    def stacked_bar(slice_name, metric_value, target_line=None):
        filt = f"metric = '{metric_value}'"
        params = {
            "viz_type": "echarts_timeseries_bar", "x_axis": "bar",
            "x_axis_force_categorical": True, "metrics": [SUM_VALUE],
            "groupby": ["segment"], "stack": "Stack",
            "row_limit": 100, "orientation": "vertical", "show_legend": True,
            "show_value": True, "truncateYAxis": False,
            "sort_series_type": "name", "sort_series_ascending": True,
            "y_axis_format": "SMART_NUMBER",
        }
        if target_line is not None:
            # Drop the 'target' bar; show the target as a dotted horizontal line.
            filt += " AND bar = 'actual'"
            params["annotation_layers"] = [{
                "name": "Target", "annotationType": "FORMULA",
                "value": str(int(target_line)), "style": "dotted",
                "width": 2, "opacity": "", "color": None, "sourceType": "",
                "show": True, "showLabel": True, "showMarkers": False,
                "hideLine": False,
            }]
        params["adhoc_filters"] = sql_filter(filt)
        return (slice_name, "echarts_timeseries_bar", params, 6, 50)

    specs.append(stacked_bar(FEATURE_STACK_CHART, "features", target_line=feat_target))

    # 3e. Historical Feature Reuse: time series over feature_view.created for
    #     prod-tagged feature views, from the feature_reuse_daily dataset (one row
    #     per day, values precomputed). A MIXED chart with two y-axes:
    #       primary   — "Feature reuse count (cumulative)" (running usage total).
    #       secondary — "Percentage of features that are reused" (reused / total),
    #                   query B with yAxisIndexB=1 and a percent (.1%) format.
    #     No time grain so the precomputed daily values are plotted as-is.
    def reuse_metric(col, label, opt):
        return {"expressionType": "SQL", "sqlExpression": f"SUM({col})",
                "label": label, "optionName": opt, "hasCustomLabel": True}

    specs.append((
        FEATURE_REUSE_CHART, "mixed_timeseries",
        {"viz_type": "mixed_timeseries", "x_axis": "day", "time_grain_sqla": None,
         # Query A — primary axis: cumulative reuse count.
         "metrics": [reuse_metric("cumulative_count",
                                  "Feature reuse count (cumulative)", "m_cum")],
         "groupby": [], "adhoc_filters": [], "yAxisIndex": 0,
         # Query B — secondary axis: percentage of features reused.
         "metrics_b": [reuse_metric("reuse_pct",
                                    "Percentage of features that are reused",
                                    "m_pct")],
         "groupby_b": [], "adhoc_filters_b": [], "yAxisIndexB": 1,
         "row_limit": 10000, "show_legend": True, "markerEnabled": True,
         "x_axis_title": "day",
         "y_axis_format": "SMART_NUMBER", "y_axis_title": "feature reuse count",
         "y_axis_format_secondary": ".1%",
         "y_axis_title_secondary": "% of features reused"},
        12, 50,
    ))

    # 3f. Pipeline Lifecycle "funnel" (feature groups): feature counts by asset
    #     status, as a HORIZONTAL bar with categories pinned in lifecycle order
    #     untagged -> rnd -> uat -> qa -> prod. Bars are ordered by the numeric
    #     sort_order (order_desc off + orderby on MIN(sort_order)), not by size;
    #     the numbered stage labels keep the order unambiguous. From
    #     fg_lifecycle_funnel.
    specs.append((
        FG_FUNNEL_CHART, "echarts_timeseries_bar",
        {"viz_type": "echarts_timeseries_bar", "x_axis": "stage",
         "x_axis_force_categorical": True,
         "metrics": [{"expressionType": "SQL", "sqlExpression": "SUM(cnt)",
                      "label": "features", "optionName": "m_funnel",
                      "hasCustomLabel": True}],
         "groupby": [], "adhoc_filters": [], "orientation": "horizontal",
         "row_limit": 10, "order_desc": False,
         "orderby": [[{"expressionType": "SQL", "sqlExpression": "MIN(sort_order)",
                       "label": "stage_order", "hasCustomLabel": True}, False]],
         "x_axis_sort": "stage", "x_axis_sort_asc": False,
         "show_legend": False, "show_value": True, "truncateYAxis": False,
         "y_axis_format": ",d"},
        6, 50,
    ))

    # 3g. Most Popular Features: top-20 features by the number of distinct feature
    #     views they are used in. Horizontal bar, top 20 sorted by count desc
    #     (orderby + x_axis_sort on the metric, the viz ignores dataset order),
    #     whole-number value labels. From feature_popularity.
    specs.append((
        MOST_POPULAR_CHART, "echarts_timeseries_bar",
        {"viz_type": "echarts_timeseries_bar", "x_axis": "feature",
         "x_axis_force_categorical": True,
         "metrics": [{"expressionType": "SQL",
                      "sqlExpression": "SUM(fv_count)", "label": "feature views",
                      "optionName": "m_fv_count", "hasCustomLabel": True}],
         "groupby": [], "adhoc_filters": [], "orientation": "horizontal",
         "row_limit": 20, "order_desc": True,
         "orderby": [[{"expressionType": "SQL", "sqlExpression": "SUM(fv_count)",
                       "label": "feature views", "hasCustomLabel": True}, False]],
         "x_axis_sort": "feature views", "x_axis_sort_asc": False,
         "show_legend": False,
         "show_value": True, "truncateYAxis": False, "y_axis_format": ",d"},
        12, 80,
    ))

    # 3h. Model Time-to-Market (TTM) Velocity: distribution of the number of days
    #     between a model version's source feature view being created and the model
    #     version itself being created. Histogram over ttm_days from the
    #     model_ttm_velocity dataset (one row per model version).
    specs.append((
        MODEL_TTM_CHART, "histogram_v2",
        {"viz_type": "histogram_v2", "column": "ttm_days", "groupby": [],
         "bins": 20, "row_limit": 50000, "normalize": False, "cumulative": False,
         "adhoc_filters": [],
         "x_axis_title": "Time from when a feature view was created until it "
                         "was tagged as `prod`",
         "y_axis_title": "# model versions",
         "x_axis_format": "SMART_NUMBER", "y_axis_format": "SMART_NUMBER"},
        12, 55,
    ))

    # 3i. Suspected Duplicate Features: per feature name, how many times it was
    #     flagged as a suspected duplicate by the nightly detection job. Counts
    #     are embedded from the suspected_duplicate_features FG at build time (see
    #     build_dup_features_counts_sql). Horizontal bar sorted by count desc;
    #     only flagged features appear (zero-count features are excluded).
    specs.append((
        DUP_FEATURES_CHART, "echarts_timeseries_bar",
        {"viz_type": "echarts_timeseries_bar", "x_axis": "feature_name",
         "x_axis_force_categorical": True,
         "metrics": [{"expressionType": "SQL",
                      "sqlExpression": "SUM(times_suspected)",
                      "label": "times suspected as duplicate",
                      "optionName": "m_dup", "hasCustomLabel": True}],
         "groupby": [], "adhoc_filters": [], "orientation": "horizontal",
         "row_limit": 100, "order_desc": True,
         "orderby": [[{"expressionType": "SQL",
                       "sqlExpression": "SUM(times_suspected)",
                       "label": "times suspected as duplicate",
                       "hasCustomLabel": True}, False]],
         # Horizontal bar: ECharts draws the first category at the BOTTOM, so to
         # show the most-suspected feature at the TOP we sort the category axis
         # ASCENDING by the metric (smallest -> bottom, largest -> top).
         "x_axis_sort": "times suspected as duplicate", "x_axis_sort_asc": True,
         "show_legend": False, "show_value": True, "truncateYAxis": False,
         "y_axis_format": ",d", "x_axis_title": "",
         "y_axis_title": "# times suspected as duplicate"},
        12, 70,
    ))

    # 4. Detail table.
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


def stack_segment_filter_metadata(ds_id, stack_chart_ids, all_chart_ids):
    """Native multi-select on the stacked-bar 'segment', scoped to the two
    stacked charts (features + feature views) only.

    Defaults to every segment EXCEPT 'deprecated', so deprecated counts are off by
    default in both charts; the user can add it back from the filter. Every other
    chart is excluded from the filter's scope so the rest of the dashboard is
    unaffected.
    """
    keep = set(stack_chart_ids)
    excluded = [cid for cid in all_chart_ids if cid not in keep]
    # All segment values except 'deprecated' (the default selection). 'target' is
    # the separate target bar's series and stays visible by default.
    default_vals = [s for s in STATUS_ENUM if s != "deprecated"] + ["untagged", "target"]
    return json.dumps({
        "native_filter_configuration": [{
            "id": "NATIVE_FILTER-stack_segment",
            "name": "Status segments (deprecated off by default)",
            "filterType": "filter_select", "type": "NATIVE_FILTER",
            "targets": [{"datasetId": ds_id, "column": {"name": "segment"}}],
            "controlValues": {"multiSelect": True, "enableEmptyFilter": False,
                              "defaultToFirstItem": False, "inverseSelection": False,
                              "searchAllOptions": False},
            "scope": {"rootPath": ["ROOT_ID"], "excluded": excluded},
            "defaultDataMask": {
                "filterState": {"value": default_vals},
                "extraFormData": {"filters": [
                    {"col": "segment", "op": "IN", "val": default_vals}]},
            },
            "cascadeParentIds": [],
        }],
        "cross_filters_enabled": False,
    })


def ensure_dashboard(api, title, charts, json_metadata=None):
    position_json = build_position_json(charts, title)
    dash_id = next((d["id"] for d in list_all(api, "dashboard")
                    if d.get("dashboard_title") == title), None)
    kwargs = {"dashboard_title": title, "published": True,
              "position_json": position_json}
    if json_metadata is not None:
        kwargs["json_metadata"] = json_metadata
    if dash_id is None:
        dash_id = api.create_dashboard(**kwargs)["id"]
        print(f"Created dashboard id={dash_id}")
    else:
        api.update_dashboard(dash_id, **kwargs)
        print(f"Updated dashboard id={dash_id}")
    for ch in charts:
        api.update_chart(ch["id"], dashboards=[dash_id])
    return dash_id


def main():
    project = hopsworks.login()
    api = project.get_superset_api()

    db_id, db_name = find_mysql_db_id(api)
    print(f"hopsworks_analytics connection: id={db_id} ({db_name})\n")

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

    # Feature-grain dataset (one row per feature + its asset value)
    # backing the Active / Feature OKR Progression KPI panels.
    feat_ds_id = ensure_dataset(api, db_id, FEATURE_STATUS_DATASET, FEATURE_STATUS_SQL)
    print(f"Dataset '{FEATURE_STATUS_DATASET}' ready (id={feat_ds_id}).")

    # Stacked dataset: features (vs features target) + Feature Views (vs models
    # target), each split by asset with a gap-to-target segment.
    feat_target = int(targets.get("features", 0))
    fv_target = int(targets.get("models", 0))
    stack_ds_id = ensure_dataset(
        api, db_id, FEATURE_STACK_DATASET,
        build_feature_stack_sql(feat_target, fv_target))
    print(f"Dataset '{FEATURE_STACK_DATASET}' ready (id={stack_ds_id}).")

    # Daily feature-reuse time series (features added to prod-tagged feature views).
    reuse_ds_id = ensure_dataset(
        api, db_id, FEATURE_REUSE_DATASET, FEATURE_REUSE_SQL)
    print(f"Dataset '{FEATURE_REUSE_DATASET}' ready (id={reuse_ds_id}).")

    # Feature-group lifecycle funnel dataset (feature counts by asset status).
    fg_funnel_ds_id = ensure_dataset(
        api, db_id, FG_FUNNEL_DATASET, build_fg_funnel_sql())
    print(f"Dataset '{FG_FUNNEL_DATASET}' ready (id={fg_funnel_ds_id}).")

    # Feature popularity dataset (distinct feature-view usage per feature).
    popularity_ds_id = ensure_dataset(
        api, db_id, FEATURE_POPULARITY_DATASET, FEATURE_POPULARITY_SQL)
    print(f"Dataset '{FEATURE_POPULARITY_DATASET}' ready (id={popularity_ds_id}).")

    # Feature-view status dataset (one row per FV + its asset status).
    fv_status_ds_id = ensure_dataset(api, db_id, FV_STATUS_DATASET, FV_STATUS_SQL)
    print(f"Dataset '{FV_STATUS_DATASET}' ready (id={fv_status_ds_id}).")

    # Model time-to-market dataset (days from FV created to model created).
    ttm_ds_id = ensure_dataset(api, db_id, MODEL_TTM_DATASET, MODEL_TTM_SQL)
    print(f"Dataset '{MODEL_TTM_DATASET}' ready (id={ttm_ds_id}).")

    # Suspected-duplicate-feature counts, embedded from the FG (read in-process).
    dup_ds_id = ensure_dataset(api, db_id, DUP_FEATURES_DATASET,
                               build_dup_features_counts_sql(project))
    print(f"Dataset '{DUP_FEATURES_DATASET}' ready (id={dup_ds_id}).")

    # Delete charts retired from the dashboard so they don't linger in Superset.
    # Covers the explicit RETIRED_CHARTS plus every per-OKR "— % to target" bar
    # (one per metric), which have all been removed from the dashboard.
    retired = set(RETIRED_CHARTS)
    for c in list_all(api, "chart"):
        name = c.get("slice_name") or ""
        if name in retired or (
                name.startswith(CHART_PREFIX) and name.endswith("— % to target")):
            api.delete_chart(c["id"])
            print(f"Deleted retired chart '{name}' (id={c['id']})")

    print("\nCreating charts:")
    charts, stack_chart_ids = [], []
    for slice_name, viz_type, params, width, height in chart_specs(targets):
        if slice_name == FEATURE_STACK_CHART:
            chart_ds = stack_ds_id
        elif slice_name == FEATURE_REUSE_CHART:         # daily reuse time series
            chart_ds = reuse_ds_id
        elif slice_name == FG_FUNNEL_CHART:             # FG lifecycle funnel
            chart_ds = fg_funnel_ds_id
        elif slice_name == MOST_POPULAR_CHART:          # top-20 popular features
            chart_ds = popularity_ds_id
        elif slice_name == MODEL_TTM_CHART:             # model TTM velocity histogram
            chart_ds = ttm_ds_id
        elif slice_name == DUP_FEATURES_CHART:          # suspected duplicate features
            chart_ds = dup_ds_id
        elif slice_name in (ACTIVE_FEATURE_CHART, PROD_FEATURE_CHART):  # feature KPIs
            chart_ds = feat_ds_id
        elif slice_name == PROD_MODEL_CHART:            # prod feature-view count
            chart_ds = fv_status_ds_id
        else:
            chart_ds = ds_id
        cid = replace_chart(api, slice_name, viz_type, chart_ds, params)
        if slice_name == FEATURE_STACK_CHART:
            stack_chart_ids.append(cid)
        charts.append({"id": cid, "name": slice_name,
                       "width": width, "height": height})
        print(f"  [{viz_type}] {slice_name} -> id={cid}")

    json_metadata = stack_segment_filter_metadata(
        stack_ds_id, stack_chart_ids, [c["id"] for c in charts])
    dash_id = ensure_dashboard(api, DASHBOARD_TITLE, charts, json_metadata)
    print(f"\nDashboard '{DASHBOARD_TITLE}' ready (id={dash_id}).")
    print(f"Open it: {host}/hopsworks-api/superset/superset/dashboard/{dash_id}/")


if __name__ == "__main__":
    main()
