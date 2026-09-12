"""パス・定数の設定。"""

from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

# 環境変数 AUTOTECHNICAL_DATA_DIR でデータ保存先を変更できる
DATA_DIR = Path(os.environ.get("AUTOTECHNICAL_DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "autotechnical.db"
CSV_DIR = DATA_DIR / "csv"
LOG_DIR = DATA_DIR / "logs"

# 初回登録時に取得する期間（yfinance の period 指定）
INITIAL_PERIOD = "1y"
