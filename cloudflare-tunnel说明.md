# Cloudflare 命名隧道部署

## 服务器

```bash
sudo bash cloudflare-tunnel-install.sh
sudo nano /etc/super-agent/cloudflared.env
sudo systemctl restart cloudflared-super-agent
sudo systemctl status cloudflared-super-agent --no-pager
```

在 Cloudflare Zero Trust → Networks → Tunnels 创建命名隧道，Public Hostname 指向：

```text
http://127.0.0.1:8000
```

使用 Cloudflare 分配的固定 hostname；`trycloudflare.com` 快速隧道不保证永久固定。

## 校验

```bash
sudo systemctl is-active super-agent-api cloudflared-super-agent
curl -sS http://127.0.0.1:8000/health
API_KEY=$(sudo sed -n 's/^GUICHAN_API_KEY=//p' /etc/super-agent/super-agent.env)
curl -i -sS -H "X-API-Key: $API_KEY" -H 'Content-Type: application/json' \\
  -d '{"code":"sh600000"}' http://127.0.0.1:8000/analyze
```

公网调用使用 Cloudflare hostname，并携带 `X-API-Key`。API Key 与隧道 token 均只保存在服务器，禁止提交 Git 或发给调用方。UUMit 回调也必须能够传递该请求头；若 UUMit 不支持自定义 Header，需要改为随机私有路径或 Cloudflare Access 服务令牌。
