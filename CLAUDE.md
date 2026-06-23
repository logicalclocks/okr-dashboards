This repo is used to build superset dashboards for Hopsworks.

The first task is to mount all the tables in the mysql 'hopsworks' database as external feature groups. You do this by running 'mount_hopsworks_db.py'. AskUserQuestion if they want to mount the hopsworks database tables if they have not already been mounted.

The second task is to create a new feature group called okrs that will store the user's OKRs that will be shown in an 'executive dashboard'. If the okrs feature group has not been created and populated, then do the following:


AskUserQuestion: 
The following are some questions about your OKRs (KPIs) for your organization for the curent year for AI assets in Hopsworks.
 - What is your target for the total number of features (e.g., 1000)?
AskUserQuestion: 
 - What is your target for the total number of models (e.g., 10)?
AskUserQuestion: 
 - What is your target for the total number of model deployments (e.g., 8)?
AskUserQuestion: 
 - What is your target for the total number of agent deployments (e.g., 5)?
AskUserQuestion: 
 - What is your target for the total number of apps in Hopsworks (e.g., 10)?
AskUserQuestion: 
 - What is your target for the total number of dashboards in Hopsworks (e.g., 10)?


Use the answers to create the okrs feature group with 'target' and 'value' columns. Create a DataFrame with the following data: "features/models/model deployments/agent deployments/apps/dashboards" as the 'target' entries and the 'value' entries being the numerical answer provided by the user.


Then, you want to
AskUserQuestion:
Can we schedule a daily job to update the schematized tags in the system?
If the user answers yes, create a Python job to run 'create_tag_dataset.py' once/day by default at 04.00. Use 1 CPU and 4 GB of memory in the job.
