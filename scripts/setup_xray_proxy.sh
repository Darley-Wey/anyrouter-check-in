#!/usr/bin/env bash
# 通过 xray-core 将 VLESS 分享链接转为本地 HTTP 代理并探测可用性。
# 环境变量:
#   PROXY_VLESS_URL   vless:// 分享链接（必填才启用）
#   PROXY_TEST_URL    探测目标，默认 https://www.google.com/generate_204
#   PROXY_REQUIRED    true 时探测失败则退出 1
#   PROXY_PORT        本地 HTTP 代理端口，默认 7890
#   XRAY_VERSION      xray-core 版本，默认 v25.9.11

set -euo pipefail

PROXY_REQUIRED="${PROXY_REQUIRED:-false}"
if [[ -z "${PROXY_VLESS_URL:-}" ]]; then
	if [[ "${PROXY_REQUIRED}" == 'true' ]]; then
		echo '[FAILED] PROXY_VLESS_URL is required but not set'
		exit 1
	fi
	echo '[INFO] PROXY_VLESS_URL not set, skip proxy setup'
	exit 0
fi

PROXY_DIR="${RUNNER_TEMP:-/tmp}/checkin-proxy"
PROXY_PORT="${PROXY_PORT:-7890}"
PROXY_TEST_URL="${PROXY_TEST_URL:-https://www.google.com/generate_204}"
XRAY_VERSION="${XRAY_VERSION:-v25.9.11}"

mkdir -p "${PROXY_DIR}"
cd "${PROXY_DIR}"

echo "[INFO] Downloading xray-core ${XRAY_VERSION}..."
ARCHIVE='Xray-linux-64.zip'
if ! curl --retry 3 --retry-delay 5 --retry-all-errors -fsSL -o "${ARCHIVE}" \
	"https://github.com/XTLS/Xray-core/releases/download/${XRAY_VERSION}/${ARCHIVE}"; then
	echo "[WARN] Failed to download xray-core ${XRAY_VERSION}, skip proxy setup"
	if [[ "${PROXY_REQUIRED}" == 'true' ]]; then
		exit 1
	fi
	exit 0
fi
unzip -o -q "${ARCHIVE}" xray
chmod +x xray
XRAY_BIN="${PROXY_DIR}/xray"

# 解析 vless://uuid@host:port?query#tag 为 xray outbound 配置。
echo "[INFO] Parsing VLESS share link..."
if ! PROXY_VLESS_URL="${PROXY_VLESS_URL}" PROXY_PORT="${PROXY_PORT}" python3 - <<'PY' > config.json; then
import json
import os
import sys
from urllib.parse import parse_qs, unquote, urlparse

raw = os.environ['PROXY_VLESS_URL'].strip()
if not raw.startswith('vless://'):
	print(f'[FAILED] Only vless:// links are supported, got: {raw[:12]}...', file=sys.stderr)
	sys.exit(1)

parsed = urlparse(raw)
uuid = unquote(parsed.username or '')
host = parsed.hostname
port = parsed.port
if not (uuid and host and port):
	print('[FAILED] VLESS link missing uuid/host/port', file=sys.stderr)
	sys.exit(1)

query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
network = query.get('type', 'tcp')
security = query.get('security', 'none')
# SNI 优先级: sni > host > 连接域名，确保 CDN 场景下握手正确。
sni = query.get('sni') or query.get('host') or host

stream: dict = {'network': network, 'security': security}
if security == 'tls':
	stream['tlsSettings'] = {'serverName': sni, 'allowInsecure': False}
	if query.get('fp'):
		stream['tlsSettings']['fingerprint'] = query['fp']
	if query.get('alpn'):
		stream['tlsSettings']['alpn'] = query['alpn'].split(',')
if network == 'ws':
	stream['wsSettings'] = {
		'path': unquote(query.get('path', '/')),
		'headers': {'Host': query.get('host') or sni},
	}
elif network == 'grpc':
	stream['grpcSettings'] = {'serviceName': query.get('serviceName', '')}

config = {
	'log': {'loglevel': 'warning'},
	'inbounds': [
		{
			'listen': '127.0.0.1',
			'port': int(os.environ['PROXY_PORT']),
			'protocol': 'http',
			'sniffing': {'enabled': True, 'destOverride': ['http', 'tls']},
		}
	],
	'outbounds': [
		{
			'protocol': 'vless',
			'settings': {
				'vnext': [
					{
						'address': host,
						'port': port,
						'users': [
							{
								'id': uuid,
								'encryption': query.get('encryption', 'none'),
								'flow': query.get('flow', ''),
							}
						],
					}
				]
			},
			'streamSettings': stream,
		}
	],
}
print(json.dumps(config, indent=2))
print(f'[INFO] Outbound: {host}:{port} network={network} security={security} sni={sni}', file=sys.stderr)
PY
	echo '[FAILED] Failed to parse PROXY_VLESS_URL'
	if [[ "${PROXY_REQUIRED}" == 'true' ]]; then
		exit 1
	fi
	exit 0
fi

if ! "${XRAY_BIN}" run -test -config config.json; then
	echo '[FAILED] xray config validation failed'
	if [[ "${PROXY_REQUIRED}" == 'true' ]]; then
		exit 1
	fi
	exit 0
fi

echo "[INFO] Starting xray on 127.0.0.1:${PROXY_PORT}..."
nohup "${XRAY_BIN}" run -config config.json > xray.log 2>&1 &
echo $! > xray.pid

PROXY_URL="http://127.0.0.1:${PROXY_PORT}"
READY=false
for attempt in $(seq 1 30); do
	if curl -fsS -x "${PROXY_URL}" --max-time 20 "${PROXY_TEST_URL}" -o /dev/null 2>/dev/null; then
		READY=true
		break
	fi
	echo "[INFO] Waiting for proxy health check (${attempt}/30)..."
	sleep 2
done

if [[ "${READY}" != 'true' ]]; then
	echo "[FAILED] Proxy health check failed for ${PROXY_TEST_URL}"
	tail -n 30 xray.log || true
	if [[ -f xray.pid ]]; then
		kill "$(cat xray.pid)" 2>/dev/null || true
		rm -f xray.pid
	fi
	if [[ "${PROXY_REQUIRED}" == 'true' ]]; then
		exit 1
	fi
	exit 0
fi

echo "[SUCCESS] Proxy is ready: ${PROXY_URL}"
echo '[INFO] Proxy is scoped to CHECKIN_PROXY_URL (browser/python only, not global HTTP_PROXY)'
if [[ -n "${GITHUB_ENV:-}" ]]; then
	echo "CHECKIN_PROXY_URL=${PROXY_URL}" >> "${GITHUB_ENV}"
fi
