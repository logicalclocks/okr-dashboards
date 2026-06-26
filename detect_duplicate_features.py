#!/usr/bin/env python
"""Flag suspected duplicate features with `claude -p`.

Each run this program:

  1. reads the mounted `feature_group` and `cached_feature` tables (external
     feature groups over the `hopsworks` MySQL DB) via the Hopsworks Feature
     Query Service — no Spark needed,
  2. builds two JSON datasets — the name+description of every feature group, and
     the name+description (plus owning feature group) of every cached feature,
  3. pipes them on stdin to a single headless `claude -p` call whose prompt asks
     it to identify the feature group and feature name of any suspected
     duplicate features, and
  4. (over)writes the offline DELTA feature group `suspected_duplicate_features`
     with the result — `insert(..., overwrite=True)` deletes the rows that were
     there and inserts only the fresh set.

`claude -p` is given NO tools (`--allowed-tools ""`): all the data it needs is on
stdin, so it answers directly and never blocks on a tool permission prompt.

Custom conda environments are disabled on this (reserved) project, so the Claude
Code CLI cannot be baked into the job environment. Instead the job runs on the
stock `python-feature-pipeline` env and bootstraps Claude at runtime: it
downloads the self-contained `claude` binary from HopsFS and materialises the
OAuth credentials from a PRIVATE Hopsworks secret into a temporary
`CLAUDE_CONFIG_DIR`. Stage those two assets first with `stage_claude_assets.py`.

Run:  python detect_duplicate_features.py
Stage assets + schedule it with:  python stage_claude_assets.py
                                   python schedule_duplicate_features_job.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile

import hopsworks
import pandas as pd

# Feature group this pipeline (over)writes.
FG_NAME = "suspected_duplicate_features"
FG_VERSION = 1

# Staged Claude assets (see stage_claude_assets.py): the self-contained binary in
# HopsFS and the OAuth credentials in a PRIVATE user secret.
CLAUDE_BINARY_HOPSFS = "Resources/bin/claude-real"
CLAUDE_SECRET_NAME = "claude_code_oauth_credentials"

# Sentinels the model wraps its JSON answer in, so we can extract it cleanly.
RESULT_BEGIN = "===DUPLICATES_JSON_BEGIN==="
RESULT_END = "===DUPLICATES_JSON_END==="

# The prompt. The two JSON datasets are supplied on stdin (not in the prompt).
INSTRUCTION = (
    "You are given on stdin a single JSON object with two arrays:\n"
    "  - 'feature_groups': every feature group, each {name, description}.\n"
    "  - 'features': every feature, each {feature_group, feature, description} "
    "where 'feature_group' is the name of the feature group the feature "
    "belongs to.\n\n"
    "Identify the feature group (and feature name) of any suspected DUPLICATE "
    "features: features that appear to capture the same thing as another "
    "feature — the same or very similar name and/or description — whether they "
    "live in the same feature group or different ones.\n\n"
    "Output ONLY a JSON array of objects, each with exactly three string "
    "fields:\n"
    "  - 'feature_group': the feature group of the suspected-duplicate feature,\n"
    "  - 'feature_name': the suspected-duplicate feature's name,\n"
    "  - 'reason': which other feature it appears to duplicate and why.\n"
    "List each side of a suspected duplicate pair as its own object. If you "
    "suspect no duplicates, output an empty array [].\n\n"
    f"Print the JSON array between a line containing only {RESULT_BEGIN} and a "
    f"line containing only {RESULT_END}, with nothing else between those two "
    "markers."
)


def _s(value) -> str:
    """A clean string for a possibly-NaN/None pandas cell."""
    return "" if pd.isna(value) else str(value)


# --------------------------------------------------------------------------- #
# 1. Read the source tables
# --------------------------------------------------------------------------- #
def read_inputs(fs):
    fg_cols = ["id", "name", "description",
               "cached_feature_group_id", "stream_feature_group_id"]
    fgs = fs.get_feature_group("feature_group", version=1).select(fg_cols).read()

    cf_cols = ["name", "description",
               "cached_feature_group_id", "stream_feature_group_id"]
    feats = fs.get_feature_group("cached_feature", version=1).select(cf_cols).read()
    return fgs, feats


# --------------------------------------------------------------------------- #
# 2. Build the JSON payload
# --------------------------------------------------------------------------- #
def build_payload(fgs: pd.DataFrame, feats: pd.DataFrame) -> dict:
    """name+description of every FG, and name+description+owning-FG of every
    cached feature.

    A cached_feature links to its parent FG by the *subtype* id, not
    feature_group.id: cached_feature.cached_feature_group_id matches
    feature_group.cached_feature_group_id (likewise stream_feature_group_id).
    """
    cached_map, stream_map = {}, {}
    for r in fgs.itertuples(index=False):
        if pd.notna(r.cached_feature_group_id):
            cached_map[int(r.cached_feature_group_id)] = r.name
        if pd.notna(r.stream_feature_group_id):
            stream_map[int(r.stream_feature_group_id)] = r.name

    feature_groups = [{"name": _s(r.name), "description": _s(r.description)}
                      for r in fgs.itertuples(index=False)]

    features = []
    for r in feats.itertuples(index=False):
        fg_name = None
        if pd.notna(r.cached_feature_group_id):
            fg_name = cached_map.get(int(r.cached_feature_group_id))
        elif pd.notna(r.stream_feature_group_id):
            fg_name = stream_map.get(int(r.stream_feature_group_id))
        features.append({"feature_group": fg_name,
                         "feature": _s(r.name),
                         "description": _s(r.description)})

    return {"feature_groups": feature_groups, "features": features}


# --------------------------------------------------------------------------- #
# 3. Bootstrap Claude (binary + credentials) then ask claude -p
# --------------------------------------------------------------------------- #
def bootstrap_claude(project) -> tuple[str, dict]:
    """Download the staged `claude` binary and materialise the OAuth secret into
    a private CLAUDE_CONFIG_DIR. Returns (binary_path, env) for subprocess.

    Running from the stock pipeline env, neither the binary nor `~/.claude`
    exists in the job container — both are staged by `stage_claude_assets.py`.
    """
    workdir = tempfile.mkdtemp(prefix="claude-")
    binary_path = os.path.join(workdir, "claude")
    config_dir = os.path.join(workdir, "config")
    os.makedirs(config_dir, exist_ok=True)

    # 1. Download the self-contained binary and make it executable.
    project.get_dataset_api().download(
        CLAUDE_BINARY_HOPSFS, local_path=binary_path, overwrite=True)
    os.chmod(binary_path, 0o755)

    # 2. Materialise the OAuth credentials from the PRIVATE secret. claude reads
    #    `<CLAUDE_CONFIG_DIR>/.credentials.json` and refreshes the access token
    #    itself using the long-lived refresh token at call time.
    creds_json = hopsworks.get_secrets_api().get(CLAUDE_SECRET_NAME)
    creds_file = os.path.join(config_dir, ".credentials.json")
    with open(creds_file, "w") as f:
        f.write(creds_json)
    os.chmod(creds_file, 0o600)

    env = {**os.environ, "CLAUDE_CONFIG_DIR": config_dir}
    print(f"Bootstrapped claude binary at {binary_path} "
          f"(CLAUDE_CONFIG_DIR={config_dir}).")
    return binary_path, env


def run_claude(payload: dict, claude_bin: str, claude_env: dict) -> list[dict]:
    data = json.dumps(payload, ensure_ascii=False)
    proc = subprocess.run(
        [claude_bin, "-p", INSTRUCTION,
         "--allowed-tools", "",          # all data is on stdin; no tools needed
         "--output-format", "json"],
        input=data, text=True, capture_output=True, env=claude_env,
    )
    if proc.returncode != 0:
        sys.exit(f"`claude -p` failed (rc={proc.returncode}):\n{proc.stderr}")

    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"Could not parse `claude -p` envelope: {e}\n---\n{proc.stdout}")
    if envelope.get("is_error"):
        sys.exit(f"claude reported an error: {envelope.get('result')}")

    return parse_results(envelope.get("result", ""))


def parse_results(text: str) -> list[dict]:
    m = re.search(re.escape(RESULT_BEGIN) + r"(.*?)" + re.escape(RESULT_END),
                  text, re.S)
    blob = (m.group(1) if m else text).strip()
    # Tolerate a ```json ... ``` fence around the array.
    blob = re.sub(r"^```(?:json)?\s*|\s*```$", "", blob, flags=re.S).strip()
    try:
        dupes = json.loads(blob)
    except json.JSONDecodeError as e:
        sys.exit(f"Could not parse the duplicates JSON: {e}\n---\n{text}")
    if not isinstance(dupes, list):
        sys.exit(f"Expected a JSON array, got {type(dupes).__name__}: {text}")
    return dupes


# --------------------------------------------------------------------------- #
# 4. (Over)write the feature group
# --------------------------------------------------------------------------- #
def write_feature_group(project, dupes: list[dict]) -> None:
    fs = project.get_feature_store()
    df = pd.DataFrame(
        [{"id": i + 1,
          "feature_group": _s(d.get("feature_group")),
          "feature_name": _s(d.get("feature_name")),
          "reason": _s(d.get("reason"))}
         for i, d in enumerate(dupes)],
        columns=["id", "feature_group", "feature_name", "reason"],
    ).astype({"id": "int64", "feature_group": "string",
              "feature_name": "string", "reason": "string"})

    # Clear-and-replace. NOTE: fg.insert(overwrite=True) is unusable for DELTA
    # feature groups on this cluster — the backend's "clear" step recreates the
    # FG and invalidates the in-memory id, so the follow-up commit 404s. Deleting
    # the FG and recreating it, then a plain insert, is the reliable equivalent:
    # it removes every row that was there and inserts only the new ones.
    try:
        fs.get_feature_group(FG_NAME, version=FG_VERSION).delete()
        print(f"Dropped existing '{FG_NAME}' v{FG_VERSION}.")
    except Exception:
        pass  # did not exist yet

    if df.empty:
        print("No suspected duplicates — feature group left empty.")
        return

    fg = fs.create_feature_group(
        name=FG_NAME, version=FG_VERSION,
        description="Features suspected to be duplicates of one another, flagged "
                    "by a claude -p analysis of feature_group + cached_feature "
                    "metadata.",
        primary_key=["id"], online_enabled=False, time_travel_format="DELTA",
    )
    fg.insert(df, wait=True)
    print(f"Wrote {len(df)} row(s) to '{FG_NAME}' v{FG_VERSION}.")


def main() -> None:
    project = hopsworks.login()
    fs = project.get_feature_store()

    fgs, feats = read_inputs(fs)
    print(f"Read {len(fgs)} feature groups and {len(feats)} features.")

    claude_bin, claude_env = bootstrap_claude(project)
    dupes = run_claude(build_payload(fgs, feats), claude_bin, claude_env)
    print(f"claude flagged {len(dupes)} suspected duplicate feature(s).")

    write_feature_group(project, dupes)


if __name__ == "__main__":
    main()
