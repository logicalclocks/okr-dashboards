"""Point an existing `update-tag-dataset` job at refresh_dashboards.py.

Installations set up before refresh_dashboards.py existed have a job that runs
create_tag_dataset.py alone. It reports success on every run while the executive dashboard's
targets and duplicate counts never change, and the UI's Refresh Dashboard Now action looks the
job up by name, so it cannot tell the old job from the new one. Changing the instructions in
CLAUDE.md fixes new installations only.

This finds the job, uploads the runner and every sibling script it invokes next to the job's
current entry point, and switches the entry point. Idempotent: running it against a migrated job
re-uploads the scripts and changes nothing else, which is also how the scripts get updated.

    python migrate_refresh_job.py            # migrate, then run once to confirm
    python migrate_refresh_job.py --no-run   # migrate only
"""

from __future__ import annotations

import argparse
import os
import posixpath
import sys

import hopsworks

JOB_NAME = "update-tag-dataset"
ENTRYPOINT = "refresh_dashboards.py"
# Everything refresh_dashboards.py runs, plus what those import. The builders resolve
# `import superset` from their own directory, so the library has to travel with them.
SIBLINGS = [
    "refresh_dashboards.py",
    "create_tag_dataset.py",
    "create_executive_dashboard.py",
    "superset.py",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-run", action="store_true", help="migrate without a confirming run")
    args = parser.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    for name in SIBLINGS:
        if not os.path.exists(os.path.join(here, name)):
            sys.exit(f"{name} is not next to this script; run from a full checkout.")

    project = hopsworks.login()
    job = project.get_jobs_api().get_job(JOB_NAME)
    if job is None:
        sys.exit(f"No job named '{JOB_NAME}'. Nothing to migrate; run the setup flow instead.")

    app_path = job.config.get("appPath") or ""
    if not app_path:
        sys.exit(f"'{JOB_NAME}' has no appPath in its configuration; not a Python job?")
    target_dir = posixpath.dirname(app_path)
    print(f"'{JOB_NAME}' currently runs {app_path}")

    dataset_api = project.get_dataset_api()
    for name in SIBLINGS:
        uploaded = dataset_api.upload(os.path.join(here, name), target_dir, overwrite=True)
        print(f"  uploaded {name} -> {uploaded}")

    new_path = posixpath.join(target_dir, ENTRYPOINT)
    if app_path == new_path:
        print("Entry point already correct; scripts refreshed.")
    else:
        job.config["appPath"] = new_path
        job.save()
        print(f"Entry point changed to {new_path}")

    if args.no_run:
        return 0
    print("Running once to confirm...")
    execution = job.run(await_termination=True)
    state = getattr(execution, "final_status", None) or getattr(execution, "state", None)
    print(f"Execution finished: {state}")
    return 0 if str(state).upper() in ("SUCCEEDED", "FINISHED") else 1


if __name__ == "__main__":
    sys.exit(main())
