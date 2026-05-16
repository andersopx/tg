#!/usr/bin/env bash
set -euo pipefail
PKG="${1:-btc_bot_v15_1_3_full_project.tar.gz}"
echo "FILE=$PKG"
ls -lh "$PKG"
echo "sha256=$(sha256sum "$PKG" | awk '{print $1}')"
echo "tar_count=$(tar -tzf "$PKG" | wc -l)"
echo "top_50:"
tar -tzf "$PKG" | head -50
