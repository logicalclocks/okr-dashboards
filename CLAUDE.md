This repo is used to build superset dashboards for Hopsworks. You should pick the most appropriate Chart type based on the dataset and the distribution/cardinality of values.

The first task is to mount all the tables in the mysql 'hopsworks' database as external feature groups. You do this by running 'mount_hopsworks_db.py'. AskUserQuestion if they want to mount the hopsworks database tables if they have not already been mounted.

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


AskUserQuestion:
Do you want to create the dashboards now (executive, developer, others)?

If the user answers yes, then run the python programs to create the dashboards (create_tag_dataset.py, create_executive_dashboard.py, feature_group_dashboard.py, feature_usage_dashboard.py, etc).

'feature_group_dashboard.py' builds the "Feature Group Activity" dashboard: feature/feature-group counts and new-features-per-week growth (bucketed to the week each feature group was created), with native filter "selection boxes" (Tag, Tag value, Feature group kind) that let you slice/group the feature data by tag values mirrored from the 'feature_store_tags_by_value' virtual dataset. It reuses the shared Superset helpers in create_tag_dataset.py, is idempotent, and disables result caching — re-run it anytime to refresh.
