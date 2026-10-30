#!/bin/bash
# promlightd.sh - Launchd wrapper for promlightd
#
# Uses $(dirname "$0") to locate the binary and config,
# so the whole project directory can be moved freely.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/promlightd" --idle-exit 1800 "$@"
