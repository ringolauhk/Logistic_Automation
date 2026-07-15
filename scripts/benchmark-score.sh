#!/usr/bin/env bash
# Offline benchmark scoring inside the container (no provider calls). Requires
# a manifest + ground truth under ./benchmark and an extraction workbook under
# ./output. Example:
#   ./scripts/benchmark-score.sh \
#     --manifest /data/benchmark/manifest.json \
#     --workbook /data/output/results.xlsx \
#     --usage /data/output/results.usage.csv \
#     --output /data/output/benchmark_report.xlsx
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

ensure_dirs
mkdir -p "${ROOT}/benchmark"
compose run --rm invoice-extractor benchmark score "$@"
