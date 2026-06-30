import os
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

RUNNER_PATH = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "qoder-pr-review" / "qoder_review_pr.py"
spec = importlib.util.spec_from_file_location("qoder_review_pr", RUNNER_PATH)
assert spec is not None
assert spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class QoderReviewPrTests(unittest.TestCase):
    def test_extract_glm_models_dedupes_model_list(self) -> None:
        text = """MODEL
Auto
GLM-5.2
glm-5.2
GLM-4.6
Kimi-K2.7-Code
"""
        self.assertEqual(runner.extract_glm_models(text), ["GLM-5.2", "GLM-4.6"])

    def test_pick_latest_glm_uses_numeric_version(self) -> None:
        self.assertEqual(runner.pick_latest_glm(["GLM-4.6", "GLM-5", "GLM-5.2"]), "GLM-5.2")

    def test_resolve_model_falls_back_when_list_models_fails(self) -> None:
        with mock.patch.object(runner, "run_capture", return_value=(1, "", "boom")):
            model, fallback_used = runner.resolve_model()

        self.assertEqual(model, "auto")
        self.assertTrue(fallback_used)

    def test_qoder_model_restriction_falls_back_to_lite(self) -> None:
        calls = []

        def fake_run_qoder(prompt: str, model: str) -> tuple[int, str, str]:
            calls.append(model)
            if model in {"GLM-5.2", "auto"}:
                return 1, f"Model '{model}' is restricted for this repository by a security policy.", ""
            return 0, "PASS — Qoder PR Review\n\n## Summary\n- ok\n", ""

        with mock.patch.object(runner, "run_qoder", side_effect=fake_run_qoder):
            code, stdout, stderr, model = runner.run_qoder_with_model_fallback("prompt", "GLM-5.2")

        self.assertEqual(code, 0)
        self.assertEqual(model, "Lite")
        self.assertEqual(calls, ["GLM-5.2", "auto", "Lite"])

    def test_binary_only_files_skip(self) -> None:
        self.assertTrue(runner.should_skip_review(["assets/logo.png", "docs/manual.pdf", "assets/font.woff2"]))

    def test_svg_does_not_skip(self) -> None:
        self.assertFalse(runner.should_skip_review(["assets/logo.svg"]))

    def test_archive_does_not_skip(self) -> None:
        self.assertFalse(runner.should_skip_review(["release/app.zip"]))

    def test_mixed_source_and_binary_does_not_skip(self) -> None:
        self.assertFalse(runner.should_skip_review(["src/app.ts", "assets/logo.png"]))

    def test_empty_changed_files_do_not_skip(self) -> None:
        self.assertFalse(runner.should_skip_review([]))

    def test_validate_inputs_requires_core_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                Path("AGENTS.md").write_text("# policy\n", encoding="utf-8")
                with mock.patch.dict(os.environ, {}, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "GITHUB_REPOSITORY"):
                        runner.validate_inputs()
            finally:
                os.chdir(cwd)

    def test_validate_inputs_requires_root_agents_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            try:
                os.chdir(tmp)
                env = {
                    "GITHUB_REPOSITORY": "owner/repo",
                    "PR_NUMBER": "1",
                    "BASE_REF": "origin/main",
                    "HEAD_REF": "feature/test",
                }
                with mock.patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(RuntimeError, "AGENTS.md"):
                        runner.validate_inputs()
            finally:
                os.chdir(cwd)

    def test_review_output_must_include_required_sections(self) -> None:
        text = """PASS — Qoder PR Review

## Summary
- ok
"""
        self.assertEqual(runner.status_from_output(text), "PASS")
        self.assertIn("## Blocking findings", runner.missing_required_sections(text))


if __name__ == "__main__":
    unittest.main()
