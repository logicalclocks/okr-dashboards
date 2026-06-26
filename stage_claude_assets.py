#!/usr/bin/env python
"""Stage the assets the `suspected-duplicate-features` job needs to run `claude
-p` from a stock pipeline environment.

Custom conda environments are disabled on this (reserved) project, so we cannot
bake the Claude Code CLI into a cloned env. Instead we stage two things that the
job downloads at runtime:

  1. the self-contained `claude` binary  -> HopsFS  `Resources/bin/claude`
  2. the Claude Code OAuth credentials   -> a PRIVATE Hopsworks user secret

`detect_duplicate_features.py` downloads the binary, materialises the secret into
a private `CLAUDE_CONFIG_DIR`, and runs `claude -p` against it.

Re-run this whenever the local Claude binary or your OAuth credentials change
(e.g. after a `claude` self-update or a re-login).

Run:  python stage_claude_assets.py
"""
import json
import os

import hopsworks

# Source of the self-contained binary in this terminal image.
LOCAL_BINARY = "/usr/local/bin/claude-real"
# Where the OAuth token lives in the user's HopsFS home (a symlink target).
CREDS_PATH = os.path.join(
    os.environ.get("HOPSFS_USER_HOME_DIR", "/hopsfs/Users/meb10000"),
    ".claude", ".credentials.json",
)

BINARY_DIR = "Resources/bin"          # HopsFS dataset dir for the binary
BINARY_NAME = "claude"
SECRET_NAME = "claude_code_oauth_credentials"


def main() -> None:
    project = hopsworks.login()

    # 1. Upload the self-contained claude binary (~230 MB). upload() keeps the
    #    local basename, so it lands as `Resources/bin/claude-real` — that is the
    #    path detect_duplicate_features.py downloads.
    ds = project.get_dataset_api()
    ds.upload(LOCAL_BINARY, BINARY_DIR, overwrite=True)
    print(f"Uploaded binary -> {BINARY_DIR}/{os.path.basename(LOCAL_BINARY)}")

    # 2. Store the OAuth credentials as a PRIVATE user secret (not project-wide).
    with open(CREDS_PATH) as f:
        creds_json = f.read()
    json.loads(creds_json)  # validate it parses before storing

    secrets = hopsworks.get_secrets_api()
    existing = secrets.get_secret(SECRET_NAME)
    if existing is not None:
        existing.delete()
        print(f"Replaced existing secret '{SECRET_NAME}'.")
    secrets.create_secret(SECRET_NAME, creds_json)
    print(f"Stored OAuth credentials in PRIVATE secret '{SECRET_NAME}'.")


if __name__ == "__main__":
    main()
