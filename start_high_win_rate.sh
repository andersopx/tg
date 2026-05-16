#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
bash ./apply_high_win_rate_mode.sh
python3 -m compileall -q .
python3 -m pytest -q tests/test_v15_high_win_rate_guard.py tests/test_multi_asset.py::MultiAssetConfigTests::test_database_window_locks_are_per_asset tests/test_v14_2_product_doctor.py::test_product_doctor_reports_execution_cluster_without_api_touch || true
exec bash ./start.sh "$@"
