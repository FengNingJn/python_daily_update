#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
from pathlib import Path

from bili_digest.analysis import AnalysisError
from bili_digest.bilibili import AuthenticationRequired, BilibiliClient, BilibiliError
from bili_digest.config import ensure_directories, load_config
from bili_digest.database import connect
from bili_digest.pipeline import DigestPipeline


def build_logger(config) -> logging.Logger:
    logger = logging.getLogger("bilibili_digest")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    file_handler = logging.FileHandler(config["log_path"], encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def doctor(config, as_json: bool = False) -> int:
    client = BilibiliClient(config)
    auth = client.auth_status()
    checks = {
        "project_root": str(config["root"]),
        "database": str(config["db_path"]),
        "accounts": config["accounts"],
        "account_count": len(config["accounts"]),
        "cookie_file": auth["cookie_path"],
        "cookie_file_exists": auth["cookie_file_exists"],
        "bilibili_sessdata": auth["has_sessdata"],
        "deepseek_configured": bool(config.get("deepseek_api_key")),
        "faster_whisper_installed": importlib.util.find_spec("faster_whisper") is not None,
    }
    if as_json:
        print(json.dumps(checks, ensure_ascii=False, indent=2))
    else:
        print("B站观点汇总环境检查")
        print(f"- 跟踪UP主：{checks['account_count']} 个")
        print(f"- SQLite：{checks['database']}")
        print(f"- B站Cookie：{'已配置' if checks['bilibili_sessdata'] else '未配置，需要联系用户'}")
        print(f"- DeepSeek：{'已配置' if checks['deepseek_configured'] else '未配置'}")
        print(f"- faster-whisper：{'已安装' if checks['faster_whisper_installed'] else '未安装，仅在B站无字幕时需要'}")
    return 0 if checks["bilibili_sessdata"] and checks["deepseek_configured"] else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="B站UP主增量观点汇总：SQLite + Markdown")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化目录和SQLite表")
    doctor_parser = sub.add_parser("doctor", help="检查Cookie、DeepSeek与Whisper环境")
    doctor_parser.add_argument("--json", action="store_true")
    run_parser = sub.add_parser("run", help="执行一个定时时段")
    run_parser.add_argument("--slot", required=True, choices=["08:00", "21:00"])
    backfill_parser = sub.add_parser("backfill", help="全量回填公开视频、字幕或本地Whisper转写")
    backfill_parser.add_argument(
        "--phase", required=True, choices=["inventory", "subtitles", "whisper", "all"]
    )
    backfill_parser.add_argument("--limit", type=int, default=None, help="本轮最多处理多少个分P；默认不限")

    args = parser.parse_args()
    config = load_config()
    ensure_directories(config)
    with connect(config["db_path"]):
        pass

    if args.command == "init":
        print(f"初始化完成：{config['root']}")
        print(f"数据库：{config['db_path']}")
        return 0
    if args.command == "doctor":
        return doctor(config, args.json)

    logger = build_logger(config)
    try:
        pipeline = DigestPipeline(config, logger)
        if args.command == "backfill":
            result = pipeline.backfill(args.phase, limit=args.limit)
        else:
            result = pipeline.run(args.slot)
    except AuthenticationRequired as exc:
        logger.error("需要用户提供B站Cookie：%s", exc)
        print("AUTH_REQUIRED：需要你提供有效的B站Cookie后才能继续。")
        return 2
    except (BilibiliError, AnalysisError, RuntimeError) as exc:
        logger.error("任务失败：%s", exc)
        print(f"FAILED：{exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
