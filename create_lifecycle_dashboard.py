"""Build the "Asset Lifecycle" Superset dashboard: dev -> qa -> prod, per asset kind.

The question this answers is "how does work move through our environments": how many assets of
each kind sit in dev, qa and prod right now, how long they take to get there, and which ones have
been stuck somewhere for too long.

It reads `hopsworks.tag_history` (HWORKS-2895), the append-only record of every change to a tag's
values. A lifecycle tag records one interval per state an asset passed through, so the dwell in
`qa` is a subtraction rather than a guess. `feature_store_tag_value` and its siblings cannot answer
this: a tag write is an upsert in place, so they know the current state and nothing about the
previous one.

**Asset kinds.** `tag_history` records six artifact types. Five map straight through. `DEPLOYMENT`
covers both model deployments and agent deployments, which are the same kind of row in `serving`
and are told apart by whether a model artifact is attached, so this splits them:

    serving.id -> serving_deployment.serving_id -> serving_model_artifact.serving_depl_id

A deployment with a model artifact is a model deployment; one without is an agent.

**Apps are absent, and cannot be added here.** Hopsworks apps are not a taggable artifact: there is
no tags sub-resource on `AppsResource`, no tag methods on the `App` entity, and no APP member of
`TagHistoryArtifactType`. Tagging apps is a backend change, not a dashboard one, so this dashboard
covers the five kinds that can carry a lifecycle tag and says so rather than quietly showing four
of the six the request asked for.

Requires a tag schema with "Archive tag history" turned on and a lifecycle field. Nothing is
recorded for a schema without the flag, so the script checks first and says so instead of building
empty charts.

Run:  python create_lifecycle_dashboard.py [--tag asset_lifecycle] [--field status]
"""

from __future__ import annotations

import argparse
import sys

import hopsworks

from superset import (
    ChartSpec,
    Superset,
    categorical_bar,
    simple_filter,
    sql_metric,
    sql_str,
)

DATASET = "asset_lifecycle_intervals"
DASHBOARD_TITLE = "Asset Lifecycle"

DEFAULT_TAG = "asset_lifecycle"
DEFAULT_FIELD = "status"

# The order states are meant to be traversed in. Used to sort charts so the columns read
# dev -> qa -> prod rather than alphabetically, and to decide what "promoted" means.
STAGE_ORDER = ["dev", "qa", "uat", "prod"]


def intervals_sql(tag_name: str, tag_key: str) -> str:
    """One row per (asset, state) interval, with the asset kind resolved.

    The window runs over ALL events and the OPENED filter is applied outside it. Filtering inside
    would hide every CLOSED row from `LEAD`, and then a detached tag or a deleted asset would read
    as still current with a dwell time growing against `NOW()` forever. A value change would still
    look right, which is what makes that mistake so easy to miss.
    """
    return f"""
SELECT
    e.artifact_id,
    e.asset_kind,
    e.asset_name,
    e.project_name,
    e.tag_value AS stage,
    e.added_on,
    e.removed_at,
    TIMESTAMPDIFF(SECOND, e.added_on, COALESCE(e.removed_at, NOW())) AS dwell_seconds,
    CASE WHEN e.removed_at IS NULL THEN 1 ELSE 0 END AS is_current
FROM (
    SELECT
        h.artifact_id,
        h.event_type,
        h.tag_value,
        h.project_name,
        h.event_time AS added_on,
        LEAD(h.event_time) OVER (
            PARTITION BY h.artifact_type, h.artifact_id, h.tag_name, h.tag_key
            ORDER BY h.event_time,
                     CASE WHEN h.event_type = 'CLOSED' THEN 0 ELSE 1 END,
                     h.id
        ) AS removed_at,
        CASE
            WHEN h.artifact_type = 'DEPLOYMENT' AND EXISTS (
                SELECT 1
                FROM serving_deployment sd
                JOIN serving_model_artifact sma ON sma.serving_depl_id = sd.id
                WHERE sd.serving_id = h.artifact_id
            ) THEN 'Model deployment'
            WHEN h.artifact_type = 'DEPLOYMENT' THEN 'Agent deployment'
            WHEN h.artifact_type = 'FEATURE_GROUP' THEN 'Feature group'
            WHEN h.artifact_type = 'FEATURE_VIEW' THEN 'Feature view'
            WHEN h.artifact_type = 'TRAINING_DATASET' THEN 'Training dataset'
            WHEN h.artifact_type = 'MODEL' THEN 'Model'
            WHEN h.artifact_type = 'JOB' THEN 'Job'
            ELSE h.artifact_type
        END AS asset_kind,
        -- Names are resolved per type and left NULL when the asset is gone: the history
        -- deliberately outlives what it describes, so a join that dropped those rows would
        -- silently under-report exactly the deletions the close-outs exist to record.
        CASE h.artifact_type
            WHEN 'FEATURE_GROUP' THEN (SELECT fg.name FROM feature_group fg WHERE fg.id = h.artifact_id)
            WHEN 'FEATURE_VIEW' THEN (SELECT fv.name FROM feature_view fv WHERE fv.id = h.artifact_id)
            WHEN 'TRAINING_DATASET' THEN (SELECT td.name FROM training_dataset td WHERE td.id = h.artifact_id)
            WHEN 'JOB' THEN (SELECT j.name FROM jobs j WHERE j.id = h.artifact_id)
            WHEN 'MODEL' THEN (
                SELECT m.name FROM model m
                JOIN model_version mv ON mv.model_id = m.id
                WHERE mv.id = h.artifact_id
            )
            WHEN 'DEPLOYMENT' THEN (SELECT s.name FROM serving s WHERE s.id = h.artifact_id)
        END AS asset_name
    FROM tag_history h
    WHERE h.tag_name = {sql_str(tag_name)}
      AND h.tag_key = {sql_str(tag_key)}
) e
WHERE e.event_type = 'OPENED'
""".strip()


