#!/usr/bin/env bash
# Per-page classification report for ./input (no API calls, nothing written).
#   ./scripts/classify-invoices.sh
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

ensure_dirs
compose run --rm invoice-extractor classify --input /data/input "$@"
