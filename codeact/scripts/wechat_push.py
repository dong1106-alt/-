#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Server酱(ServerChan) 微信推送：密钥优先读取 config/secrets.yaml（gitignored），
否则回退 config/settings.yaml 的 wechat_push.sckey。未启用或 sckey 为空时跳过。"""
import sys
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "settings.yaml"
SECRETS_PATH = ROOT / "config" / "secrets.yaml"
PUSH_URL = "https://sctapi.ftqq.com/{sckey}.send"


def _read_wechat_section(path):
    import yaml
    with open(path, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return (cfg.get("wechat_push") or {}) or {}


def _load_wechat_config():
    try:
        merged = {}
        env_key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
        if env_key:
            merged.update({"enable": True, "sckey": env_key})
        if SECRETS_PATH.exists():
            for key, value in _read_wechat_section(SECRETS_PATH).items():
                merged.setdefault(key, value)
        if CONFIG_PATH.exists():
            for key, value in _read_wechat_section(CONFIG_PATH).items():
                merged.setdefault(key, value)
        return merged
    except Exception as exc:
        print(f"[wechat_push] 读取配置失败: {exc}")
        return {}


def wechat_push(title, desp, force=False):
    """发送一条微信推送。返回 True=已发送；False=跳过或失败。"""
    wp = _load_wechat_config()
    enable = bool(wp.get("enable"))
    sckey = str(wp.get("sckey") or "").strip()
    if not force and (not enable or not sckey):
        state = f"enable={enable}, sckey={'已填' if sckey else '空'}"
        print(f"[wechat_push] 未启用或未配置 ({state}) -> 跳过")
        return False
    if not sckey:
        print("[wechat_push] sckey 为空 -> 跳过")
        return False
    if os.environ.get("WECHAT_PUSH_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}:
        print("[wechat_push] dry-run -> 跳过实际发送")
        return True
    try:
        import requests
        resp = requests.post(PUSH_URL.format(sckey=sckey),
                             data={"title": title, "desp": desp}, timeout=20)
        ok = resp.status_code == 200
        print(f"[wechat_push] {'发送成功' if ok else '发送失败'} HTTP {resp.status_code}")
        if not ok:
            print((resp.text or "")[:500])
        return ok
    except Exception as exc:
        print(f"[wechat_push] 发送异常: {exc}")
        return False


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        wechat_push(sys.argv[1], sys.argv[2], force=True)
    else:
        wechat_push("超级智能体测试", "这是一条来自本机的测试消息", force=False)
