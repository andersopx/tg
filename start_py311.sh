#!/bin/bash
# Python 3.11 startup script for BTC Polymarket Bot
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"

echo "======================================"
echo "BTC Polymarket Bot v${VERSION:-unknown}"
echo "Python: $($PYTHON_BIN --version)"
echo "======================================"

# Check dependencies
$PYTHON_BIN -c "import py_clob_client; import telegram; import aiohttp; import numpy" 2>&1 || {
    echo "❌ Missing dependencies. Run: $PYTHON_BIN -m pip install -r requirements.txt"
    exit 1
}

echo "✅ All dependencies ready"
echo ""

# Run auto pipeline (preflight -> backtest -> replay -> main)
exec $PYTHON_BIN auto_pipeline.py "$@"
