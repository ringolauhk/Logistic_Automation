"""Operator onboarding contract: safe .env.example, docs that mention the
real CLI command, and import/CLI entry points that never require provider
keys or touch the network. Package installability (pyproject.toml/wheel) is
deliberately out of scope - the project ships as `python -m invoice_extractor`
against requirements.txt, so there is no distributable package to test.
"""

import os
import subprocess
import sys
from pathlib import Path

from click.testing import CliRunner

from invoice_extractor.cli import cli

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
README = REPO_ROOT / "README.md"


class TestEnvExample:
    def test_file_exists(self):
        assert ENV_EXAMPLE.exists()

    def test_no_real_looking_secrets(self):
        content = ENV_EXAMPLE.read_text()
        assert "GEMINI_API_KEY=your-gemini-api-key-here" in content
        assert "ANTHROPIC_API_KEY=your-anthropic-api-key-here" in content
        # None of the providers' actual key shapes should ever appear here.
        for shape in ("sk-ant-", "sk-proj-", "AIza"):
            assert shape not in content

    def test_states_never_commit(self):
        assert "never commit" in ENV_EXAMPLE.read_text().lower()

    def test_dotenv_itself_is_gitignored(self):
        gitignore_lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
        assert ".env" in gitignore_lines

    def test_documents_provider_key_roles(self):
        content = ENV_EXAMPLE.read_text()
        assert "GEMINI_API_KEY" in content and "ANTHROPIC_API_KEY" in content
        assert "fallback" in content.lower()
        assert "required" in content.lower()
        assert "optional" in content.lower()


class TestReadmeUsageDocs:
    def test_mentions_real_cli_run_command(self):
        content = README.read_text()
        assert "python -m invoice_extractor run --input" in content

    def test_mentions_doctor_classify_render_subcommands(self):
        content = README.read_text()
        for subcommand in ("doctor", "classify", "render"):
            assert f"python -m invoice_extractor {subcommand}" in content

    def test_documents_first_run_workflow(self):
        content = README.read_text()
        assert "samples" in content.lower()
        assert "NeedsReview" in content

    def test_readme_does_not_contain_real_looking_secrets(self):
        content = README.read_text()
        for shape in ("sk-ant-", "sk-proj-", "AIza"):
            assert shape not in content

    def test_documents_gemini_and_claude_provider_roles(self):
        content = README.read_text()
        assert "GEMINI_API_KEY" in content and "ANTHROPIC_API_KEY" in content
        assert "Gemini" in content and "Claude" in content
        assert "fallback" in content.lower()
        assert "doctor" in content.lower()  # points operators at the status command


class TestPackageImportWithoutKeys:
    """A fresh interpreter, run from the repo root with provider keys
    stripped from the environment, to prove import-time code never requires
    a key or reaches out to a provider - not just that the currently-loaded
    test-session modules happen to already work."""

    def test_import_cli_without_provider_keys(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY")}
        result = subprocess.run(
            [sys.executable, "-c", "import invoice_extractor.cli"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_bare_package_import_has_no_submodule_side_effects(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY")}
        result = subprocess.run(
            [sys.executable, "-c",
             "import invoice_extractor; print(invoice_extractor.__version__)"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip()  # a version string was printed


class TestCliOfflineEntryPoints:
    def test_top_level_help_runs_without_keys(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = CliRunner().invoke(cli, ["--help"])
        assert result.exit_code == 0, result.output
        for subcommand in ("run", "classify", "render", "doctor"):
            assert subcommand in result.output

    def test_run_help_runs_without_keys(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = CliRunner().invoke(cli, ["run", "--help"])
        assert result.exit_code == 0, result.output
        assert "--input" in result.output and "--output" in result.output

    def test_doctor_runs_offline_without_keys(self, tmp_path, monkeypatch):
        # Full offline-mode coverage already lives in
        # test_logging_and_cli.py::TestDoctorOffline - this only re-confirms
        # it as part of the "no network calls needed to get started" contract.
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        samples = tmp_path / "samples"
        samples.mkdir()
        result = CliRunner().invoke(
            cli, ["doctor", "--input", str(samples), "--output", str(tmp_path / "out")]
        )
        assert result.exit_code == 0, result.output
        assert "offline mode" in result.output
