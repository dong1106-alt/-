#!/usr/bin/env bash
set -euo pipefail

# 在新服务器上运行：把超级智能体从旧服务器迁移过来（或全新部署）。
# 代码优先从 GitHub 私有仓库克隆（避免中文文件名损坏）；未提供 token 时回退为从旧服务器 rsync。
#
# 用法：
#   OLD_HOST=ubuntu@134.175.19.111 GITHUB_TOKEN=ghp_xxx bash scripts/migrate_server.sh
#   OLD_HOST=ubuntu@134.175.19.111 bash scripts/migrate_server.sh

BASE=/opt/super-agent
REPO=https://github.com/dong1106-alt/-.git
BRANCH=snapshot/current-source
OLD_HOST=${OLD_HOST:-}
GITHUB_TOKEN=${GITHUB_TOKEN:-}
VERSION=${VERSION:-$(date +%Y%m%d-%H%M%S)}

if [ -z "$OLD_HOST" ]; then
  echo "错误：必须设置 OLD_HOST（旧服务器 SSH 地址，如 ubuntu@134.175.19.111）" >&2
  exit 1
fi

echo "==> 1/10 安装基础依赖"
sudo apt update
sudo apt install -y python3-venv python3-pip rsync git caddy ufw

echo "==> 2/10 创建用户与目录"
sudo useradd --system --home "$BASE" --shell /usr/sbin/nologin superagent 2>/dev/null || true
sudo mkdir -p "$BASE/releases" "$BASE/data" "$BASE/logs" "$BASE/reports" /etc/super-agent

echo "==> 3/10 拉取代码"
RELEASE="$BASE/releases/$VERSION"
if [ -n "$GITHUB_TOKEN" ]; then
  sudo -u superagent git clone --branch "$BRANCH" \
    "https://x-access-token:${GITHUB_TOKEN}@github.com/dong1106-alt/-.git" "$RELEASE"
  # 克隆完成后抹掉 URL 中的 token，避免泄露到 git 配置里
  sudo -u superagent git -C "$RELEASE" remote set-url origin "$REPO"
else
  sudo -u superagent mkdir -p "$RELEASE"
  sudo rsync -avz -e ssh "$OLD_HOST:$BASE/" "$RELEASE/" \
    --exclude data --exclude logs --exclude reports --exclude .venv \
    --exclude releases --exclude current --exclude __pycache__
fi

echo "==> 4/10 运行时目录软链（代码按 __file__ 相对定位）"
for d in data logs reports .venv; do
  sudo ln -sfn "$BASE/$d" "$RELEASE/$d"
done

echo "==> 5/10 切换 current（原子）"
sudo ln -sfn "$RELEASE" "$BASE/current.next"
sudo mv -Tf "$BASE/current.next" "$BASE/current"

echo "==> 6/10 准备虚拟环境"
if [ ! -x "$BASE/.venv/bin/python" ]; then
  # 优先复用旧服务器 venv（同架构）；否则重建
  if ! sudo rsync -avz -e ssh "$OLD_HOST:$BASE/.venv/" "$BASE/.venv/" >/dev/null 2>&1; then
    sudo python3 -m venv "$BASE/.venv"
    sudo "$BASE/.venv/bin/pip" install -r "$BASE/current/requirements.txt"
  fi
fi
sudo chown -R superagent:superagent "$BASE"

echo "==> 7/10 搬运行数据（持仓/交易/任务状态/K线缓存）"
sudo rsync -avz -e ssh "$OLD_HOST:$BASE/data/" "$BASE/data/"

echo "==> 8/10 搬密钥与 Caddy 配置"
if [ ! -f /etc/super-agent/super-agent.env ]; then
  sudo rsync -avz -e ssh "$OLD_HOST:/etc/super-agent/super-agent.env" /etc/super-agent/
  sudo chmod 600 /etc/super-agent/super-agent.env
fi
if [ ! -f /etc/caddy/Caddyfile ]; then
  sudo rsync -avz -e ssh "$OLD_HOST:/etc/caddy/Caddyfile" /etc/caddy/
fi

echo "==> 9/10 安装 systemd 服务"
sudo cp -f "$BASE/current/super-agent-api.service" /etc/systemd/system/
sudo cp -f "$BASE/current/super-agent-scheduler.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now super-agent-api super-agent-scheduler caddy

echo "==> 10/10 放行防火墙"
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw --force enable

cat <<'DONE'
迁移完成。请手动完成：
1. 更新 DuckDNS A 记录指向本机公网 IP（token 勿提交）。
2. curl -I https://superquant-2026.duckdns.org/health 验证证书。
3. curl -fsS http://127.0.0.1:8000/health 验证 API。
回滚：把 DuckDNS 指回旧服务器 IP 即可，旧服务器未受影响。
DONE