def chart_specs() -> list[ChartSpec]:
    """The four questions a lifecycle dashboard is for."""
    current = simple_filter("is_current", "==", 1)
    completed = simple_filter("is_current", "==", 0)
    days = sql_metric("AVG(dwell_seconds) / 86400", "avg days in stage")
    assets = sql_metric("COUNT(DISTINCT artifact_id)", "assets")

    return [
        # Where everything is right now, split by kind. The headline: how much of the estate
        # has actually reached production.
        ChartSpec(
            name="Lifecycle · Assets in each stage now",
            viz_type="echarts_timeseries_bar",
            width=6,
            params=categorical_bar(
                x_axis="stage",
                series="asset_kind",
                metrics=[assets],
                adhoc_filters=[current],
            ),
        ),
        # How long a stage takes, per kind. Completed intervals only: mixing in the ones still
        # running would drag every average toward whatever is in flight right now.
        ChartSpec(
            name="Lifecycle · Average days in each stage",
            viz_type="echarts_timeseries_bar",
            width=6,
            params=categorical_bar(
                x_axis="stage",
                series="asset_kind",
                metrics=[days],
                adhoc_filters=[completed],
                y_axis_format=",.1f",
            ),
        ),
        # Is it getting faster or slower, by when the stage was entered.
        ChartSpec(
            name="Lifecycle · Time in stage, by week entered",
            viz_type="echarts_timeseries_line",
            width=12,
            params={
                "viz_type": "echarts_timeseries_line",
                "x_axis": "added_on",
                "time_grain_sqla": "P1W",
                "metrics": [days],
                "groupby": ["stage"],
                "adhoc_filters": [completed],
                "row_limit": 1000,
                "y_axis_format": ",.1f",
            },
        ),
        # What is stuck. Open intervals, longest first: the actionable end of the dashboard.
        ChartSpec(
            name="Lifecycle · Longest running current stages",
            viz_type="table",
            width=12,
            height=60,
            params={
                "viz_type": "table",
                "query_mode": "raw",
                "all_columns": [
                    "project_name",
                    "asset_kind",
                    "asset_name",
                    "artifact_id",
                    "stage",
                    "added_on",
                    "dwell_seconds",
                ],
                "order_by_cols": ['["dwell_seconds", false]'],
                "adhoc_filters": [current],
                "row_limit": 100,
            },
        ),
    ]


def report_coverage(superset: Superset, tag_name: str, tag_key: str) -> int:
    """Print what the history actually holds, and return the event count."""
    total = superset.scalar(
        f"SELECT COUNT(*) FROM tag_history WHERE tag_name = {sql_str(tag_name)}"
    )
    total = int(total or 0)
    print(f"tag_history holds {total} event(s) for tag {tag_name!r}.")
    if not total:
        print(
            f"\nNothing recorded for {tag_name!r}. Two things to check:\n"
            f"  1. The schema has 'Archive tag history' turned on (Settings -> Schematised tags).\n"
            f"     Recording starts when it is turned on; existing attachments are backfilled.\n"
            f"  2. The tag has a {tag_key!r} field and is attached to something."
        )
        return 0

    print("\nEvents by asset kind and stage:")
    rows = superset.sql(
        f"""
        SELECT h.artifact_type, h.tag_value, COUNT(*) AS events,
               COUNT(DISTINCT h.artifact_id) AS assets
        FROM tag_history h
        WHERE h.tag_name = {sql_str(tag_name)} AND h.tag_key = {sql_str(tag_key)}
          AND h.event_type = 'OPENED'
        GROUP BY h.artifact_type, h.tag_value
        ORDER BY h.artifact_type, h.tag_value
        """
    )
    for row in rows:
        print(
            f"  {row['artifact_type']:<18} {str(row['tag_value']):<8} "
            f"{row['events']:>4} events across {row['assets']:>3} asset(s)"
        )
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default=DEFAULT_TAG, help="lifecycle tag schema name")
    parser.add_argument("--field", default=DEFAULT_FIELD, help="field holding the stage")
    args = parser.parse_args()

    project = hopsworks.login()
    superset = Superset.connect(project.get_superset_api())
    print(f"hopsworks_analytics connection: id={superset.database_id} "
          f"({superset.database_name})\n")

    if not report_coverage(superset, args.tag, args.field):
        # Building charts over an empty history produces a dashboard that looks broken rather
        # than one that says "nothing has been recorded yet".
        return 1

    superset.build(
        dataset=DATASET,
        title=DASHBOARD_TITLE,
        statement=intervals_sql(args.tag, args.field),
        specs=chart_specs(),
        host=project.get_url() if hasattr(project, "get_url") else None,
    )
    print(
        "\nNote: Hopsworks apps are not covered. They are not a taggable artifact "
        "(no tags sub-resource on the apps API, no APP artifact type in tag_history), "
        "so a lifecycle tag cannot be attached to one."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
