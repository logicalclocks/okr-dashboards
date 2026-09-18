#!/usr/bin/env bash
# Ask the Setup Analytics questions in the terminal and write the answers to
# okrs.md, which CLAUDE.md / AGENTS.md then tell the coding agent to read.
#
# The questions used to be AskUserQuestion prompts inside CLAUDE.md, which only
# Claude Code can answer. Every agent can read a file, and the wizard runs this
# in the Terminal before it launches the agent, so the user answers here and
# the agent picks the answers up. The agent itself must never run this: agents
# run shell commands without a terminal, so `read` would block forever.
#
# Output goes to the user's HopsFS-backed home (HOPSFS_USER_HOME_DIR, falling
# back to HOME), not next to this script: in the terminal image the repo sits
# under /opt, which the terminal user cannot write, and the home is what
# survives a terminal restart. Pass a path to write somewhere else.
#
# Usage: okrs.sh [path/to/okrs.md]
#   Re-running with an existing file shows the current answers and offers to
#   keep them, so the wizard can run it every time without forcing re-entry.
set -euo pipefail

out="${1:-${HOPSFS_USER_HOME_DIR:-$HOME}/okrs.md}"

if [ ! -t 0 ]; then
    echo "okrs.sh: needs an interactive terminal to ask questions (stdin is not a TTY)." >&2
    echo "Run it in the Hopsworks Terminal, not from a coding agent or a job." >&2
    exit 2
fi

if [ -f "$out" ]; then
    echo "Existing answers in $out:"
    echo
    sed 's/^/    /' "$out"
    echo
    read -r -p "Keep these answers? [Y/n] " keep
    case "${keep:-Y}" in
        [Yy]*) echo "Keeping $out."; exit 0 ;;
    esac
    echo
fi

# A non-negative integer, or empty for the default. 0 means "no OKR row for
# this target", which is the rule CLAUDE.md applies when it builds the okrs
# feature group.
ask_number() {
    local prompt="$1" default="$2" answer
    while true; do
        read -r -p "$prompt [$default] " answer
        answer="${answer:-$default}"
        case "$answer" in
            ''|*[!0-9]*) echo "  Please enter a whole number (0 to skip this target)." >&2 ;;
            *) printf '%s' "$answer"; return ;;
        esac
    done
}

ask_yes_no() {
    local prompt="$1" default="$2" answer
    while true; do
        read -r -p "$prompt [$default] " answer
        answer="${answer:-$default}"
        case "$answer" in
            [Yy]|[Yy][Ee][Ss]) printf 'yes'; return ;;
            [Nn]|[Nn][Oo]) printf 'no'; return ;;
            *) echo "  Please answer yes or no." >&2 ;;
        esac
    done
}

echo "Setup Analytics: a few questions about your OKRs for AI assets in Hopsworks"
echo "for the current year. Enter 0 for a target you do not want to track."
echo
features=$(ask_number "Target total number of production features (e.g. 1000)?" 0)
models=$(ask_number "Target total number of production models / feature views (e.g. 10)?" 0)
deployments=$(ask_number "Target total number of production model deployments (e.g. 5)?" 0)
agents=$(ask_number "Target total number of production agent deployments (e.g. 2)?" 0)
echo
mount_db=$(ask_yes_no "Mount the hopsworks metadata tables as external feature groups, if not already mounted?" Y)
daily_job=$(ask_yes_no "Schedule a daily job (04:00) to refresh the tags the dashboards use?" Y)
dashboards=$(ask_yes_no "Create the dashboards now (executive, analyst, jobs, lifecycle, promotion)?" Y)

mkdir -p "$(dirname "$out")"
cat > "$out" <<MD
# Hopsworks Analytics OKRs

Written by okrs.sh on $(date -u +%Y-%m-%dT%H:%MZ). Re-run okrs.sh to change any answer.
The coding agent reads this file instead of asking the questions itself.

## Targets for the current year

A value of 0 means no OKR row is created for that target.

| target | value |
| --- | --- |
| features | $features |
| feature views (models) | $models |
| model deployments | $deployments |
| agent deployments | $agents |

## Setup choices

- mount_hopsworks_db: $mount_db
- schedule_daily_tag_job: $daily_job
- create_dashboards_now: $dashboards
MD

echo
echo "Saved to $out"
