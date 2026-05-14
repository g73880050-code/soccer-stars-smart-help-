#!/usr/bin/env bash
# ============================================================================
# clean.sh — Wipe ALL Buildozer / Android build caches
# ============================================================================
#
# Run this before a guaranteed-clean build:
#
#   chmod +x clean.sh
#   ./clean.sh
#
# What it removes
# ---------------
#   .buildozer/   — Buildozer work dir: downloaded SDK/NDK, compiled recipes,
#                   APK staging, Gradle cache.
#   bin/          — Previously built APK files.
#   *.log         — Stale build logs.
#   __pycache__/  — Python bytecode caches (all subdirs).
#   *.pyc         — Any stray compiled bytecode files.
#
# After running this script, the next `buildozer android debug` will:
#   1. Re-download the Android SDK and NDK (~1.5 GB total, one-time).
#   2. Recompile all python-for-android recipes from source (~30-60 min).
#   3. Produce a freshly linked APK in ./bin/.
# ============================================================================

set -euo pipefail

echo ""
echo "========================================"
echo "  Soccer Stars Analyzer — Clean Build   "
echo "========================================"
echo ""

# Confirm before wiping
read -rp "This will DELETE .buildozer/, bin/, logs, and all caches. Continue? [y/N] " confirm
case "$confirm" in
  [yY][eE][sS]|[yY]) echo "" ;;
  *) echo "Aborted."; exit 0 ;;
esac

# ---------------------------------------------------------------------------
# Buildozer working directory
# ---------------------------------------------------------------------------
if [ -d ".buildozer" ]; then
  echo "[1/5] Removing .buildozer/ ..."
  rm -rf .buildozer
  echo "      Done."
else
  echo "[1/5] .buildozer/ not found — skipping."
fi

# ---------------------------------------------------------------------------
# Built APKs
# ---------------------------------------------------------------------------
if [ -d "bin" ]; then
  echo "[2/5] Removing bin/ ..."
  rm -rf bin
  echo "      Done."
else
  echo "[2/5] bin/ not found — skipping."
fi

# ---------------------------------------------------------------------------
# Build logs
# ---------------------------------------------------------------------------
LOG_COUNT=$(find . -maxdepth 2 -name "*.log" | wc -l)
if [ "$LOG_COUNT" -gt 0 ]; then
  echo "[3/5] Removing $LOG_COUNT log file(s) ..."
  find . -maxdepth 2 -name "*.log" -delete
  echo "      Done."
else
  echo "[3/5] No log files found — skipping."
fi

# ---------------------------------------------------------------------------
# Python bytecode caches
# ---------------------------------------------------------------------------
CACHE_COUNT=$(find . -type d -name "__pycache__" | wc -l)
PYC_COUNT=$(find . -name "*.pyc" | wc -l)
if [ "$CACHE_COUNT" -gt 0 ] || [ "$PYC_COUNT" -gt 0 ]; then
  echo "[4/5] Removing $CACHE_COUNT __pycache__/ dirs and $PYC_COUNT .pyc files ..."
  find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
  find . -name "*.pyc" -delete 2>/dev/null || true
  echo "      Done."
else
  echo "[4/5] No Python caches found — skipping."
fi

# ---------------------------------------------------------------------------
# Buildozer's global home cache (optional — comment out to keep it)
# This lives outside the project at ~/.buildozer
# ---------------------------------------------------------------------------
if [ -d "$HOME/.buildozer" ]; then
  echo "[5/5] Removing ~/.buildozer (global cache) ..."
  rm -rf "$HOME/.buildozer"
  echo "      Done."
else
  echo "[5/5] ~/.buildozer not found — skipping."
fi

echo ""
echo "========================================"
echo "  Clean complete. Ready for fresh build."
echo "  Run:  buildozer android debug"
echo "========================================"
echo ""
