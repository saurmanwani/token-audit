#!/usr/bin/env bash
set -eu
# Generates a synthetic config directory; timestamps are relative to now.
python3 "$(dirname "$0")/../../scripts/make_demo_logs.py" wasteful-agents ./claude-config
