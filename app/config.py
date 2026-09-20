"""パス・定数の設定。"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

# 環境変数 CHRONOS_DATA_DIR でデータ保存先を変更できる
DATA_DIR = Path(os.environ.get("CHRONOS_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "chronos.db"
CSV_DIR = DATA_DIR / "csv"
LOG_DIR = DATA_DIR / "logs"
# EDINET の書類一覧（documents.json）の日次キャッシュ。銘柄で絞る前のものを置く（SPEC §2.4.2）
EDINET_CACHE_DIR = DATA_DIR / "edinet_cache"

# 出力タブ（AI 向けファイル）の保存先。環境変数 CHRONOS_OUTPUT_DIR で変更できる
OUTPUT_DIR = Path(os.environ.get("CHRONOS_OUTPUT_DIR", BASE_DIR / "output"))

# 初回登録時に取得する期間（yfinance の period 指定）
INITIAL_PERIOD = "1y"
