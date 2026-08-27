"""Build the "Asset Promotion Time" Superset dashboard: how long dev -> uat -> prod takes.

This answers a different question from the Asset Lifecycle dashboard, and needs a different
derivation. That one measures *dwell*: how long an asset sits in one stage, one row per interval.
This one measures the *journey*: for each asset, the time between first entering one stage and
first entering the next, so `dev -> prod` is a single number per asset regardless of how many
stages or detours it passed through on the way.

The unit is therefore one row per (asset, transition), which is what lets a chart put `dev -> uat`
and `uat -> prod` side by side and average, max or histogram them.

**First entry, not last.** Stage entry is `MIN(event_time)` over the OPENED events for that value.
An asset demoted from prod back to qa and promoted again has entered prod twice; taking the later
one would report the round trip as the time to production, which is not what anyone means by it.

**Only forward journeys count.** A transition is recorded only when the later stage was entered
after the earlier one. A rollback would otherwise contribute a negative duration and drag every
average toward zero, silently.

Reads `hopsworks.tag_history`, so it needs a tag schema with archiving on. `asset_lifecycle` is
created that way by mount_hopsworks_db.py. A schema without it records nothing, and the script
says so rather than building empty charts.

Run:  python create_promotion_dashboard.py [--tag asset_lifecycle] [--field status]
                                           [--stages dev,uat,prod]
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

DATASET = "asset_promotion_times"
DASHBOARD_TITLE = "Asset Promotion Time"

DEFAULT_TAG = "asset_lifecycle"
DEFAULT_FIELD = "status"
DEFAULT_STAGES = "dev,uat,prod"


def transitions_sql(tag_name: str, tag_key: str, stages: list[str]) -> str:
    """One row per (asset, transition), with how long that step took.

    Both the consecutive steps and the end-to-end first-to-last step are emitted, because
    "how long does uat take" and "how long does it take to reach production" are different
    questions and a dashboard that answers only one of them invites the other to be
    derived wrongly by adding averages up.
    """
    entered = ",\n".join(
        f"        MIN(CASE WHEN e.stage = {sql_str(s)} THEN e.entered END) AS entered_{i}"
        for i, s in enumerate(stages)
    )

    # Consecutive steps, plus the whole journey when there are more than two stages.
    steps = [(i, i + 1) for i in range(len(stages) - 1)]
    if len(stages) > 2:
        steps.append((0, len(stages) - 1))

    legs = "\nUNION ALL\n".join(
        f"""SELECT a.artifact_type, a.artifact_id, a.asset_kind, a.asset_name, a.project_name,
       {sql_str(f'{stages[i]} -> {stages[j]}')} AS transition,
       a.entered_{i} AS started, a.entered_{j} AS reached,
       TIMESTAMPDIFF(SECOND, a.entered_{i}, a.entered_{j}) / 86400 AS days
FROM assets a
WHERE a.entered_{i} IS NOT NULL AND a.entered_{j} IS NOT NULL
  AND a.entered_{j} >= a.entered_{i}"""
        for i, j in steps
    )

    return f"""
