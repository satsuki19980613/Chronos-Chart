"""pywebview から JavaScript に公開する API。

JS 側では window.pywebview.api.<メソッド名>() で呼び出す。
戻り値はすべて {"ok": bool, "data" | "error": ...} の形にそろえる。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from functools import wraps
from pathlib import Path

from .fetcher import FetchError
from .service import StockService

log = logging.getLogger(__name__)


def _response(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return {"ok": True, "data": func(*args, **kwargs)}
        except (FetchError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            log.exception("api error in %s", func.__name__)
            return {"ok": False, "error": f"予期しないエラーが発生しました: {exc}"}

    return wrapper


class Api:
    def __init__(self, service: StockService):
        # 先頭が _ の属性は JS に公開されない
        self._service = service

    @_response
    def search(self, query: str):
        return self._service.search(query)

    @_response
    def register(self, symbol: str, name: str, exchange: str | None = None):
        return self._service.register(symbol, name, exchange)

    @_response
    def update(self, symbol: str):
        return self._service.update(symbol)

    @_response
    def update_all(self):
        return self._service.update_all()

    @_response
    def delete(self, symbol: str):
        self._service.delete(symbol)
        return True

    @_response
    def list_stocks(self):
        return self._service.list_stocks()

    @_response
    def dashboard(self, symbol: str):
        return self._service.dashboard(symbol)

    @_response
    def open_csv_folder(self):
        folder = Path(self._service.csv_dir)
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(folder)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return str(folder)
