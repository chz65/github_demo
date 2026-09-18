# ODSL Script Sync

A GitHub Actions workflow that automatically pushes script files to the OpenDataDSL script REST service whenever they're added, changed, or removed on `main` or `test`.

## What it does

On every push to `main` or `test`, the workflow:

1. Diffs the commits in that push against what was there before.
2. Filters the changed files down to script types the ODSL service understands: `.odsl`, `.html`, `.js`, `.css`, `.py`, `.mustache`.
3. For each added or modified file, builds the JSON payload below and `POST`s it to the ODSL script API.
4. For each deleted (or renamed-away-from) file, sends a `DELETE` for that script's id.
5. Sends a header identifying which ODSL environment to target, based on which branch triggered the run (`main` -> `production`, `test` -> `test`).

Renames are handled as a delete of the old path plus an upsert of the new path. A file that's both deleted and re-added within the same push (e.g. moved) is treated as a single upsert, not a delete-then-add.

## Files in this repo

```
.github/workflows/sync-odsl-scripts.yml   the workflow (trigger + job config)
scripts/sync_odsl_scripts.py              the script that does the diffing, filtering and API calls
README.md                                 this file
```

## The payload

For a file like `scripts/pricing/forward_curve.odsl` in a repo called `energy-scripts`, a POST looks like:

```json
{
  "_id": "energy-scripts\\scripts\\pricing\\forward_curve",
  "_type": "VarScript",
  "category": "odsl",
  "scriptType": "odsl",
  "script": "<base64-encoded raw file content>"
}
```

- **`_id`** is `<repo name>\<path within the repo>\<file name without extension>`, joined with literal backslashes. The repo name comes from GitHub (`github.event.repository.name`), not from anything you configure.
- **`_type`** is always `"VarScript"`.
- **`category`** and **`scriptType`** are both the file's extension, lowercased, with no dot.
- **`script`** is the raw file bytes, base64-encoded.

A `DELETE` for the same file goes to:

```
POST endpoint + "/" + urlencode(_id) + "/*"   -> https://api.opendatadsl.com/api/script/v1/private/energy-scripts%5Cscripts%5Cpricing%5Cforward_curve/*
```

(method is `DELETE`, not `POST` - the `_id` is percent-encoded because it contains backslashes, which aren't valid unescaped in a URL path.)

Every request also carries:

- `Authorization: Basic ...` - built from the `ODSL_USERNAME` / `ODSL_APIKEY` secrets.
- `x-odsl-environment: <value>` - the value of the `ODSL_ENVIRONMENT` variable in whichever GitHub Environment the run resolved to. Omitted entirely if that variable is empty.

## Using this in your own repo

This repo is meant to be cloned or used as a template - the workflow and script don't need any code changes to work in a new repo. What you need to set up is GitHub configuration, not code:

### 1. Get the files into your repo

Either use this repo as a GitHub template (**Use this template** button, if enabled) or copy these two files into your own repo at the same relative paths:

- `.github/workflows/sync-odsl-scripts.yml`
- `scripts/sync_odsl_scripts.py`

Commit and push them to `main`.

### 2. Create two GitHub Environments

In your repo: **Settings -> Environments -> New environment**. Create:

- `production`
- `test`

(Add more later, and a matching branch entry in the workflow's `on.push.branches` and the `environment:` expression, if you ever need a third stage.)

### 3. Set each environment's target variable

On each environment's page, under **Environment variables**, add:

| Environment  | Variable          | Example value |
|--------------|-------------------|----------------|
| `production` | `ODSL_ENVIRONMENT`| `production`   |
| `test`       | `ODSL_ENVIRONMENT`| `test`         |

This is the value your ODSL service expects to see in the `x-odsl-environment` header for that stage - set it to whatever your API actually uses, not necessarily the word "production" or "test".

### 4. Add your ODSL credentials as secrets

**Settings -> Secrets and variables -> Actions -> New repository secret**:

- `ODSL_USERNAME`
- `ODSL_APIKEY`

These are shared across both environments by default. If a client needs different credentials per environment, add the same two secret names as **environment-scoped** secrets instead (on each environment's own page) - environment-scoped values take priority over repo-level ones.

### 5. Push your scripts

Add your `.odsl` / `.html` / `.js` / `.css` / `.py` / `.mustache` files anywhere in the repo (any folder structure works - it's reflected in the `_id`), commit, and push to `test` first to verify against your test ODSL environment. Merge `test` into `main` to promote the same push to `production`.

### 6. Backfill existing scripts (first time only)

If you're adding this workflow to a repo that already has script files in it, they won't be picked up automatically since there's no "previous push" to diff against. Run it manually instead:

**Actions tab -> "Sync ODSL Scripts" -> Run workflow -> tick "full_resync" -> Run workflow.**

This walks every matching file currently checked out and POSTs it (no deletes are sent during a full resync).

## Notes and assumptions

- The workflow uses `POST` for creates/updates. If your ODSL API actually expects `PUT`, change the `method="POST"` in `post_script()` inside `scripts/sync_odsl_scripts.py`.
- `_id` deliberately uses backslashes, not forward slashes, per the ODSL convention this was built against.
- A `404` response to a `DELETE` is treated as success (the script's already gone) rather than failing the run.
- Any other non-2xx response from either the `POST` or `DELETE` call fails the workflow run - check the run's log in the Actions tab for the status code and response body the API returned.
- Consider adding required reviewers to the `production` environment (in its Environment settings) if you want a manual approval gate between merging to `main` and scripts actually reaching production.