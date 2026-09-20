import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402  (sys.path を通してから import する)


def make_prices(closes, start="2025-01-06") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(closes)).strftime("%Y-%m-%d")
    return pd.DataFrame(
        {
            "date": dates,
            "open": closes,
            "high": closes + 1,
            "low": closes - 1,
            "close": closes,
            "volume": np.full(len(closes), 1000, dtype="int64"),
            "splits": 0.0,
        }
    )


@pytest.fixture
def prices_factory():
    return make_prices


@pytest.fixture(autouse=True)
def isolate_edinet_cache(tmp_path, monkeypatch):
    """EDINET の日次キャッシュの既定の置き場所を、テストごとの一時フォルダに向ける。

    `service.register()` はローカルのキャッシュを再走査する（SPEC §2.4.2）。既定のままだと
    テストが開発機の `data/edinet_cache/` を読みに行き、手元のデータの有無で結果が変わってしまう。
    `base_dir` を明示しているテストには影響しない。
    """
    monkeypatch.setattr(config, "EDINET_CACHE_DIR", tmp_path / "edinet_cache")
