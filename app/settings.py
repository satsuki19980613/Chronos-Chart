"""設定の読み書きと API キーの管理（SPEC §2.1）。

API キー以外は settings テーブルに、API キーは OS の資格情報ストア（keyring）に保存する。
data/ がクラウド同期フォルダの配下に置かれることがあるため、キーを DB やファイルに平文で書かない。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import Database
from .errors import UserFacingError

log = logging.getLogger(__name__)

KEYRING_SERVICE = "ChronosChart"

# API キー: 設定キー -> 優先して読む環境変数
SECRET_ENV = {
    "edinet_api_key": "CHRONOS_EDINET_API_KEY",
    "gemini_api_key": "GEMINI_API_KEY",
}

# data/ がこの環境変数の指すフォルダ配下にあればクラウド同期されているとみなす
SYNC_FOLDER_ENV = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


@dataclass(frozen=True)
class Spec:
    kind: type
    default: Any
    minimum: int | None = None
    label: str = ""


SPECS: dict[str, Spec] = {
    "gemini_model": Spec(str, "", label="Gemini モデル名"),
    "gemini_rpm": Spec(int, 0, 0, "1分あたりリクエスト上限"),
    "gemini_tpm": Spec(int, 0, 0, "1分あたりトークン上限"),
    "gemini_rpd": Spec(int, 0, 0, "1日あたりリクエスト上限"),
    "gemini_max_output_tokens": Spec(int, 8192, 256, "出力トークン上限"),
    "gemini_thinking_budget": Spec(int, 1024, 0, "thinking の予算"),
    "scrape_interval_sec": Spec(int, 10, 5, "スクレイピングの間隔（秒）"),
    "scrape_contact": Spec(str, "", label="User-Agent に含める連絡先"),
    "short_recheck_hours": Spec(int, 24, 1, "空売り残高を再取得しない時間"),
    "auto_update_on_start": Spec(bool, True, label="起動時の自動更新"),
    "auto_update_short": Spec(bool, False, label="自動更新に空売り残高を含める"),
    "auto_update_min_interval_min": Spec(int, 60, 0, "自動更新でスキップする間隔（分）"),
}


def mask(value: str | None) -> str:
    """画面表示用。先頭4文字だけ見せる（短いキーは全部隠す）。"""
    if not value:
        return ""
    return f"{value[:4]}****" if len(value) > 4 else "****"


def sync_folder_of(path: Path) -> str | None:
    """path がクラウド同期フォルダ配下ならそのフォルダを返す。"""
    resolved = Path(path).resolve()
    for name in SYNC_FOLDER_ENV:
        root = os.environ.get(name)
        if root and resolved.is_relative_to(Path(root).resolve()):
            return root
    return None


class Settings:
    def __init__(self, db: Database, data_dir: Path | None = None, keyring_backend=None):
        self.db = db
        self.data_dir = data_dir
        self._keyring = keyring_backend  # テストではフェイクを渡す

    # ---------- 通常の設定値 ----------
    def get(self, key: str):
        spec = _spec(key)
        raw = self.db.get_setting(key)
        return spec.default if raw is None else _decode(spec, raw)

    def all(self) -> dict[str, Any]:
        stored = self.db.get_settings()  # schema_version など SPECS に無いキーは含めない
        return {k: (_decode(s, stored[k]) if k in stored else s.default) for k, s in SPECS.items()}

    def update(self, values: dict[str, Any]) -> None:
        """検証してから保存する。1つでも不正なら何も保存しない。API キーもここで受け付ける。"""
        if not isinstance(values, dict):
            raise UserFacingError("設定値の形式が正しくありません")
        plain = {k: _encode(k, v) for k, v in values.items() if k not in SECRET_ENV}
        secrets = {k: v for k, v in values.items() if k in SECRET_ENV}
        for key, value in secrets.items():
            self.set_secret(key, value)
        for key, raw in plain.items():
            self.db.set_setting(key, raw)

    # ---------- API キー ----------
    def get_secret(self, key: str) -> str:
        """環境変数を優先し、無ければ資格情報ストアから読む。未設定は空文字。"""
        env = os.environ.get(_secret_env(key), "").strip()
        if env:
            return env
        try:
            return self._backend().get_password(KEYRING_SERVICE, key) or ""
        except UserFacingError:
            return ""
        except Exception:
            log.warning("could not read %s from the credential store", key)
            return ""

    def secret_source(self, key: str) -> str:
        """'env' | 'keyring' | ''（未設定）"""
        if os.environ.get(_secret_env(key), "").strip():
            return "env"
        return "keyring" if self.get_secret(key) else ""

    def set_secret(self, key: str, value: str | None) -> None:
        """空文字・None なら削除する。値はログに出さない。"""
        _secret_env(key)
        value = (value or "").strip()
        backend = self._backend()
        try:
            if value:
                backend.set_password(KEYRING_SERVICE, key, value)
            elif backend.get_password(KEYRING_SERVICE, key):
                backend.delete_password(KEYRING_SERVICE, key)
        except Exception as exc:
            raise UserFacingError(_KEYRING_HELP.format(env=_secret_env(key))) from exc
        log.info("%s %s", key, "saved" if value else "cleared")

    # ---------- 画面向け ----------
    def public_view(self) -> dict:
        """画面に渡す設定。API キーはマスクし、平文を含めない。"""
        secrets = {
            key: {"masked": mask(self.get_secret(key)), "source": self.secret_source(key), "env": env}
            for key, env in SECRET_ENV.items()
        }
        return {"values": self.all(), "secrets": secrets, "warnings": self.warnings()}

    def warnings(self) -> list[str]:
        if self.data_dir is None:
            return []
        root = sync_folder_of(self.data_dir)
        if root is None:
            return []
        return [
            f"データ保存先（{self.data_dir}）はクラウド同期フォルダ（{root}）の配下にあります。"
            "SQLite のファイルは同期ツールと相性が悪く、破損や競合の原因になります。"
            "環境変数 CHRONOS_DATA_DIR で同期対象外の場所を指定することをおすすめします。"
        ]

    def _backend(self):
        if self._keyring is not None:
            return self._keyring
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring
        except ImportError as exc:
            raise UserFacingError(_KEYRING_HELP.format(env="CHRONOS_EDINET_API_KEY / GEMINI_API_KEY")) from exc
        if isinstance(keyring.get_keyring(), FailKeyring):
            raise UserFacingError(_KEYRING_HELP.format(env="CHRONOS_EDINET_API_KEY / GEMINI_API_KEY"))
        return keyring


_KEYRING_HELP = (
    "OS の資格情報ストアに API キーを保存できませんでした。"
    "代わりに環境変数 {env} で指定してください。"
)


def _spec(key: str) -> Spec:
    if key not in SPECS:
        raise UserFacingError(f"不明な設定項目です: {key}")
    return SPECS[key]


def _secret_env(key: str) -> str:
    if key not in SECRET_ENV:
        raise UserFacingError(f"不明な API キーです: {key}")
    return SECRET_ENV[key]


def _encode(key: str, value: Any) -> str:
    spec = _spec(key)
    if spec.kind is bool:
        if isinstance(value, bool):
            return "1" if value else "0"
        if str(value).strip().lower() in ("1", "true", "on"):
            return "1"
        if str(value).strip().lower() in ("0", "false", "off", ""):
            return "0"
        raise UserFacingError(f"{spec.label}: オン／オフで指定してください")
    if spec.kind is int:
        try:
            if isinstance(value, bool) or (isinstance(value, float) and not value.is_integer()):
                raise ValueError
            number = int(str(value).strip()) if isinstance(value, str) else int(value)
        except (TypeError, ValueError):
            raise UserFacingError(f"{spec.label}: 整数で指定してください") from None
        if spec.minimum is not None and number < spec.minimum:
            raise UserFacingError(f"{spec.label}: {spec.minimum} 以上で指定してください")
        return str(number)
    return str(value or "").strip()


def _decode(spec: Spec, raw: str):
    if spec.kind is bool:
        return raw == "1"
    if spec.kind is int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return spec.default
    return raw or ""
