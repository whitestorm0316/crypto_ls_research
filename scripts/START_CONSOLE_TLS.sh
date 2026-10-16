#!/usr/bin/env bash
# 从外网访问控制台 —— 必须带 TLS（明文绑定非本机时服务会拒绝启动）。
#
# 用法：
#   1) 先生成一张证书（自签即可，浏览器会报"不安全"，手工确认一次）：
#        openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
#          -keyout ls-key.pem -out ls-cert.pem -subj '/CN=<你的域名或IP>'
#   2) 改下面的 LS_HOST 为你想监听的地址（0.0.0.0 = 所有网卡）
#   3) ./scripts/START_CONSOLE_TLS.sh
#
# ⚠️ 登进来之后能下真钱单。绑 0.0.0.0 之前确认：
#    * 防火墙/安全组只放行你需要的来源 IP；
#    * 不要用默认弱口令（首次设置那一步会要求至少 8 位）。
set -euo pipefail
cd "$(dirname "$0")/.."

export LS_HOST="${LS_HOST:-0.0.0.0}"
export LS_TLS_CERT="${LS_TLS_CERT:-./ls-cert.pem}"
export LS_TLS_KEY="${LS_TLS_KEY:-./ls-key.pem}"
PORT="${1:-8790}"

if [ ! -f "$LS_TLS_CERT" ] || [ ! -f "$LS_TLS_KEY" ]; then
  echo "缺少证书：$LS_TLS_CERT / $LS_TLS_KEY"
  echo "先生成一张自签证书："
  echo "  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \\"
  echo "    -keyout ls-key.pem -out ls-cert.pem -subj '/CN=<你的域名或IP>'"
  exit 1
fi

PY="$HOME/.workbuddy-ai/binaries/python/envs/default/bin/python"
[ -x "$PY" ] || PY=python3
echo "监听 https://$LS_HOST:$PORT  （证书 $LS_TLS_CERT）"
exec "$PY" -m webapp.server "$PORT"
