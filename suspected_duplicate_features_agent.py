#!/usr/bin/env python
"""Nightly feature pipeline: flag suspected duplicate features.

This is an ordinary Python program (so it runs on the simple
`python-feature-pipeline` environment and can be scheduled like any job). Each
run it:

  1. invokes a Hopsworks AGENT TASK (an `agentJobConfiguration` job) via the
     hops CLI to analyse all features and RETURN the suspected duplicates as
     JSON — the agent only reads (permissionPreset READ_ONLY),
  2. parses that JSON, and
  3. (re)creates and overwrites the `suspected_duplicate_features` feature
     group with the results (columns: id, feature_ref, reason).

The feature-group write is done deterministically here with hopsworks-api, not
by the agent — the agent just produces the candidate list.

Deploy & schedule it like any Python job (agent tasks / plain jobs are both
scheduled by name with a Quartz cron — there is no schedule field in the job
config itself):

    hops job deploy suspected-duplicate-features-pipeline \\
        suspected_duplicate_features_agent.py \\
        --env python-feature-pipeline --cron "0 0 4 ? * * *" --overwrite
    # "0 0 4 ? * * *"  ->  nightly at 04:00

Run:  python suspected_duplicate_features_agent.py
"""
import json
import os
import re
import subprocess
import sys

# Agent task (the read-only analysis step).
AGENT_APP_NAME = "suspected-duplicate-features-agent-task"
MODEL = "claude-opus-4-8"
CONFIG_FILENAME = "agent_task.json"
RESULT_BEGIN = "===DUPLICATES_JSON_BEGIN==="
RESULT_END = "===DUPLICATES_JSON_END==="

# Feature group this pipeline writes.
FG_NAME = "suspected_duplicate_features"
FG_VERSION = 1

# Prompt: read-only analysis that emits a JSON array between sentinels. The
# agent does NOT touch any feature group — this program writes the results.
PROMPT = (
    "Use the hops CLI to list all feature names and their descriptions across "
    "every feature group. Identify the features you suspect are duplicates of "
    "one another (for example, the same or very similar names/descriptions "
    "appearing in different feature groups). Output ONLY a JSON array of "
    "objects, each with exactly two string fields: 'feature_ref' (the "
    "suspected-duplicate feature as 'fg_name.feature_name') and 'reason' (why "
    "it is suspected to be a duplicate, e.g. which other feature it appears to "
    f"duplicate and why). Print the JSON array between a line containing only "
    f"{RESULT_BEGIN} and a line containing only {RESULT_END}, with nothing else "
    "between those two markers. Do NOT create, modify, or insert into any "
    "feature group yourself."
)

AGENT_TASK_CONFIG = {
    "type": "agentJobConfiguration",
    "appName": AGENT_APP_NAME,
    "prompt": PROMPT,
    "model": MODEL,
    "maxTurns": 40,
    "permissionPreset": "READ_ONLY",   # analysis only; this program does writes
    "refs": [],                        # operates over all feature groups
    "envVars": ["PYTHONUNBUFFERED=1"],
}


# --------------------------------------------------------------------------- #
# hops CLI helpers
# --------------------------------------------------------------------------- #
def sh(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=True, text=True, capture_output=capture)


def _json_from_cli(stdout: str):
    """Parse CLI --json output, tolerating a leading login banner."""
    start = stdout.find("[")
    if start == -1:
        start = stdout.find("{")
    return json.loads(stdout[start:]) if start != -1 else None


def agent_task_exists() -> bool:
    out = subprocess.run(["hops", "job", "list", "--json"],
                         text=True, capture_output=True).stdout
    data = _json_from_cli(out) or []
    return any(j.get("NAME") == AGENT_APP_NAME for j in data)


# --------------------------------------------------------------------------- #
# 1–2. Run the agent task and collect its JSON result
# --------------------------------------------------------------------------- #
def run_agent_task() -> list[dict]:
    here = os.path.dirname(os.path.abspath(__file__))
    cfg_path = os.path.join(here, CONFIG_FILENAME)
    with open(cfg_path, "w") as f:
        json.dump(AGENT_TASK_CONFIG, f, indent=2)
    print(f"Wrote agent task config: {cfg_path}\n")

    # Idempotent: create the agent task once, then reuse it on later runs.
    if agent_task_exists():
        print(f"Agent task '{AGENT_APP_NAME}' exists; reusing.")
    else:
        sh(["hops", "job", "create", "-f", cfg_path])

    # Run it and wait for completion, then read its output from the logs.
    sh(["hops", "job", "start", AGENT_APP_NAME, "--wait"])
    logs = sh(["hops", "job", "logs", AGENT_APP_NAME, "--tail", "2000"],
              capture=True).stdout
    return parse_results(logs)


def parse_results(text: str) -> list[dict]:
    m = re.search(re.escape(RESULT_BEGIN) + r"(.*?)" + re.escape(RESULT_END),
                  text, re.S)
    if not m:
        sys.exit("Could not find the JSON result markers in the agent output.")
    dupes = json.loads(m.group(1).strip())
    print(f"Agent returned {len(dupes)} suspected duplicate(s).")
    return dupes


# --------------------------------------------------------------------------- #
# 3. Write the feature group
# --------------------------------------------------------------------------- #
def write_feature_group(dupes: list[dict]) -> None:
    import hopsworks
    import pandas as pd

    if not dupes:
        print("No suspected duplicates returned — nothing to write.")
        return

    df = pd.DataFrame(
        [{"id": i + 1,
          "feature_ref": str(d["feature_ref"]),
          "reason": str(d["reason"])}
         for i, d in enumerate(dupes)],
        columns=["id", "feature_ref", "reason"],
    )

    project = hopsworks.login()
    fs = project.get_feature_store()
    fg = fs.get_or_create_feature_group(
        name=FG_NAME, version=FG_VERSION,
        description="Features suspected to be duplicates, flagged nightly by "
                    "an agent task.",
        primary_key=["id"], online_enabled=False,
    )
    # overwrite=True deletes the existing rows, then inserts the fresh set.
    fg.insert(df, overwrite=True, wait=True)
    print(f"Wrote {len(df)} row(s) to '{FG_NAME}' (overwrite).")


def main() -> None:
    dupes = run_agent_task()
    write_feature_group(dupes)


if __name__ == "__main__":
    main()
