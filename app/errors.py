"""アプリ共通の例外。"""

from __future__ import annotations


class UserFacingError(ValueError):
    """メッセージをそのまま画面に出してよいエラー。

    api._response は ValueError のメッセージをそのまま返すので、その規約に乗せる。
    """
