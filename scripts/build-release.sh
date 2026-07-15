#!/usr/bin/env bash
# Build a clean deployment archive from GIT-TRACKED files only (M8).
#
#   ./scripts/build-release.sh              # refuses if the tree is dirty
#   ./scripts/build-release.sh --allow-dirty
#
# Produces release/invoice-extractor-<version>.tar.gz and prints its SHA-256.
# Because it archives `git archive HEAD`, forbidden files (.env, PDFs, output,
# logs, real benchmark data, .venv) that are git-ignored can NEVER be included.
# No Docker registry upload, no git tag, no publish.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

allow_dirty=0
[ "${1:-}" = "--allow-dirty" ] && allow_dirty=1

cd "${ROOT}"
command -v git >/dev/null 2>&1 || die "git is required to build a release archive."
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "not a git repository."

if [ "${allow_dirty}" -eq 0 ] && [ -n "$(git status --porcelain)" ]; then
  die "working tree is dirty; commit/stash first or pass --allow-dirty."
fi

version="$(python3 -c 'import re,sys; s=open("invoice_extractor/__init__.py").read(); m=re.search(r"__version__\s*=\s*\"([^\"]+)\"", s); print(m.group(1))')"
[ -n "${version}" ] || die "could not read __version__."

mkdir -p "${ROOT}/release"
archive="${ROOT}/release/invoice-extractor-${version}.tar.gz"
prefix="invoice-extractor-${version}/"

# git archive includes ONLY tracked files (never ignored .env/PDFs/output/etc).
git archive --format=tar.gz --prefix="${prefix}" -o "${archive}" HEAD

# Deterministic-ish name; print checksum for verification.
if command -v shasum >/dev/null 2>&1; then
  sum="$(shasum -a 256 "${archive}" | awk '{print $1}')"
else
  sum="$(sha256sum "${archive}" | awk '{print $1}')"
fi
printf 'Built %s\nSHA-256: %s\n' "${archive}" "${sum}"
