#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请使用 sudo bash install_ubuntu.sh 运行"
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR=/opt/super-agent
DOMAIN=superquant-2026.duckdns.org

apt-get update
apt-get install -y python3-venv python3-pip rsync unzip ufw curl ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null 2>&1; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt -o /etc/apt/sources.list.d/caddy-stable.list
  chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  chmod o+r /etc/apt/sources.list.d/caddy-stable.list
  apt-get update
  apt-get install -y caddy
fi

id superagent >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin superagent
mkdir -p "$APP_DIR" /etc/super-agent "$APP_DIR/logs" "$APP_DIR/reports" "$APP_DIR/data"
rsync -a --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' --exclude=logs --exclude=reports "$SOURCE_DIR/" "$APP_DIR/"
chown -R superagent:superagent "$APP_DIR"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

install -m 600 "$APP_DIR/super-agent.env.example" /etc/super-agent/super-agent.env
install -m 644 "$APP_DIR/super-agent-api.service" /etc/systemd/system/super-agent-api.service
install -m 644 "$APP_DIR/super-agent-scheduler.service" /etc/systemd/system/super-agent-scheduler.service

CALLBACK_PATH="uumit-$(openssl rand -hex 24)"
sed "s/CALLBACK_PATH/${CALLBACK_PATH}/g" "$APP_DIR/Caddyfile.example" > /etc/caddy/Caddyfile
chmod 644 /etc/caddy/Caddyfile
printf '%s\n' "$CALLBACK_PATH" > /etc/super-agent/callback-path
chmod 600 /etc/super-agent/callback-path

timedatectl set-timezone Asia/Shanghai

if ! swapon --show=NAME --noheadings | grep -qx /swapfile; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || printf '/swapfile none swap sw 0 0\n' >> /etc/fstab

systemctl daemon-reload
systemctl enable --now super-agent-api.service super-agent-scheduler.service caddy.service

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

echo "本机健康检查："
curl -fsS http://127.0.0.1:8000/health
echo
echo "部署完成。DuckDNS 生效后公网健康地址：https://${DOMAIN}/health"
echo "UUMit 回调地址：https://${DOMAIN}/${CALLBACK_PATH}"
echo "回调路径也已保存到 /etc/super-agent/callback-path"
