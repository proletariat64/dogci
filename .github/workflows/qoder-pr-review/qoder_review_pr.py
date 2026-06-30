#!/usr/bin/env python3
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

COMMENT_MARKER = "<!-- qoder-pr-review:v1 -->"
MAX_COMMENT_CHARS = 60000
POLICY_TARGET = Path("AGENTS.md")
SECRET_ENV_NAMES = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
)

SAFE_SKIP_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".ico",
    ".bmp",
    ".tif",
    ".tiff",
    ".pdf",
    ".mp3",
    ".wav",
    ".mp4",
    ".mov",
    ".webm",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
}

NEVER_SKIP_NAMES = {
    "README.md",
    "AGENTS.md",
}

NEVER_SKIP_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".sh",
    ".bash",
    ".zsh",
    ".md",
    ".mdx",
    ".lock",
    ".xml",
    ".html",
    ".css",
    ".scss",
    ".sql",
}

VALID_RESULT_PREFIXES = (
    "PASS — Qoder PR Review",
    "FAIL — Qoder PR Review",
    "SKIP — Qoder PR Review",
)

REQUIRED_REVIEW_SECTIONS = (
    "## Summary",
    "## Blocking findings",
    "## Non-blocking suggestions",
    "## Acceptance check",
    "## File-skip check",
    "## Recommended next action",
    "## Model used",
)


def github_warning(message: str) -> None:
    if os.getenv("GITHUB_ACTIONS"):
        safe = message.replace("\n", " ")
        print(f"::warning title=Qoder PR Review::{safe}", file=sys.stderr)
    else:
        print(f"WARNING: {message}", file=sys.stderr)


def log(message: str) -> None:
    print(message, file=sys.stderr)


