# dogci

Standalone target-repo deployment for Qoder-powered Pull Request review CI.

## What It Does

`dogci` runs a deterministic Python wrapper around `qodercli`:

- uses Qoder `/review`
- prefers the latest available GLM model
- falls back to `auto` when model discovery fails
- falls back from repository-restricted GLM models to `Qwen3.7-Max`, then `auto`
- deploys the bundled review policy to project-root `AGENTS.md` before Qoder starts
- skips only explicit safe binary/document asset PRs
- posts or updates one stable PR comment
- exits with `PASS`, `FAIL`, or `SKIP`

## Quick Deploy

Copy the standalone files into the target repository:

```text
.github/
  workflows/
    qoder-pr-review.yml
    qoder-pr-review/
      qoder_review_pr.py
      AGENTS.md
```

The workflow file:

```yaml
name: Qoder PR Review

on:
  pull_request:
    types:
      - opened
      - synchronize
      - reopened
      - ready_for_review

permissions:
  contents: read
  pull-requests: write
  issues: write

concurrency:
  group: qoder-pr-review-${{ github.event.pull_request.number }}
  cancel-in-progress: true

jobs:
  qoder-pr-review:
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-24.04

    env:
      QODERCLI_VERSION: "1.0.34"
      QODERCLI_CACHE_DIR: .github/.cache/qodercli
      QODER_REVIEW_TIMEOUT_SECONDS: "600"

    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-node@v4
        with:
          node-version: "22"

      - id: qodercli-cache-restore
        uses: actions/cache/restore@v4
        with:
          path: ${{ env.QODERCLI_CACHE_DIR }}
          key: qodercli-${{ runner.os }}-${{ runner.arch }}-${{ env.QODERCLI_VERSION }}

      - if: steps.qodercli-cache-restore.outputs.cache-hit != 'true'
        run: |
          mkdir -p "$QODERCLI_CACHE_DIR"
          npm install -g "@qoder-ai/qodercli@${QODERCLI_VERSION}" --prefix "$GITHUB_WORKSPACE/$QODERCLI_CACHE_DIR"

      - if: steps.qodercli-cache-restore.outputs.cache-hit != 'true'
        uses: actions/cache/save@v4
        with:
          path: ${{ env.QODERCLI_CACHE_DIR }}
          key: ${{ steps.qodercli-cache-restore.outputs.cache-primary-key }}

      - run: echo "$GITHUB_WORKSPACE/$QODERCLI_CACHE_DIR/bin" >> "$GITHUB_PATH"

      - run: |
          ACTUAL_VERSION="$(qodercli --version)"
          echo "qodercli version: ${ACTUAL_VERSION}"
          test "${ACTUAL_VERSION}" = "${QODERCLI_VERSION}"

      - run: cp .github/workflows/qoder-pr-review/AGENTS.md AGENTS.md

      - env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          QODER_PERSONAL_ACCESS_TOKEN: ${{ secrets.QODER_PERSONAL_ACCESS_TOKEN }}
          GITHUB_REPOSITORY: ${{ github.repository }}
          PR_NUMBER: ${{ github.event.pull_request.number }}
          BASE_REF: origin/${{ github.event.pull_request.base.ref }}
          HEAD_REF: ${{ github.event.pull_request.head.ref }}
        run: |
          cd "$GITHUB_WORKSPACE"
          python3 .github/workflows/qoder-pr-review/qoder_review_pr.py
```

Create the `QODER_PERSONAL_ACCESS_TOKEN` repository secret from a token accepted by `qodercli`.

## Deployed Policy

The source review policy is stored at [.github/workflows/qoder-pr-review/AGENTS.md](.github/workflows/qoder-pr-review/AGENTS.md).

The workflow copies it to project-root `AGENTS.md` before Qoder starts. Qoder is launched from `$GITHUB_WORKSPACE`, so it reads the root policy.

## Deployment Files

Files to copy into each target repository:

| Target path | Purpose |
|---|---|
| `.github/workflows/qoder-pr-review.yml` | Workflow trigger, qodercli cache/install, root policy deploy, runner invocation |
| `.github/workflows/qoder-pr-review/qoder_review_pr.py` | Deterministic PR review runner |
| `.github/workflows/qoder-pr-review/AGENTS.md` | Source review policy copied to project-root `AGENTS.md` |

Development-only files:

| Path | Purpose |
|---|---|
| `tests/` | Local smoke tests |

## Result Contract

Every run ends with one of:

```text
PASS — Qoder PR Review
FAIL — Qoder PR Review
SKIP — Qoder PR Review
```

`SKIP` is only for PRs where all changed files are ignored binary/document assets such as images, PDFs, media, or fonts.
