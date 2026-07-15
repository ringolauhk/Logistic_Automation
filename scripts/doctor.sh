#!/usr/bin/env bash
# Offline readiness check inside the container (no provider calls). Confirms
# Python, packages, PyMuPDF, mounted-path read/write, gateway, masked key
# presence, model lists, chunk/budget values, and debug-artifact status.
#   ./scripts/doctor.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_env
ensure_dirs
# Point doctor at the mounted volumes so it verifies the real read/write paths.
compose run --rm invoice-extractor doctor \
  --input /data/input --output /data/output "$@"
