#!/usr/bin/env bash
set -euo pipefail

# Bundle the enrichment Lambda (#9) for deployment.
#
# The handler imports `noaa_poller` (issue #11) which lives in the scraper
# package, so we copy it alongside handler.py into the build dir. Lambda's
# default sys.path includes /var/task, so a flat layout means no path tricks
# at runtime.

ENRICH_DIR="$(cd "$(dirname "$0")/../functions/enrich" && pwd)"
SCRAPER_DIR="$(cd "$(dirname "$0")/../functions/scraper" && pwd)"
BUILD_DIR="$ENRICH_DIR/build"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

pip install -r "$ENRICH_DIR/requirements.txt" -t "$BUILD_DIR" --quiet
cp "$ENRICH_DIR"/*.py "$BUILD_DIR/"
cp "$SCRAPER_DIR/noaa_poller.py" "$BUILD_DIR/"

echo "Built enrich Lambda package at $BUILD_DIR"
