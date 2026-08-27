This repo is used to build superset dashboards for Hopsworks. You should pick the most appropriate Chart type based on the dataset and the distribution/cardinality of values.

The first task is to mount all the tables in the mysql 'hopsworks' database as external feature groups. You do this by running 'mount_hopsworks_db.py'. AskUserQuestion if they want to mount the hopsworks database tables if they have not already been mounted.

That same run also registers the 'asset_lifecycle' tag schema if it is not already there: status (deprecated | dev | qa | uat | prod) and owner. It is created with history archiving ON, which is what lets create_lifecycle_dashboard.py chart how long assets sit in each stage. Archiving cannot be added retroactively: switching it on later backfills a baseline rather than recovering the transitions that happened while it was off, so creation is the only moment that loses nothing.

An existing schema is left exactly as it is, archive flag included, because re-registering would mean deleting it and that cascades away every attachment across every project. If it exists without archiving, the run says so and prints the admin call to turn it on.

Registering a tag schema is admin-only, which is fine here: this wizard is admin-only too.

The second task is to create a new feature group called okrs that will store the user's OKRs that will be shown in an 'executive dashboard'. If the okrs feature group has not been created and populated, then do the following:

AskUserQuestion: 
The following are some questions about your OKRs (KPIs) for your organization for the curent year for AI assets in Hopsworks.
 - What is your target for the total number of production features (e.g., 1000)?
AskUserQuestion: 
 - What is your target for the total number of production models (e.g., 10)?
AskUserQuestion: 

If the user answers '0' or 'none/no', then do not include that value as an OKR row.

Use the answers to create the okrs feature group with 'target' and 'value' columns. Create a DataFrame with the following data: "features/feature views (models)/model deployments/agent deployments" as the 'target' entries and the 'value' entries being the numerical answer provided by the user.

We will then create a schematized tag called 'asset' if it doesn't exist. 
Here is the json for the 'status' schematized tag:
{
    "name": "asset",
    "description": "Status of AI assets in Hopsworks",
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "description": "Status of AI assets in Hopsworks",
            "enum": [
                "deprecated",
                "rnd",
                "qa",
                "uat",
                "prod"
            ]
        },
        "asset_ts": {
            "type": "string",
            "description": "When the tag was added YYYYMMDD HH:MM, ISO format.",
        }
    },
    "required": [
        "status"
    ],
    "additionalProperties": false
}

Build the executive dashboard by running 'create_executive_dashboard.py'. This reads the targets from the okrs feature group and pairs each one against its live actual, computed from the real hopsworks metadata tables via the hopsworks_analytics JDBC connection in Superset (no Trino). Re-run it after the OKR targets change to refresh them. Note that when you mount the MySQL tables as feature groups, it can rename columns. You will create the dashboards against the MySQL tables, so use its colun names.


Then, you want to
AskUserQuestion:
Can we schedule a daily job to update the schematized tags in the system?
If the user answers yes, create a Python job to run 'create_tag_dataset.py' once/day by default at 04.00. Use 1 CPU and 4 GB of memory in the job.

Name that job exactly `update-tag-dataset`. This is a contract, not a preference: the Hopsworks UI's "Refresh Dashboard Now" action looks the job up by that literal name (ANALYTICS_TAG_JOB in hopsworks-front's `src/modules/wizard/Wizard.tsx`). Any other name and the action 404s and tells the user to run Setup Analytics first, which they will already have done.


AskUserQuestion:
Do you want to create the dashboards now (executive, developer, others)?

If the user answers yes, then run the python programs to create the dashboards: create_tag_dataset.py, create_executive_dashboard.py, create-analyst-dashboard.py, create_jobs_dashboard.py, create_tag_history_dashboard.py, create_lifecycle_dashboard.py.

'create_tag_history_dashboard.py' builds the "Tag Lifecycle" dashboard over hopsworks.tag_history: how long artifacts sit in each tag value, whether that is getting slower, what is in each state now, and what is currently stuck. It registers a `tag_history_intervals` virtual dataset that derives added_on/removed_at from the append-only event log with a window function, and charts that. It only has data for tag schemas with "Archive tag history" turned on (Settings -> Schematised tags in the Hopsworks UI); a schema without it records nothing, and the script says so rather than building empty charts.

'create-analyst-dashboard.py' builds the analyst dashboard covering feature/feature-group counts and growth over time, with native filter "selection boxes" (Tag, Tag value, Feature group kind) that let you slice/group the feature data by tag values mirrored from the 'feature_store_tags_by_value' virtual dataset. It reuses the shared Superset helpers in create_tag_dataset.py, is idempotent, and disables result caching — re-run it anytime to refresh. It supersedes the removed 'feature_group_dashboard.py' and 'feature_usage_dashboard.py'; do not try to run those.


'superset.py' is the library the dashboard builders share: connecting to the analytics database,
registering a virtual dataset, (re)creating charts and laying them out. It has no dashboard of its
own and is not run directly. Import from it rather than from another builder script, which is what
the builders used to do and which dragged that script's constants and argument parsing along.

'create_lifecycle_dashboard.py' builds the "Asset Lifecycle" dashboard: how assets move through
dev -> qa -> prod, per asset kind. It reads hopsworks.tag_history through a
`asset_lifecycle_intervals` virtual dataset and charts how many assets sit in each stage now, how
long each stage takes, whether that is trending, and what is stuck. Pass `--tag` and `--field` if
the lifecycle tag is not `asset_lifecycle`/`status`.

It covers feature groups, feature views, training datasets, models, jobs, model deployments and
agent deployments. The last two are the same kind of row in `serving` and are told apart by whether
a model artifact is attached. **Hopsworks apps are not covered**: they are not a taggable artifact
(no tags sub-resource on the apps API, no tag methods on the App entity, no APP artifact type in
tag_history), so a lifecycle tag cannot be attached to one. Covering apps needs a backend change,
not a dashboard one.

Two things about the interval SQL, both of which produced wrong numbers before they were fixed and
neither of which is obvious from reading the result:

  - The `LEAD()` window must run over ALL events, with the OPENED filter applied in an OUTER query.
    SQL evaluates WHERE before window functions, so filtering inside hides every CLOSED row from
    the window, and anything ending without a successor (a detach, an artifact delete) reads as
    still current with a dwell growing against NOW() forever.
  - The window must order by `(event_time, CLOSED-before-OPENED, id)`. Both halves of a value change
    share a timestamp by design, and the row ids are NOT assigned in the logical order, so ordering
    by `(event_time, id)` returns an interval's own start as its end and every intermediate stage
    reports a zero-second dwell.

'seed_asset_lifecycle.py' attaches that lifecycle tag to every taggable asset in a project and
promotes a share of them through the stages, so the dashboard has real transitions to read. It is a
demo/test aid, not part of the setup flow: run it against a populated project
(`python seed_asset_lifecycle.py --project <name>`) when you need data. It turns archiving on
BEFORE attaching anything, because turning it on afterwards backfills each attachment at its attach
time and records no transitions.
