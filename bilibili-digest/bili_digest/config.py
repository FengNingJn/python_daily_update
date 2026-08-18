from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是对象：{path}")
    return data


def load_config() -> dict[str, Any]:
    load_dotenv(ROOT / ".env")
    config_dir = Path(os.environ.get("BILIBILI_CONFIG_DIR", ROOT / "config"))
    data_root = Path(os.environ.get("BILIBILI_DATA_ROOT", ROOT))
    settings = _read_yaml(config_dir / "settings.yaml")
    creators = _read_yaml(config_dir / "creators.yaml")
    accounts = [str(item).strip() for item in creators.get("accounts", []) if str(item).strip()]
    if not accounts:
        raise ValueError("config/creators.yaml 未配置 accounts")
    if len(accounts) != len(set(accounts)):
        raise ValueError("config/creators.yaml 中存在重复 UID")

    cookie_path = Path(
        os.environ.get("BILIBILI_COOKIE_FILE", str(settings.get("cookie_file", "config/bilibili_cookies.txt")))
    )
    if not cookie_path.is_absolute():
        cookie_path = data_root / cookie_path

    return {
        **settings,
        "accounts": accounts,
        "project_root": ROOT,
        "config_dir": config_dir,
        "root": data_root,
        "cookie_path": cookie_path,
        "db_path": data_root / "data" / "bilibili_digest.db",
        "state_path": data_root / "state" / "scheduler_state.json",
        "lock_path": data_root / "state" / "bilibili_digest.lock",
        "log_path": data_root / "logs" / "bilibili_digest.log",
        "deepseek_api_key": os.environ.get("DEEPSEEK_API_KEY", "").strip(),
        "deepseek_base_url": os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        "deepseek_model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat").strip(),
    }


def ensure_directories(config: dict[str, Any]) -> None:
    for name in ("data", "transcripts", "summaries", "digests", "state", "logs"):
        (config["root"] / name).mkdir(parents=True, exist_ok=True)
