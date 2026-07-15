#!/usr/bin/env bash
# Shared helpers for the operator launcher scripts (M8). Sourced, not run.
set -euo pipefail

# Deployment root = the directory that CONTAINS scripts/ (space-safe).
_here="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
ROOT="$(cd "${_here}/.." >/dev/null 2>&1 && pwd)"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

require_env() {
  # .env must exist; we NEVER read or print its contents (no key ever echoed).
  [ -f "${ROOT}/.env" ] || die "no .env found at ${ROOT}/.env - copy .env.example to .env and add your key(s)."
}

ensure_dirs() {
  mkdir -p "${ROOT}/input" "${ROOT}/output"
}

compose() {
  # Prefer the docker compose plugin; fall back to docker-compose if present.
  if docker compose version >/dev/null 2>&1; then
    ( cd "${ROOT}" && docker compose "$@" )
  elif command -v docker-compose >/dev/null 2>&1; then
    ( cd "${ROOT}" && docker-compose "$@" )
  else
    die "Docker Compose not found - install Docker Desktop or the compose plugin."
  fi
}
