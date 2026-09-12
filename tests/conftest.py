import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


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
