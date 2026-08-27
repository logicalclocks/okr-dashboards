"""Build the "Tag Lifecycle" Superset dashboard over hopsworks.tag_history.

hopsworks.tag_history holds one immutable row per transition of one key of one tag on one
artifact: an OPENED when a value became current, a CLOSED when it stopped being current. A value
change writes both at the same instant, so one interval's end is the next one's start.

This script turns that event log into the shape people actually ask about, and then charts it:

  1. tag_history_intervals  -- one row per (artifact, tag, key, value) with added_on / removed_at.
     removed_at is DERIVED, with LEAD() over the next event for the same key, rather than stored.
     That is what keeps the writer append-only: nothing is ever updated, so a lost or repeated
     write cannot leave an interval open forever and inflate the very dwell times this measures.

  2. Charts over that view: how long artifacts sit in each state, what is moving between states,
     and what is currently stuck.

The intervals view lives here rather than in the platform schema because RonDB views are not part
of the schema Hopsworks manages, and a Superset virtual dataset is refreshable without a migration.

Requires the `archive` flag on a tag schema (Hopsworks settings -> schematised tags), which is what
decides whether any history is recorded at all. A tag without it produces no rows here.

Run:  python create_tag_history_dashboard.py
"""
from __future__ import annotations


import hopsworks

from superset import (
    SCHEMA,
    ChartSpec,
    Superset,
    categorical_bar,
    project_filter,
)

INTERVALS_DATASET = "tag_history_intervals"
DASHBOARD_TITLE = "Tag Lifecycle"

# Intervals, derived from the event log.
#
# LEAD() over the events for one (artifact, tag, key) gives the moment the value stopped being
# current: for a value change that is the CLOSED written at the same instant, and for the value an
# artifact still holds there is no next event, so removed_at is NULL and the interval is open.
#
# Only OPENED rows become intervals. A CLOSED row is the END of the interval before it, never the
# start of one, so selecting it would invent an interval that never existed.
#
# But the filter has to come AFTER the window, in an outer query, and that is the whole reason this
# is written as a subquery. SQL evaluates WHERE before window functions, so filtering to OPENED in
# the same SELECT as the LEAD() would hide every CLOSED row from the window. A value change would
# still look right, because the next OPENED shares the CLOSED's timestamp. Everything that ends
# WITHOUT a successor would not: a detach, an artifact delete or a schema delete would leave the
# last OPENED with no following row, so removed_at would be NULL, is_current would be 1, and the
# dwell time would be measured against NOW() and grow forever. Every close-out the backend writes
# would be invisible here, and deleted artifacts would be counted as still in their last state.
#
# event_time is NULL for an attachment that predates the created_on column, where the start is
# genuinely unknown. Those rows are kept (the artifact IS in that state) but dwell_seconds is NULL
# rather than a number computed from a fabricated start.
INTERVALS_SQL = f"""
SELECT
    e.artifact_type,
    e.artifact_id,
    e.project_id,
    e.project_name,
    e.tag_name,
    e.tag_key,
    e.tag_value,
    e.added_on,
    e.removed_at,
    TIMESTAMPDIFF(SECOND, e.added_on, COALESCE(e.removed_at, NOW())) AS dwell_seconds,
    CASE WHEN e.removed_at IS NULL THEN 1 ELSE 0 END AS is_current
FROM (
    SELECT
        h.artifact_type,
        h.artifact_id,
        h.project_id,
        h.project_name,
        h.tag_name,
        h.tag_key,
        h.tag_value,
        h.event_type,
        h.event_time AS added_on,
        LEAD(h.event_time) OVER (
            PARTITION BY h.artifact_type, h.artifact_id, h.tag_name, h.tag_key
            ORDER BY h.event_time,
                     CASE WHEN h.event_type = 'CLOSED' THEN 0 ELSE 1 END,
                     h.id
        ) AS removed_at
    FROM {SCHEMA}.tag_history h
) e
WHERE e.event_type = 'OPENED'
""".strip()


def _adhoc_count(label="count"):
    return {
        "expressionType": "SIMPLE",
        "column": {"column_name": "artifact_id"},
        "aggregate": "COUNT",
        "label": label,
    }


