# Deployment guide

Two supported delivery modes:

1. **Docker / Docker Compose** — recommended for pilot users.
2. **Native Python virtual environment** — for developers/technical users.

There is also an optional **single-user pilot web UI** delivered as a separate
Docker service (`invoice-extractor-web`, built from the `web` stage of the same
Dockerfile). The default `docker build` and the `invoice-extractor` CLI image
are unchanged and never gain Streamlit. See §21 and `docs/WEB_UI.md`.

> **Privacy disclosure.** Extraction sends invoice page content (text and, for
> scanned pages, rendered page images) to the **configured external model
> provider** (OpenRouter, or Gemini/Claude under the direct gateway). Data is
> **not** fully local during extraction. Everything else — classification,
> rendering, aggregation, the workbook, usage CSV, logs — stays on your
> machine. No telemetry, no analytics, no automatic uploads other than the
> provider calls you configure.

## 1. Supported platforms

- macOS (Apple Silicon **arm64** and Intel **amd64**) via Docker Desktop.
- Linux (**amd64** / **arm64**).
- The image is built from a multi-arch base tag (`python:3.13.14-slim-bookworm`),
  so `docker build` produces a native image on either architecture.

## 2. Docker prerequisites

- Docker Engine 24+ (or Docker Desktop) with the Compose plugin (`docker compose`).
- No other system packages needed on the host. **Poppler is NOT required** —
  PDF rendering runs in-process via PyMuPDF.

## 3. First-time setup

```bash
cp .env.example .env            # then edit .env and add your key(s)
mkdir -p input output
docker compose build
docker compose run --rm invoice-extractor doctor    # offline; no paid calls
./scripts/run-invoices.sh                            # or the compose command below
```

## 4. `.env` setup

`.env` is supplied **at runtime** (Compose `env_file`) and is never baked into
an image layer or copied into the build context (`.dockerignore` excludes it).
Configure the gateway, key(s), model lists, chunk sizes, and safety limits —
see `.env.example` and `docs/OPERATIONS.md` §5. Never commit `.env`.

## 5. Build command

```bash
docker compose build                 # or: docker build -t invoice-extractor:0.1.0 .
```

The build makes **no** provider calls. Dependencies install from the pinned
`requirements.txt` (wheels only); the application installs from `pyproject.toml`.

## 6. Doctor (offline readiness)

```bash
./scripts/doctor.sh
# or:
docker compose run --rm invoice-extractor doctor --input /data/input --output /data/output
```

Confirms Python, packages, PyMuPDF, mounted-path read/write, gateway, **masked**
key presence, model lists, chunk/budget values, and debug-artifact status — with
zero provider calls. There is **no** Docker HEALTHCHECK that would trigger
provider requests.

## 7. Classify

```bash
./scripts/classify-invoices.sh
```

Per-page text/image/blank report; no API calls, nothing written.

## 8. Run extraction

```bash
./scripts/run-invoices.sh                 # refuses to overwrite existing output
./scripts/run-invoices.sh --overwrite     # explicit replace
# or directly:
docker compose run --rm invoice-extractor run \
  --input /data/input --output /data/output/results.xlsx
```

## 9. Input / output folders

| Host | Container | Mode |
|------|-----------|------|
| `./input`  | `/data/input`  | read-only (`:ro`) |
| `./output` | `/data/output` | writable |
| `./benchmark` | `/data/benchmark` | optional |

Outputs (`results.xlsx`, `results.usage.csv`, optional run metadata) land in
`./output` on the host. The read-only input mount does not break classification
or rendering — rendering reads input and writes only to `/data/output`.

## 10. Overwrite behavior

Runs **refuse** (before any provider call) if any planned output already
exists; pass `--overwrite` to replace. See `docs/OPERATIONS.md` §11.

## 11. Safe Ctrl+C

`tini` is PID 1 in the container and forwards `SIGINT`, so Ctrl+C reaches
Python: new calls/retries stop, a valid partial workbook + usage CSV is written
if ≥1 file completed, and the container exits **130**. Compose sets
`stop_signal: SIGINT` and a 30s grace period.

## 12. Cost and chunk guidance

See `docs/OPERATIONS.md` §14–15 (paid-call formula, dense-scan
`MAX_VISION_PAGES` pilot value of 1–2). Set `MAX_MODEL_ATTEMPTS_PER_FILE` /
`MAX_COST_USD_PER_FILE` / `MAX_COST_USD_PER_RUN` before real batches.

## 13. Updating

1. Back up `.env` (`cp .env .env.bak`).
2. Pull/replace the deployment package (`git pull` or replace the folder).
3. Rebuild: `docker compose build`.
4. Rerun doctor: `./scripts/doctor.sh`.
5. Run a small known sample and eyeball the workbook.
6. Keep old outputs (rebuilding the image never touches host-mounted
   `input/`/`output/`).

