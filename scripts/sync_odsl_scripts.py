#!/usr/bin/env python3
"""
Sync changed script files to the OpenDataDSL script REST service.

Diffs the commits in the current push (github.event.before -> github.event.after),
filters to recognized script extensions, and for each file:
  - added / modified / renamed-into -> POST the script JSON to ODSL_ENDPOINT
  - deleted / renamed-away-from     -> DELETE {ODSL_ENDPOINT}/{_id}/*

On a manual workflow_dispatch run with full_resync=true, every matching file
currently in the repo is POSTed instead of diffing.

Required env vars:
  ODSL_ENDPOINT     base REST endpoint, e.g. https://api.opendatadsl.com/api/script/v1/private
  ODSL_USERNAME     basic auth username
  ODSL_APIKEY       basic auth password / api key
  REPO_NAME         repository name (github.event.repository.name)
  GITHUB_EVENT_NAME github event name (push / workflow_dispatch)
  GITHUB_BEFORE     commit SHA before the push (github.event.before)
  GITHUB_AFTER      commit SHA after the push (github.event.after)
  FULL_RESYNC       "true"/"false" - only relevant for workflow_dispatch
"""

import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

ALLOWED_EXTENSIONS = {"odsl", "html", "js", "css", "py", "mustache"}
ZERO_SHA = "0000000000000000000000000000000000000000"
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def run_git(*args):
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=True)
    return result.stdout


def commit_exists(sha):
    if not sha:
        return False
    check = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True
    )
    return check.returncode == 0


def resolve_diff_range(before, after):
    """Pick the two refs to diff between, handling first-push / force-push edge cases."""
    if before and before != ZERO_SHA and commit_exists(before):
        return before, after

    # First push to the branch, or 'before' isn't reachable (force-push that
    # rewrote history, etc). Fall back to the after commit's parent, or the
    # empty tree if it has none (e.g. the very first commit in the repo).
    parent = subprocess.run(["git", "rev-parse", f"{after}^"], capture_output=True, text=True)
    if parent.returncode == 0:
        return parent.stdout.strip(), after
    return EMPTY_TREE_SHA, after


def get_changed_files(before, after):
    """Returns (upserts, deletes) as lists of repo-relative paths."""
    output = run_git("diff", "--name-status", "-M", before, after)
    upserts, deletes = [], []
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status = parts[0]
        if status.startswith("R"):
            old_path, new_path = parts[1], parts[2]
            deletes.append(old_path)
            upserts.append(new_path)
        elif status == "D":
            deletes.append(parts[1])
        elif status in ("A", "M") or status.startswith("C"):
            upserts.append(parts[1])
        else:
            print(f"::warning::Skipping file with unrecognized git status '{status}': {parts[1:]}")
    return upserts, deletes


def extension_of(path):
    name = path.rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower()


def build_id(repo_name, path):
    """<repo name>\\<path in repo>\\<script name without extension>, backslash-separated."""
    if "/" in path:
        directory, filename = path.rsplit("/", 1)
    else:
        directory, filename = "", path
    script_name = filename.rsplit(".", 1)[0]
    parts = [repo_name] + ([p for p in directory.split("/") if p] if directory else []) + [script_name]
    return "\\".join(parts)


def basic_auth_header(username, password):
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def post_script(endpoint, auth_header, repo_name, path, environment):
    if not os.path.isfile(path):
        print(f"::warning::{path} listed as changed but not found on disk, skipping")
        return
    with open(path, "rb") as f:
        content = f.read()
    ext = extension_of(path)
    script_id = build_id(repo_name, path)
    payload = {
        "_id": script_id,
        "_type": "VarScript",
        "category": ext,
        "scriptType": ext,
        "script": base64.b64encode(content).decode("ascii"),
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", auth_header)
    req.add_header("x-odsl-environment", environment)
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"POST {path} -> {script_id}: {resp.status}")
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"::error::POST failed for {path} ({script_id}): {e.code} {body_text}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"::error::POST failed for {path} ({script_id}): {e.reason}")
        sys.exit(1)


def delete_script(endpoint, auth_header, repo_name, path, environment):
    script_id = build_id(repo_name, path)
    encoded_id = urllib.parse.quote(script_id, safe="")
    url = f"{endpoint}/{encoded_id}/*"
    req = urllib.request.Request(url, method="DELETE")
    req.add_header("Authorization", auth_header)
    req.add_header("x-odsl-environment", environment)
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"DELETE {path} -> {script_id}: {resp.status}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            print(f"DELETE {path} -> {script_id}: 404 (already absent, ignoring)")
            return
        body_text = e.read().decode("utf-8", errors="replace")
        print(f"::error::DELETE failed for {path} ({script_id}): {e.code} {body_text}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"::error::DELETE failed for {path} ({script_id}): {e.reason}")
        sys.exit(1)


def dedupe(paths):
    seen = set()
    result = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def main():
    endpoint = os.environ["ODSL_ENDPOINT"].rstrip("/")
    username = os.environ["ODSL_USERNAME"]
    apikey = os.environ["ODSL_APIKEY"]
    repo_name = os.environ["REPO_NAME"]
    environment = os.environ["ENVIRONMENT"]
    event_name = os.environ.get("GITHUB_EVENT_NAME", "push")
    auth_header = basic_auth_header(username, apikey)

    if event_name == "workflow_dispatch" and os.environ.get("FULL_RESYNC", "false").lower() == "true":
        all_files = run_git("ls-files").splitlines()
        upserts = [p for p in all_files if extension_of(p) in ALLOWED_EXTENSIONS]
        deletes = []
        print(f"Full resync requested: {len(upserts)} matching files found.")
    else:
        before = os.environ.get("GITHUB_BEFORE", "")
        after = os.environ.get("GITHUB_AFTER", "")
        if not after:
            print("No GITHUB_AFTER SHA provided, nothing to do.")
            return

        diff_before, diff_after = resolve_diff_range(before, after)
        raw_upserts, raw_deletes = get_changed_files(diff_before, diff_after)

        upserts = dedupe(raw_upserts)
        deletes = dedupe(raw_deletes)
        # A path deleted and re-added within the same push range only needs the upsert.
        deletes = [p for p in deletes if p not in upserts]

        upserts = [p for p in upserts if extension_of(p) in ALLOWED_EXTENSIONS]
        deletes = [p for p in deletes if extension_of(p) in ALLOWED_EXTENSIONS]

    print(f"Scripts to upsert ({len(upserts)}): {upserts}")
    print(f"Scripts to delete ({len(deletes)}): {deletes}")

    for path in upserts:
        post_script(endpoint, auth_header, repo_name, path, environment)

    for path in deletes:
        delete_script(endpoint, auth_header, repo_name, path, environment)


if __name__ == "__main__":
    main()