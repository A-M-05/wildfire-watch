#!/usr/bin/env bash
set -euo pipefail

SCRAPER_DIR="$(cd "$(dirname "$0")/../functions/scraper" && pwd)"
BUILD_DIR="$SCRAPER_DIR/build"

rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR"

pip install -r "$SCRAPER_DIR/requirements.txt" -t "$BUILD_DIR" --quiet
cp "$SCRAPER_DIR"/*.py "$BUILD_DIR/"

echo "Built scraper Lambda package at $BUILD_DIR"