def run_capture(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.returncode, result.stdout, result.stderr


def qodercli_bin() -> str:
    return os.getenv("QODERCLI_BIN") or "qodercli"


def redact_known_secrets(text: str) -> str:
    redacted = text
    for name in SECRET_ENV_NAMES:
        value = os.getenv(name)
        if value and len(value) >= 4:
            redacted = redacted.replace(value, "***")
    return redacted


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Required environment variable is missing: {name}")
    return value


def validate_inputs() -> dict[str, str]:
    env = {
        "repository": require_env("GITHUB_REPOSITORY"),
        "pr_number": require_env("PR_NUMBER"),
        "base_ref": require_env("BASE_REF"),
        "head_ref": require_env("HEAD_REF"),
    }

    if os.getenv("GITHUB_ACTIONS") and not (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")):
        raise RuntimeError("GH_TOKEN or GITHUB_TOKEN is required in GitHub Actions to post PR comments")

    if not POLICY_TARGET.is_file():
        raise RuntimeError("Project-root AGENTS.md is required for Qoder review policy")

    log("Input validation: ok")
    return env


def changed_files(base_ref: str) -> list[str]:
    code, stdout, stderr = run_capture(["git", "diff", "--name-only", f"{base_ref}...HEAD"])
    if code != 0:
        raise RuntimeError(f"Failed to list changed files: {stderr.strip()}")

    files = [line.strip() for line in stdout.splitlines() if line.strip()]
    log(f"Changed-file discovery: {len(files)} file(s)")
    for path in files:
        log(f"Changed file: {path}")
    return files


def is_safe_binary_asset(path: str) -> bool:
    p = Path(path)
    suffix = p.suffix.lower()
    if p.name in NEVER_SKIP_NAMES:
        return False
    if suffix in NEVER_SKIP_EXTENSIONS:
        return False
    return suffix in SAFE_SKIP_EXTENSIONS


def should_skip_review(files: list[str]) -> bool:
    if not files:
        log("Skip decision: review required because changed-file list is empty")
        return False

    skip = all(is_safe_binary_asset(path) for path in files)
    if skip:
        log("Skip decision: skip because all changed files are safe binary/document assets")
    else:
        log("Skip decision: review required")
    return skip


def extract_glm_models(text: str) -> list[str]:
    found: list[str] = []
    for line in text.splitlines():
        found.extend(re.findall(r"\bGLM[-_A-Za-z0-9.]*\b", line, flags=re.IGNORECASE))

    seen: set[str] = set()
    unique: list[str] = []
    for item in found:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def version_key(model: str) -> tuple[int, ...]:
    match = re.search(r"GLM[-_]?(\d+(?:\.\d+)*)", model, flags=re.IGNORECASE)
    if not match:
        return (0,)
    return tuple(int(part) for part in match.group(1).split("."))


def pick_latest_glm(models: Iterable[str]) -> str | None:
    glm_models = [model for model in models if model.upper().startswith("GLM")]
    if not glm_models:
        return None
    return max(glm_models, key=lambda model: (version_key(model), -len(model)))


def resolve_model() -> tuple[str, bool]:
    code, stdout, stderr = run_capture([qodercli_bin(), "--list-models"])
    if code != 0:
        github_warning("Failed to run `qodercli --list-models`; fallback to `--model auto`.")
        if stderr.strip():
            log(f"Model fallback reason: {redact_known_secrets(stderr.strip())}")
        log("Selected model: auto (fallback)")
        return "auto", True

    latest_glm = pick_latest_glm(extract_glm_models(stdout))
    if not latest_glm:
        github_warning("No GLM model found in `qodercli --list-models`; fallback to `--model auto`.")
        log("Selected model: auto (fallback)")
        return "auto", True

    log(f"Selected model: {latest_glm}")
    return latest_glm, False


def model_restricted(stdout: str, stderr: str) -> bool:
    combined = f"{stdout}\n{stderr}".lower()
    return "restricted for this repository by a security policy" in combined


def qoder_failure_detail(code: int, stdout: str, stderr: str) -> str:
    parts = []
    if stdout.strip():
        parts.append(f"stdout: {redact_known_secrets(stdout.strip())}")
    if stderr.strip():
        parts.append(f"stderr: {redact_known_secrets(stderr.strip())}")
    if not parts:
        parts.append(f"qodercli exit code {code}")
    return "; ".join(parts)


def build_prompt(env: dict[str, str]) -> str:
    return f"""/review
Review GitHub PR #{env['pr_number']} in {env['repository']}.
Base: {env['base_ref']}
Head: {env['head_ref']}
Follow project-root AGENTS.md strictly.
Return only the final review markdown.
The first non-empty line must be exactly `PASS — Qoder PR Review` or `FAIL — Qoder PR Review`.
Do not include any preface, separator, or explanation before the status line.
"""


def first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def status_from_output(text: str) -> str | None:
    first = first_non_empty_line(text)
    for prefix in VALID_RESULT_PREFIXES:
        if first == prefix:
            return prefix.split(" ", 1)[0]
    return None


def missing_required_sections(text: str) -> list[str]:
    return [section for section in REQUIRED_REVIEW_SECTIONS if section not in text]


def with_actual_model_used(text: str, model: str) -> str:
    replacement = f"## Model used\n- {model}\n"
    if "## Model used" not in text:
        return text
    return re.sub(r"(?ms)^## Model used\s*\n.*\Z", replacement, text.rstrip()) + "\n"


def trim_comment(body: str) -> str:
    if len(body) <= MAX_COMMENT_CHARS:
        return body
    suffix = "\n\n_Comment truncated by dogci because it exceeded the safe GitHub comment size._\n"
    return body[: MAX_COMMENT_CHARS - len(suffix)] + suffix


def make_comment_body(result_markdown: str) -> str:
    safe_markdown = redact_known_secrets(result_markdown)
    return trim_comment(f"{COMMENT_MARKER}\n{safe_markdown.strip()}\n")


def gh_api_json(args: list[str]) -> object:
    code, stdout, stderr = run_capture(["gh", "api", *args])
    if code != 0:
        raise RuntimeError(f"gh api failed: {redact_known_secrets(stderr.strip())}")
    return json.loads(stdout or "null")


def gh_api_with_payload(method: str, endpoint: str, payload: dict[str, str]) -> object:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as f:
        json.dump(payload, f)
        payload_path = f.name
    try:
        return gh_api_json(["-X", method, endpoint, "--input", payload_path])
    finally:
        Path(payload_path).unlink(missing_ok=True)


def list_issue_comments(repository: str, pr_number: str) -> list[object]:
    comments: list[object] = []
    page = 1
    while True:
        current = gh_api_json([f"repos/{repository}/issues/{pr_number}/comments?per_page=100&page={page}"])
        if not isinstance(current, list):
            raise RuntimeError("GitHub comments API returned an unexpected response")
        comments.extend(current)
        if len(current) < 100:
            return comments
        page += 1


def post_or_update_comment(repository: str, pr_number: str, result_markdown: str) -> str | None:
    if not (os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")):
        if os.getenv("GITHUB_ACTIONS"):
            raise RuntimeError("GitHub token is required in GitHub Actions to post PR comments")
        log("PR comment result: skipped because no GitHub token is available")
        return None

    body = make_comment_body(result_markdown)
    comments = list_issue_comments(repository, pr_number)

    existing_id = None
    for comment in comments:
        if isinstance(comment, dict) and COMMENT_MARKER in str(comment.get("body", "")):
            existing_id = comment.get("id")
            break

    if existing_id:
        updated = gh_api_with_payload("PATCH", f"repos/{repository}/issues/comments/{existing_id}", {"body": body})
        url = updated.get("html_url") if isinstance(updated, dict) else None
        log(f"PR comment result: updated {url}")
        return url

    created = gh_api_with_payload("POST", f"repos/{repository}/issues/{pr_number}/comments", {"body": body})
    url = created.get("html_url") if isinstance(created, dict) else None
    log(f"PR comment result: created {url}")
    return url


def skip_result(files: list[str]) -> str:
    changed = "\n".join(f"- `{path}`" for path in files)
    return f"""SKIP — Qoder PR Review

## Summary
- Review skipped because this PR changes only ignored binary/document asset files.

## File-skip check
- Skip used: yes.
- Reason: all changed files are safe binary/document assets.

## Changed files
{changed}

## Recommended next action
- Merge if no separate human asset review is required.
"""


def fail_result(summary: str, details: str, model: str = "unknown") -> str:
    safe_details = redact_known_secrets(details) or "Unknown failure."
    return f"""FAIL — Qoder PR Review

## Summary
- {summary}

## Blocking findings
- {safe_details}

## Non-blocking suggestions
- None.

## Acceptance check
- Review could not complete successfully.

## File-skip check
- Binary/document skip was not used.

## Recommended next action
- Fix the blocking issue and rerun review.

## Model used
- {model}
"""


def run_qoder(prompt: str, model: str) -> tuple[int, str, str]:
    cmd = [
        qodercli_bin(),
        "--model",
        model,
        "--output-format",
        "text",
        "-p",
        prompt,
    ]
    log(f"Qoder invocation: starting qodercli /review with model {model}")
    return run_capture(cmd)


def run_qoder_with_model_fallback(prompt: str, preferred_model: str) -> tuple[int, str, str, str]:
    attempted: set[str] = set()
    last_model = preferred_model
    code = 1
    stdout = ""
    stderr = ""
    for model in [preferred_model, "auto", "Lite"]:
        if model in attempted:
            continue
        attempted.add(model)
        last_model = model

        code, stdout, stderr = run_qoder(prompt, model)
        log(f"Qoder exit status: {code}")
        if code == 0:
            return code, stdout, stderr, model

        if model_restricted(stdout, stderr):
            github_warning(f"Qoder model `{model}` is restricted for this repository; trying fallback model.")
            continue

        return code, stdout, stderr, model

    return code, stdout, stderr, last_model


def handle_result_comment(env: dict[str, str], result: str) -> None:
    post_or_update_comment(env["repository"], env["pr_number"], result)


def main() -> int:
    try:
        env = validate_inputs()
        files = changed_files(env["base_ref"])

        if should_skip_review(files):
            result = skip_result(files)
            print(result)
            handle_result_comment(env, result)
            return 0

        model, fallback_used = resolve_model()
        if fallback_used:
            log("Model fallback used: auto")

        code, stdout, stderr, model = run_qoder_with_model_fallback(build_prompt(env), model)
        if code != 0:
            result = fail_result(
                "qodercli failed to complete review.",
                qoder_failure_detail(code, stdout, stderr),
                model,
            )
            print(result)
            handle_result_comment(env, result)
            return code or 1

        status = status_from_output(stdout)
        safe_stdout = redact_known_secrets(stdout)
        if status is None or status == "SKIP":
            result = fail_result(
                "Qoder output did not match required PASS/FAIL contract.",
                "The first non-empty line must be `PASS — Qoder PR Review` or `FAIL — Qoder PR Review` for a real review.",
                model,
            )
            print(result)
            handle_result_comment(env, result)
            return 1

        missing_sections = missing_required_sections(safe_stdout)
        if missing_sections:
            result = fail_result(
                "Qoder output did not include all required review sections.",
                f"Missing required section(s): {', '.join(missing_sections)}.",
                model,
            )
            print(result)
            handle_result_comment(env, result)
            return 1

        final_output = with_actual_model_used(safe_stdout, model)
        print(final_output)
        handle_result_comment(env, final_output)
        return 0 if status == "PASS" else 1

    except Exception as exc:
        result = fail_result("dogci runner failed before review completed.", str(exc))
        print(result)
        repository = os.getenv("GITHUB_REPOSITORY")
        pr_number = os.getenv("PR_NUMBER")
        if repository and pr_number:
            try:
                post_or_update_comment(repository, pr_number, result)
            except Exception as comment_exc:
                log(f"Failed to post failure comment: {redact_known_secrets(str(comment_exc))}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
