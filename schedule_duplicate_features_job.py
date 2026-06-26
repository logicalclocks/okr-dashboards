#!/usr/bin/env python
"""Schedule `detect_duplicate_features.py` as a daily Hopsworks job.

Uploads the detection script to HopsFS, creates a PYTHON job on the stock
`python-feature-pipeline` environment, and schedules it daily at 04:00.

Re-run this to push an updated script / refresh the schedule — the upload uses
overwrite and `create_job` updates the job if it already exists.

PREREQUISITE: run `stage_claude_assets.py` once first. The job environment has no
Claude Code CLI (custom conda envs are disabled on this reserved project), so
`detect_duplicate_features.py` downloads the staged `claude` binary from HopsFS
and the OAuth credentials from a private secret at runtime.

Run:  python stage_claude_assets.py        # one-time / when binary or creds change
      python schedule_duplicate_features_job.py
"""
import os

import hopsworks

# A stock pipeline env backs the PYTHON job; Claude is bootstrapped at runtime by
# the script itself (see detect_duplicate_features.py / stage_claude_assets.py).
# We do NOT use `agent-job` here: that image ships the claude CLI but is not a
# pipeline base, so it lacks `python-exec.sh` and cannot start a PYTHON job.
JOB_NAME = "suspected-duplicate-features"
ENV_NAME = "python-feature-pipeline"         # stock env; claude staged at runtime
SCRIPT = "detect_duplicate_features.py"
UPLOAD_DIR = "Resources"
CRON = "0 0 4 ? * * *"                       # daily at 04:00 (Quartz cron)


def main() -> None:
    project = hopsworks.login()

    here = os.path.dirname(os.path.abspath(__file__))
    local_script = os.path.join(here, SCRIPT)

    # 1. Upload the detection script to HopsFS.
    ds = project.get_dataset_api()
    ds.upload(local_script, UPLOAD_DIR, overwrite=True)
    app_path = f"/Projects/{project.name}/{UPLOAD_DIR}/{SCRIPT}"
    print(f"Uploaded {SCRIPT} -> {app_path}")

    # 2. Create (or update) the PYTHON job on the claude-enabled environment.
    api = project.get_job_api()
    config = api.get_configuration("PYTHON")
    config["appPath"] = app_path
    config["environmentName"] = ENV_NAME          # SDK config CAN set the env
    config["resourceConfig"]["cores"] = 1.0
    config["resourceConfig"]["memory"] = 4096      # headroom for claude + reads
    job = api.create_job(JOB_NAME, config)

    # 3. Schedule it daily at 04:00.
    job.schedule(cron_expression=CRON)
    print(f"Scheduled '{JOB_NAME}' at '{CRON}' on env '{ENV_NAME}'.")
    print(f"Explore it: {job.get_url()}")


if __name__ == "__main__":
    main()
