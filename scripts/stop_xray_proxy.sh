#!/usr/bin/env bash
# 停止 setup_xray_proxy.sh 启动的本地代理。
set -euo pipefail

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PID_FILE="${PROXY_DIR}/xray.pid"

if [[ -f "${PID_FILE}" ]]; then
	echo "[INFO] Stopping xray proxy (pid $(cat "${PID_FILE}"))"
	kill "$(cat "${PID_FILE}")" 2>/dev/null || true
	rm -f "${PID_FILE}"
fi
