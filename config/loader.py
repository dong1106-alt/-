# -*- coding: utf-8 -*-
"""统一配置加载(本地版): 读 config/settings.yaml, 路径锚定到项目根目录"""
import os
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parent.parent
_CONFIG = None
DATA_ROOT_ENV = "SUPER_AGENT_DATA_ROOT"


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

    data_root_value = os.environ.get(DATA_ROOT_ENV)
    if data_root_value:
        data_root = Path(data_root_value)
        if not data_root.is_absolute():
            data_root = ROOT / data_root
        data_root = data_root.resolve()
        paths.update({
            "data_dir": str(data_root),
            "stock_data_dir": str(data_root / "stocks"),
            "raw_data_dir": str(data_root / "raw"),
            "index_data_dir": str(data_root / "index"),
            "cache_dir": str(data_root / "cache"),
            "output_dir": str(data_root / "reports"),
            "log_file": str(data_root / "logs" / "system.log"),
            "state_file": str(data_root / "market_state_timeline.json"),
            "params_file": str(data_root / "optimal_params.json"),
            "params_history": str(data_root / "params_history.json"),
            "deviation_log": str(data_root / "deviation_log.json"),
            "signal_log": str(data_root / "signal_history.json"),
            "opt_log_dir": str(data_root / "logs" / "opt"),
        })
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
