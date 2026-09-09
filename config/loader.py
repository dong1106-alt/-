# -*- coding: utf-8 -*-
"""统一配置加载(本地版): 读 config/settings.yaml, 路径锚定到项目根目录"""
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
_CONFIG = None


def _ensure_dir(p: Path):
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass


def load_config():
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG
    cfg_path = ROOT / "config" / "settings.yaml"
    with open(cfg_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    paths = cfg.setdefault("paths", {})

    def anchor(key):
        v = paths.get(key)
        if v is None:
            paths[key] = str(ROOT)
            return
        p = Path(str(v))
        if not p.is_absolute():
            p = ROOT / p
        paths[key] = str(p)

    for k in list(paths.keys()):
        anchor(k)
    # 确保关键目录存在
    for dk in ("data_dir", "stock_data_dir", "raw_data_dir", "index_data_dir",
               "cache_dir", "output_dir", "opt_log_dir"):
        if dk in paths:
            _ensure_dir(Path(paths[dk]))
    if "log_file" in paths:
        _ensure_dir(Path(paths["log_file"]).parent)
    _CONFIG = cfg
    return cfg


def get_config():
    return load_config()


def get_path(name: str) -> str:
    return load_config()["paths"][name]
