#!/usr/bin/env bash
# Extract every PDF in ./input to ./output/results.xlsx via the container.
#
#   ./scripts/run-invoices.sh              # refuses to overwrite existing output
#   ./scripts/run-invoices.sh --overwrite  # replace existing output (explicit)
#
# Additional invoice-extractor flags are passed through, e.g.:
#   ./scripts/run-invoices.sh --run-metadata /data/output/results.run.json
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/_common.sh"

require_env
ensure_dirs

# All args are forwarded verbatim; --overwrite only reaches the CLI if the
# operator actually typed it (no silent overwrite).
compose run --rm invoice-extractor run \
  --input /data/input \
  --output /data/output/results.xlsx \
  "$@"