The tool never auto-migrates or edits your `.env`.

## 14. Rollback

Return to the prior release/tag/image:

```bash
git checkout <prior-tag>       # or restore the prior deployment folder
docker compose build
```

Host-mounted `input/`/`output/` are unaffected by image rebuilds, so prior
outputs remain available.

## 15. File permissions

The container runs **non-root** as `appuser` (UID:GID **1000:1000**).

- **macOS Docker Desktop**: the file-sharing layer maps ownership, so files in
  `./output` are owned by your host user automatically.
- **Linux**: if your host user isn't 1000, export your UID/GID so generated
  files are host-owned:
  ```bash
  export HOST_UID=$(id -u) HOST_GID=$(id -g)
  docker compose run --rm invoice-extractor run --input /data/input --output /data/output/results.xlsx
  ```
  Compose reads `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`. Atomic temp
  files are created and `os.replace`d **inside** `/data/output`, so the same
  mount permission covers both temp and final.

If `/data/output` isn't writable by the runtime user, `doctor` reports it as
`not writable` with an actionable message — fix host directory permissions or
pass the correct UID/GID.

## 16. Privacy / provider disclosure

See the disclosure box at the top. Keep `SAVE_DEBUG_ARTIFACTS=false` (default)
in shared environments — enabling it persists failed provider responses, which
may contain full invoice contents. Debug artifacts are off by default and are
never written into the image.

## 17. Native installation (developers)

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .   # editable dev install
invoice-extractor --version
invoice-extractor doctor
python -m pytest                                          # full offline suite
```

Production-style install from the pinned lock:

```bash
pip install -r requirements.txt && pip install --no-deps .
```

## 18. Release packaging

See `docs/RELEASE.md` (`scripts/build-release.sh` → `tar.gz` of git-tracked
files only, SHA-256 printed, refuses a dirty tree).

## 19. Troubleshooting Docker build & permissions

| Symptom | Fix |
|---------|-----|
| `no .env found` (launcher) | `cp .env.example .env` and add your key |
| build can't find a wheel | ensure host arch is amd64/arm64; the base is multi-arch |
| output files owned by root/1000 on Linux | export `HOST_UID`/`HOST_GID` (see §15) |
| `/data/output ... not writable` | fix host dir perms or pass correct UID/GID |
| Ctrl+C didn't stop cleanly | ensure you used the image ENTRYPOINT (tini); don't `--entrypoint` around it |

More at `docs/TROUBLESHOOTING.md`.

## 20. No secrets or PDFs in Git / images

`.gitignore` keeps `.env`, `output/`, real samples/benchmark data, usage CSVs,
and logs out of Git. `.dockerignore` keeps all of those **plus** tests and
`.git` out of the image. Verify with `git status` before committing and inspect
the image with `docker run --rm --entrypoint sh invoice-extractor:0.1.0 -c 'ls -a /app'`.

## 21. Pilot web UI service

```bash
docker compose build invoice-extractor-web     # image invoice-extractor-web:0.1.0
docker compose up invoice-extractor-web        # http://localhost:8501
```

- Separate image built from the `web` stage (`--target web`); the CLI stage is
  the Dockerfile's final/default stage, so plain `docker build .` still
  produces the CLI image.
- The web service publishes host port `8501:8501` (all interfaces) so pilot
  users on the trusted LAN reach it at `http://<host>:8501`. For off-LAN
  pilots prefer `tailscale serve 8501` (authenticated, private); for
  single-machine use rebind to `127.0.0.1:8501:8501`. Never expose it to the
  public internet.
- Job storage is `./web-data` on the host (`/data/jobs` in the container),
  git-ignored and excluded from build contexts; jobs are deleted after
  `WEB_JOB_RETENTION_HOURS` (default 24).
- Runs as the same non-root user with `user: "${HOST_UID:-1000}:${HOST_GID:-1000}"`.
- No login, no telemetry (`gatherUsageStats = false`), single active job.
- Optional feature flag `TRANSFER_WORKFLOW_ENABLED=true` (in `.env`) adds the
  Transfer Note Packing List workflow selector (see
  `docs/transfer_packing/FUNCTIONAL_SPEC.md`). Default off.
- The Transfer workflow's product lookup (Build 5) uses BACKEND-ONLY
  `API_GATEWAY_*` and `PRODUCT_LOOKUP_*` variables (see `.env.example`):
  keep credentials in the server's `.env` only — never in Docker image
  layers, never in the browser. Gateway tokens are held in process memory
  per container and are never persisted; restarting a container simply
  re-authenticates on the next lookup. Packing preparation (Build 6) is
  fully local and configured by the optional `PACKING_*` variables (see
  `.env.example`).

Full usage, limits, cancellation, retention, and remote-access guidance:
`docs/WEB_UI.md`.
