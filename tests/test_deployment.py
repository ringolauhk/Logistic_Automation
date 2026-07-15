"""M8: packaging & deployment static checks - Dockerfile, .dockerignore,
compose, launcher scripts, pyproject, versioning, release archive, docs.
Offline; needs no Docker daemon (container-runtime behavior is validated
separately at build time and reported in the milestone evidence).
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def _code_lines(rel):
    """File content with '#'-comment lines removed - so a comment that
    documents what we deliberately DON'T do (e.g. 'no poppler', 'no
    privileged mode') doesn't trip a forbidden-substring check."""
    out = []
    for ln in _read(rel).splitlines():
        stripped = ln.strip()
        if stripped.startswith("#"):
            continue
        out.append(ln.split(" #", 1)[0] if " #" in ln else ln)
    return "\n".join(out)


# --- A/B: Dockerfile pinned base + non-root -----------------------------------

def test_a_dockerfile_pins_non_latest_base():
    m = re.search(r"^FROM\s+(\S+)", _read("Dockerfile"), re.MULTILINE)
    assert m, "no FROM line"
    base = m.group(1)
    assert ":latest" not in base and base != "python:latest"
    # Exact patch + explicit Debian variant (adjustment 1).
    assert re.search(r"python:3\.13\.\d+-slim-bookworm", base), base


def test_b_dockerfile_runs_non_root():
    df = _read("Dockerfile")
    users = re.findall(r"^USER\s+(\S+)", df, re.MULTILINE)
    assert users, "no USER directive - would run as root"
    assert users[-1] not in ("root", "0"), f"final USER is {users[-1]}"
    assert "useradd" in df and "appuser" in df


def test_b_no_build_toolchain_or_poppler_in_dockerfile():
    # Check executable directives only (comments legitimately mention that we
    # do NOT install poppler).
    df = _code_lines("Dockerfile").lower()
    for forbidden in ("poppler", "pdftoppm", "build-essential", "gcc "):
        assert forbidden not in df, f"unexpected {forbidden!r} in Dockerfile"


def test_dockerfile_uses_tini_for_signals_and_env_hardening():
    df = _read("Dockerfile")
    assert "tini" in df                              # signal forwarding for Ctrl+C
    assert "PYTHONDONTWRITEBYTECODE=1" in df
    assert "PYTHONUNBUFFERED=1" in df
    assert 'ENTRYPOINT ["tini"' in df
    assert 'CMD ["--help"]' in df                    # harmless default


def test_dockerfile_is_multistage_wheel_build_with_no_runtime_source():
    # A multi-stage build keeps build tooling + generated build/ + egg-info in
    # the throwaway builder; the runtime stage installs a prebuilt wheel and
    # keeps NO project source or packaging metadata under /app.
    df = _read("Dockerfile")
    froms = re.findall(r"^FROM\s+(\S+)(?:\s+AS\s+(\S+))?", df, re.MULTILINE)
    assert len(froms) >= 2, "expected a multi-stage build"
    assert any(alias == "builder" for _, alias in froms), "no builder stage"
    assert "pip wheel" in df                          # builder produces a wheel
    assert re.search(r"pip install --no-deps /tmp/wheels/\*\.whl", df)
    assert "rm -rf /tmp/wheels" in df                 # wheel removed after install
    # The runtime stage must NOT copy the project source tree or metadata into
    # the image (that residue was the bug this multi-stage build fixes).
    runtime = df.split("AS builder", 1)[1].split("FROM ", 1)[1]
    assert "COPY invoice_extractor" not in runtime
    assert "COPY pyproject.toml" not in runtime
    assert "pip install --no-deps ." not in runtime   # no in-tree build at runtime


# --- C: .dockerignore ---------------------------------------------------------

def test_c_dockerignore_excludes_secrets_and_data():
    patterns = {ln.strip() for ln in _read(".dockerignore").splitlines()
                if ln.strip() and not ln.strip().startswith("#")}
    for needed in (".env", ".git", ".venv/", "tests/", "output/", "samples/",
                   "*.usage.csv", "*.log", "benchmark/", ".claude/"):
        assert needed in patterns, f".dockerignore missing {needed!r}"


def test_c_dockerignore_keeps_env_example_visible():
    assert "!.env.example" in _read(".dockerignore")


# --- D/E/F: compose -----------------------------------------------------------

def test_d_compose_no_ports_db_privileged_or_inline_secrets():
    code = _code_lines("compose.yaml")   # comments stripped
    low = code.lower()
    assert "ports:" not in low
    assert "privileged" not in low
    assert "image: postgres" not in low and "image: mysql" not in low
    assert "env_file" in code
    assert "OPENROUTER_API_KEY=" not in code


def test_e_f_compose_mounts_input_ro_output_rw():
    c = _read("compose.yaml")
    assert "./input:/data/input:ro" in c
    assert "./output:/data/output" in c
    assert "./output:/data/output:ro" not in c


def test_compose_host_uid_gid_override_and_signal_config():
    c = _read("compose.yaml")
    assert 'user: "${HOST_UID:-1000}:${HOST_GID:-1000}"' in c
    assert "SIGINT" in c and "stop_grace_period" in c


# --- pyproject / versioning ---------------------------------------------------

def test_pyproject_console_script_and_dynamic_version():
    p = _read("pyproject.toml")
    assert 'invoice-extractor = "invoice_extractor.cli:cli"' in p
    assert 'dynamic = ["version"]' in p
    assert "invoice_extractor.__version__" in p
    assert re.search(r'requires = \["setuptools==[\d.]+", "wheel==[\d.]+"\]', p)


def test_pyproject_direct_deps_readable_and_dev_separated():
    p = _read("pyproject.toml")
    assert "pymupdf" in p and "pydantic" in p
    assert 'dev = ["pytest' in p


def test_w_version_matches_package():
    from invoice_extractor import __version__
    assert f'__version__ = "{__version__}"' in _read("invoice_extractor/__init__.py")
    assert __version__ == "0.1.0"


def test_requirements_are_fully_pinned():
    reqs = [ln.strip() for ln in _read("requirements.txt").splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert reqs
    for line in reqs:
        assert "==" in line and ">=" not in line, f"unpinned production dep: {line!r}"


def test_dev_requirements_layer_production_and_pin_pytest():
    dev = _read("requirements-dev.txt")
    assert "-r requirements.txt" in dev
    assert re.search(r"pytest==[\d.]+", dev)


# --- U/V/W: entrypoints -------------------------------------------------------

def test_u_module_entrypoint_version():
    out = subprocess.run(["python", "-m", "invoice_extractor", "--version"],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0 and "0.1.0" in out.stdout


def test_v_console_script_runs_if_on_path():
    exe = shutil.which("invoice-extractor")
    if not exe:
        pytest.skip("console script not on PATH in this env")
    out = subprocess.run([exe, "--version"], capture_output=True, text=True)
    assert out.returncode == 0 and "0.1.0" in out.stdout


# --- launcher scripts ---------------------------------------------------------

_LAUNCHERS = ("run-invoices.sh", "classify-invoices.sh", "doctor.sh",
              "benchmark-score.sh", "build-release.sh")


def test_launchers_exist_strict_and_executable():
    for name in _LAUNCHERS:
        path = ROOT / "scripts" / name
        assert path.exists(), f"missing {name}"
        assert os.access(path, os.X_OK), f"{name} not executable"
        assert "set -euo pipefail" in path.read_text()


def test_launchers_have_no_bash_syntax_errors():
    for name in (*_LAUNCHERS, "_common.sh"):
        r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)],
                           capture_output=True, text=True)
        assert r.returncode == 0, f"{name}: {r.stderr}"


def test_p_run_launcher_refuses_missing_env(tmp_path):
    dep = tmp_path / "dep"
    (dep / "scripts").mkdir(parents=True)
    for name in ("_common.sh", "run-invoices.sh"):
        (dep / "scripts" / name).write_text((ROOT / "scripts" / name).read_text())
    (dep / "scripts" / "run-invoices.sh").chmod(0o755)
    r = subprocess.run(["bash", str(dep / "scripts" / "run-invoices.sh")],
                       capture_output=True, text=True)
    assert r.returncode != 0
    assert "no .env" in (r.stderr + r.stdout)


def test_q_launchers_never_echo_env_or_key():
    for name in ("_common.sh", *_LAUNCHERS[:-1]):
        body = (ROOT / "scripts" / name).read_text()
        assert "cat .env" not in body and "cat ${ROOT}/.env" not in body
        assert "OPENROUTER_API_KEY" not in body


def test_r_s_run_launcher_forwards_flags_without_silent_overwrite():
    body = (ROOT / "scripts" / "run-invoices.sh").read_text()
    assert '"$@"' in body
    # --overwrite is never injected by the script (comments stripped).
    assert "--overwrite" not in re.sub(r"#.*", "", body)


# --- release archive ----------------------------------------------------------

def test_af_ah_build_release_git_tracked_only_and_checksum():
    body = _read("scripts/build-release.sh")
    assert "git archive" in body
    assert "git status --porcelain" in body       # refuses dirty tree
    assert "--allow-dirty" in body
    assert ("sha256sum" in body or "shasum -a 256" in body)


# --- docs (AI) ----------------------------------------------------------------

def test_ai_docs_exist_and_cover_key_topics():
    for doc in ("docs/DEPLOYMENT.md", "docs/RELEASE.md", "docs/OPERATIONS.md",
                "docs/TROUBLESHOOTING.md"):
        assert (ROOT / doc).exists(), f"missing {doc}"
    dep = _read("docs/DEPLOYMENT.md").lower()
    for topic in ("docker compose", "doctor", ".env", "overwrite", "ctrl+c",
                  "rollback", "non-root", "provider"):
        assert topic in dep, f"DEPLOYMENT.md missing topic: {topic}"


# --- AA/AB: source-tree hygiene -----------------------------------------------

def test_ab_source_tree_has_no_committed_secrets_or_real_data():
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=ROOT).stdout.splitlines()
    for f in tracked:
        assert f != ".env", ".env must never be tracked"
        assert not f.endswith(".usage.csv"), f"tracked usage csv: {f}"
        assert not f.endswith(".pdf"), f"tracked PDF: {f}"
        assert not f.endswith(".xlsx"), f"tracked workbook: {f}"
        assert not f.endswith(".log"), f"tracked log: {f}"
