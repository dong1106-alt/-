#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then echo '请使用 sudo bash cloudflare-tunnel-install.sh'; exit 1; fi
APP_DIR=/opt/super-agent
ENV_FILE=/etc/super-agent/super-agent.env

if ! command -v cloudflared >/dev/null 2>&1; then
  curl -L --fail --output /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  dpkg -i /tmp/cloudflared.deb
fi

install -d -m 750 /etc/super-agent
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"
grep -q '^GUICHAN_API_KEY=' "$ENV_FILE" || printf 'GUICHAN_API_KEY=%s\n' "$(openssl rand -hex 32)" >> "$ENV_FILE"

cat >/etc/systemd/system/super-agent-api.service <<EOF
[Unit]
After=network-online.target
[Service]
User=superagent
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/python -u $APP_DIR/api_service/server.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/cloudflared-super-agent.service <<'EOF'
[Unit]
After=network-online.target
[Service]
EnvironmentFile=/etc/super-agent/cloudflared.env
ExecStart=/usr/bin/cloudflared tunnel run --token ${CLOUDFLARED_TUNNEL_TOKEN}
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

if [[ ! -f /etc/super-agent/cloudflared.env ]]; then
  cat >/etc/super-agent/cloudflared.env <<'EOF'
# 在 Cloudflare Zero Trust 创建命名隧道后填写：
CLOUDFLARED_TUNNEL_TOKEN=
EOF
  chmod 600 /etc/super-agent/cloudflared.env
fi
systemctl daemon-reload
systemctl enable super-agent-api cloudflared-super-agent
systemctl restart super-agent-api
echo 'API 已启用。请将 Cloudflare 隧道 token 写入 /etc/super-agent/cloudflared.env 后执行：'
echo 'sudo systemctl restart cloudflared-super-agent'