def _adhoc_avg_days(label="avg days in state"):
    # Seconds are what the view stores; days are what anyone reading a lifecycle chart thinks in.
    return {
        "expressionType": "SQL",
        "sqlExpression": "AVG(dwell_seconds) / 86400",
        "label": label,
    }


def _filter(col, op, val):
    return {
        "expressionType": "SIMPLE",
        "subject": col,
        "operator": op,
        "comparator": val,
        "clause": "WHERE",
    }


def chart_specs():
    """The four questions the event log exists to answer, and nothing else.

    Each one is impossible against feature_store_tag_value alone, which knows only the current
    value: they all need the time a value became current, which is what the history adds.
    """
    completed = _filter("is_current", "==", 0)
    current = _filter("is_current", "==", 1)

    return [
        # How long does something sit in each state before moving on? Completed intervals only:
        # including open ones would mix "spent 3 days in qa then moved" with "has been in qa for 3
        # days so far", and drag every average toward whatever is in flight right now.
        ChartSpec(
            name="Average days in each state",
            viz_type="echarts_timeseries_bar",
            width=6,
            params=categorical_bar(
                x_axis="tag_value",
                series="tag_name",
                metrics=[_adhoc_avg_days()],
                adhoc_filters=[completed],
                y_axis_format=",.1f",
            ),
        ),
        # Is it getting slower? Same measure over time, by when the state was entered.
        ChartSpec(
            name="Time in state, by week entered",
            viz_type="echarts_timeseries_line",
            width=6,
            params={
                "viz_type": "echarts_timeseries_line",
                "x_axis": "added_on",
                "time_grain_sqla": "P1W",
                "metrics": [_adhoc_avg_days()],
                "groupby": ["tag_value"],
                "adhoc_filters": [completed],
                "row_limit": 1000,
                "y_axis_format": ",.1f",
            },
        ),
        # Where is everything now. The one chart the live tag could also answer, kept because a
        # lifecycle dashboard is unreadable without the denominator.
        ChartSpec(
            name="Artifacts currently in each state",
            viz_type="echarts_timeseries_bar",
            width=6,
            params=categorical_bar(
                x_axis="tag_value",
                series="artifact_type",
                metrics=[_adhoc_count("artifacts")],
                adhoc_filters=[current],
            ),
        ),
        # What is stuck. Open intervals, longest first: the actionable end of the dashboard.
        ChartSpec(
            name="Longest running current states",
            viz_type="table",
            width=12,
            height=60,
            params={
                "viz_type": "table",
                "query_mode": "raw",
                "all_columns": [
                    "project_name",
                    "artifact_type",
                    "artifact_id",
                    "tag_name",
                    "tag_key",
                    "tag_value",
                    "added_on",
                    "dwell_seconds",
                ],
                "order_by_cols": ['["dwell_seconds", false]'],
                "adhoc_filters": [current],
                "row_limit": 100,
            },
        ),
    ]


def main():
    project = hopsworks.login()
    superset = Superset.connect(project.get_superset_api())
    print(f"hopsworks_analytics connection: id={superset.database_id} "
          f"({superset.database_name})\n")

    # Fail early and legibly. An empty history almost always means no tag schema has `archive`
    # turned on, not that the SQL is wrong, and a dashboard of empty charts does not say so.
    total = int(superset.scalar(f"SELECT COUNT(*) FROM {SCHEMA}.tag_history") or 0)
    print(f"tag_history holds {total} event(s).")
    if total == 0:
        print(
            "No history recorded yet. Turn on 'Archive tag history' for a tag schema in\n"
            "Settings -> Schematised tags; recording starts from that moment, and existing\n"
            "attachments are backfilled at their attach time."
        )

    superset.build(
        dataset=INTERVALS_DATASET,
        title=DASHBOARD_TITLE,
        statement=INTERVALS_SQL,
        specs=chart_specs(),
        # Every chart reads the intervals dataset, which carries project_name.
        filters=lambda dataset_id: [project_filter(dataset_id)],
    )


if __name__ == "__main__":
    main()
