# Release & packaging

Pilot releases are **local** deployment archives — no PyPI, no container
registry, no GitHub release, no automatic publishing.

## Version source

One authoritative version: `invoice_extractor/__version__` (`0.1.0`).
`pyproject.toml` reads it dynamically; `invoice-extractor --version` and the
image `org.opencontainers.image.version` label derive from the same value.
Bump it by editing `invoice_extractor/__init__.py` only.

## Release artifacts (pilot)

A pilot deliverable is the source deployment folder / archive containing:

- `Dockerfile`, `compose.yaml`, `.dockerignore`
- `.env.example` (never a real `.env`)
- `pyproject.toml`, `requirements.txt`, `requirements-dev.txt`
- `invoice_extractor/` (application source)
- `scripts/` (launchers + release builder)
- `README.md`, `docs/OPERATIONS.md`, `docs/TROUBLESHOOTING.md`,
  `docs/DEPLOYMENT.md`, `docs/RELEASE.md`
- committed synthetic `benchmark/examples/`

**Never** included: `.env`, API keys, real PDFs, output files, logs, usage
CSVs, provider responses, real benchmark ground truth, debug artifacts,
`.venv`, or Git history other than what `git archive` embeds.

## Building a clean archive

```bash
./scripts/build-release.sh                 # refuses a dirty working tree
./scripts/build-release.sh --allow-dirty   # override (not recommended)
```

This runs `git archive HEAD`, so **only git-tracked files** are included — any
git-ignored `.env`/PDFs/outputs/real-data can never leak in. It writes
`release/invoice-extractor-<version>.tar.gz` and prints its **SHA-256**:

```
Built .../release/invoice-extractor-0.1.0.tar.gz
SHA-256: <hex>
```

Record the checksum alongside the archive for verification.

## Regenerating pinned dependencies (intentional updates)

`requirements.txt` is an exact lock built from a **clean** Python 3.13 env
installing only the direct runtime deps from `pyproject.toml`:

```bash
python3.13 -m venv /tmp/lock
/tmp/lock/bin/pip install \
  pymupdf google-genai anthropic httpx pandas openpyxl \
  python-dotenv click tenacity pydantic
/tmp/lock/bin/pip freeze --all | grep -vE '^(pip|setuptools|wheel)==' | sort > /tmp/pins
# prepend the explanatory header, then replace requirements.txt with header + /tmp/pins
```

Do **not** freeze the development `.venv` wholesale. After any change:

```bash
python -m pytest                 # full offline suite must stay green
docker compose build             # image must build from the new lock
docker compose run --rm invoice-extractor doctor
```

## Release checklist

1. Bump `__version__` if needed; update `compose.yaml`/`Dockerfile` image tag
   and OCI `version` label to match.
2. `python -m pytest` — full offline suite green.
3. Clean native install test in a fresh venv (see `docs/DEPLOYMENT.md` §17).
4. `docker compose build` and `docker compose run --rm invoice-extractor doctor`.
5. Inspect the image for forbidden files:
   `docker run --rm --entrypoint sh invoice-extractor:0.1.0 -c 'ls -a /app'`.
6. `./scripts/build-release.sh` on a clean tree; record the SHA-256.
7. Hand the archive + checksum to the pilot user with `docs/DEPLOYMENT.md`.

## What this milestone does NOT do

No Git tag, no GitHub release, no registry push, no auto-updater, no public
distribution. Those remain explicit manual, approval-gated steps.