WITH stage_entry AS (
    SELECT
        h.artifact_type,
        h.artifact_id,
        h.project_name,
        h.tag_value AS stage,
        -- First time this asset entered the stage. A later re-entry after a rollback would
        -- turn a round trip into "the time to production".
        MIN(h.event_time) AS entered
    FROM tag_history h
    WHERE h.tag_name = {sql_str(tag_name)}
      AND h.tag_key = {sql_str(tag_key)}
      AND h.event_type = 'OPENED'
      AND h.event_time IS NOT NULL
    GROUP BY h.artifact_type, h.artifact_id, h.project_name, h.tag_value
),
assets AS (
    SELECT
        e.artifact_type,
        e.artifact_id,
        MAX(e.project_name) AS project_name,
        CASE
            WHEN e.artifact_type = 'DEPLOYMENT' AND EXISTS (
                SELECT 1 FROM serving_deployment sd
                JOIN serving_model_artifact sma ON sma.serving_depl_id = sd.id
                WHERE sd.serving_id = e.artifact_id
            ) THEN 'Model deployment'
            WHEN e.artifact_type = 'DEPLOYMENT' THEN 'Agent deployment'
            WHEN e.artifact_type = 'FEATURE_GROUP' THEN 'Feature group'
            WHEN e.artifact_type = 'FEATURE_VIEW' THEN 'Feature view'
            WHEN e.artifact_type = 'TRAINING_DATASET' THEN 'Training dataset'
            WHEN e.artifact_type = 'MODEL' THEN 'Model'
            WHEN e.artifact_type = 'JOB' THEN 'Job'
            ELSE e.artifact_type
        END AS asset_kind,
        CASE e.artifact_type
            WHEN 'FEATURE_GROUP' THEN (SELECT fg.name FROM feature_group fg WHERE fg.id = e.artifact_id)
            WHEN 'FEATURE_VIEW' THEN (SELECT fv.name FROM feature_view fv WHERE fv.id = e.artifact_id)
            WHEN 'TRAINING_DATASET' THEN (SELECT td.name FROM training_dataset td WHERE td.id = e.artifact_id)
            WHEN 'JOB' THEN (SELECT j.name FROM jobs j WHERE j.id = e.artifact_id)
            WHEN 'MODEL' THEN (
                SELECT m.name FROM model m
                JOIN model_version mv ON mv.model_id = m.id
                WHERE mv.id = e.artifact_id
            )
            WHEN 'DEPLOYMENT' THEN (SELECT s.name FROM serving s WHERE s.id = e.artifact_id)
        END AS asset_name,
{entered}
    FROM stage_entry e
    GROUP BY e.artifact_type, e.artifact_id
)
{legs}
""".strip()


def chart_specs(stages: list[str]) -> list[ChartSpec]:
    """Average, worst case, spread, and the assets behind them."""
    mean_days = sql_metric("AVG(days)", "avg days")
    max_days = sql_metric("MAX(days)", "max days")
    assets = sql_metric("COUNT(DISTINCT artifact_id)", "assets")
    end_to_end = f"{stages[0]} -> {stages[-1]}"

    return [
        ChartSpec(
            name="Promotion · Average days per transition",
            viz_type="echarts_timeseries_bar",
            width=6,
            params=categorical_bar(
                x_axis="transition",
                series="asset_kind",
                metrics=[mean_days],
                y_axis_format=",.2f",
            ),
        ),
        # The worst case, next to the average, because a mean alone hides the asset that took
        # a quarter and is usually the reason anyone opens this dashboard.
        ChartSpec(
            name="Promotion · Longest days per transition",
            viz_type="echarts_timeseries_bar",
            width=6,
            params=categorical_bar(
                x_axis="transition",
                series="asset_kind",
                metrics=[max_days],
                y_axis_format=",.2f",
            ),
        ),
        # The spread. Average and max together still cannot distinguish "everything takes a
        # week" from "half go in a day and half take a month", which is the thing worth acting on.
        ChartSpec(
            name=f"Promotion · Distribution of days, {end_to_end}",
            viz_type="histogram_v2",
            width=6,
            height=55,
            params={
                "viz_type": "histogram_v2",
                "column": "days",
                "groupby": [],
                "bins": 20,
                "row_limit": 50000,
                "normalize": False,
                "cumulative": False,
                "adhoc_filters": [simple_filter("transition", "==", end_to_end)],
                "x_axis_title": f"days from {stages[0]} to {stages[-1]}",
                "y_axis_title": "assets",
                "x_axis_format": ",.2f",
                "y_axis_format": "SMART_NUMBER",
            },
        ),
        # How many complete each step at all: a transition nobody makes is not a fast one.
        ChartSpec(
            name="Promotion · Assets completing each transition",
            viz_type="echarts_timeseries_bar",
            width=6,
            height=55,
            params=categorical_bar(
                x_axis="transition",
                series="asset_kind",
                metrics=[assets],
            ),
        ),
        # Over time, by when the asset arrived, so a trend is visible rather than one number.
        ChartSpec(
            name="Promotion · Time to promote, by week reached",
            viz_type="echarts_timeseries_line",
            width=12,
            params={
                "viz_type": "echarts_timeseries_line",
                "x_axis": "reached",
                "time_grain_sqla": "P1W",
                "metrics": [mean_days],
                "groupby": ["transition"],
                "adhoc_filters": [],
                "row_limit": 1000,
                "y_axis_format": ",.2f",
            },
        ),
        ChartSpec(
            name="Promotion · Slowest journeys",
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
                    "transition",
                    "started",
                    "reached",
                    "days",
                ],
                "order_by_cols": ['["days", false]'],
                "adhoc_filters": [],
                "row_limit": 100,
            },
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", default=DEFAULT_TAG, help="lifecycle tag schema name")
    parser.add_argument("--field", default=DEFAULT_FIELD, help="field holding the stage")
    parser.add_argument(
        "--stages",
        default=DEFAULT_STAGES,
        help="stage names in promotion order, comma separated",
    )
    args = parser.parse_args()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    if len(stages) < 2:
        parser.error("--stages needs at least two stages to measure a transition between")

    project = hopsworks.login()
    superset = Superset.connect(project.get_superset_api())
    print(f"hopsworks_analytics connection: id={superset.database_id} "
          f"({superset.database_name})\n")

    statement = transitions_sql(args.tag, args.field, stages)

    rows = superset.sql(
        f"SELECT COUNT(*) AS n FROM (\n{statement}\n) _t"
    )
    total = int(list(rows[0].values())[0]) if rows else 0
    print(f"{total} completed transition(s) for tag {args.tag!r} over {' -> '.join(stages)}.")
    if total == 0:
        print(
            f"\nNothing to chart. Either no asset has moved between these stages yet, or the\n"
            f"{args.tag!r} schema is not archiving. History is only recorded while the archive\n"
            f"flag is on, and turning it on later backfills a baseline rather than recovering\n"
            f"transitions that already happened."
        )
        return 1

    for row in superset.sql(
        f"SELECT transition, COUNT(*) AS assets, ROUND(AVG(days), 3) AS avg_days,"
        f" ROUND(MAX(days), 3) AS max_days FROM (\n{statement}\n) _t"
        f" GROUP BY transition ORDER BY transition"
    ):
        print(f"  {str(row['transition']):<16} {row['assets']:>3} asset(s)  "
              f"avg {row['avg_days']} d  max {row['max_days']} d")

    superset.build(
        dataset=DATASET,
        title=DASHBOARD_TITLE,
        statement=statement,
        specs=chart_specs(stages),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
