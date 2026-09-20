"""設定の読み書きと API キーの管理（SPEC §2.1）。"""

import logging

import pytest

from app.database import Database
from app.errors import UserFacingError
from app.settings import KEYRING_SERVICE, SECRET_ENV, SPECS, Settings, mask, sync_folder_of

SECRET = "edinet-SECRET-0123456789abcdef"


class FakeKeyring:
    """OS の資格情報ストアの代わり。"""

    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        del self.store[(service, name)]


class BrokenKeyring(FakeKeyring):
    def set_password(self, service, name, value):
        raise RuntimeError("no backend")


@pytest.fixture(autouse=True)
def no_secret_env(monkeypatch):
    for env in SECRET_ENV.values():
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "test.db")
    db.init_schema()
    keyring = FakeKeyring()
    return db, keyring, Settings(db, data_dir=tmp_path, keyring_backend=keyring)


# ---------- 通常の設定値 ----------
def test_defaults_match_spec(env):
    _, _, settings = env
    values = settings.all()
    assert set(values) == set(SPECS)
    assert values["scrape_interval_sec"] == 10
    assert values["gemini_rpd"] == 0
    assert values["gemini_max_output_tokens"] == 8192
    assert values["auto_update_on_start"] is True
    assert values["auto_update_short"] is False
    assert values["auto_update_min_interval_min"] == 60


def test_all_does_not_expose_schema_version(env):
    _, _, settings = env
    assert "schema_version" not in settings.all()


def test_update_roundtrip_keeps_types(env):
    db, _, settings = env
    settings.update({"gemini_model": " some-model ", "gemini_rpd": "250", "auto_update_short": True, "scrape_interval_sec": 15})
    again = Settings(db, keyring_backend=FakeKeyring())
    assert again.get("gemini_model") == "some-model"
    assert again.get("gemini_rpd") == 250
    assert again.get("auto_update_short") is True
    assert again.get("scrape_interval_sec") == 15


@pytest.mark.parametrize(
    "values",
    [
        {"scrape_interval_sec": 4},  # 下限 5
        {"gemini_rpm": -1},
        {"gemini_rpd": "abc"},
        {"gemini_rpd": 1.5},
        {"short_recheck_hours": 0},
        {"auto_update_on_start": "maybe"},
        {"unknown_key": 1},
    ],
)
def test_invalid_values_are_rejected(env, values):
    _, _, settings = env
    with pytest.raises(UserFacingError):
        settings.update(values)


def test_update_saves_nothing_when_any_value_is_invalid(env):
    _, _, settings = env
    with pytest.raises(UserFacingError):
        settings.update({"gemini_rpd": 100, "scrape_interval_sec": 1})
    assert settings.get("gemini_rpd") == 0


def test_bool_accepts_js_style_values(env):
    _, _, settings = env
    settings.update({"auto_update_on_start": "false"})
    assert settings.get("auto_update_on_start") is False
    settings.update({"auto_update_on_start": 1})
    assert settings.get("auto_update_on_start") is True


# ---------- API キー ----------
def test_mask():
    assert mask("") == ""
    assert mask(None) == ""
    assert mask("abcd") == "****"
    assert mask(SECRET) == "edin****"


def test_secret_is_stored_in_keyring_not_in_db(env, tmp_path):
    db, keyring, settings = env
    settings.update({"edinet_api_key": SECRET, "gemini_model": "m"})

    assert keyring.store[(KEYRING_SERVICE, "edinet_api_key")] == SECRET
    assert settings.get_secret("edinet_api_key") == SECRET
    assert SECRET not in " ".join(f"{k}={v}" for k, v in db.get_settings().items())
    for path in tmp_path.iterdir():  # DB 本体・WAL のどこにも平文が無い
        assert SECRET.encode() not in path.read_bytes()


def test_secret_never_reaches_logs_or_public_view(env, caplog):
    _, _, settings = env
    with caplog.at_level(logging.DEBUG):
        settings.set_secret("gemini_api_key", SECRET)
        view = settings.public_view()
        settings.set_secret("gemini_api_key", "")
    assert SECRET not in caplog.text
    assert SECRET not in repr(view)
    assert view["secrets"]["gemini_api_key"]["masked"] == "edin****"
    assert view["secrets"]["gemini_api_key"]["source"] == "keyring"


def test_empty_secret_deletes_it(env):
    _, keyring, settings = env
    settings.set_secret("edinet_api_key", SECRET)
    settings.set_secret("edinet_api_key", "  ")
    assert keyring.store == {}
    assert settings.get_secret("edinet_api_key") == ""
    assert settings.secret_source("edinet_api_key") == ""
    settings.set_secret("edinet_api_key", None)  # 未設定の削除はエラーにしない


def test_environment_variable_wins(env, monkeypatch):
    _, _, settings = env
    settings.set_secret("edinet_api_key", SECRET)
    monkeypatch.setenv("CHRONOS_EDINET_API_KEY", "from-env-value")
    assert settings.get_secret("edinet_api_key") == "from-env-value"
    assert settings.secret_source("edinet_api_key") == "env"
    assert settings.public_view()["secrets"]["edinet_api_key"]["masked"] == "from****"


def test_gemini_key_uses_standard_env_name(env, monkeypatch):
    _, _, settings = env
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")
    assert settings.get_secret("gemini_api_key") == "gem-key"


def test_unknown_secret_is_rejected(env):
    _, _, settings = env
    with pytest.raises(UserFacingError):
        settings.set_secret("other_key", "x")


def test_keyring_failure_is_user_facing_and_mentions_env(tmp_path):
    db = Database(tmp_path / "k.db")
    db.init_schema()
    settings = Settings(db, keyring_backend=BrokenKeyring())
    with pytest.raises(UserFacingError) as err:
        settings.update({"edinet_api_key": SECRET, "gemini_rpd": 5})
    assert "CHRONOS_EDINET_API_KEY" in str(err.value)
    assert SECRET not in str(err.value)
    assert settings.get("gemini_rpd") == 0  # キーの保存に失敗したら他も保存しない


# ---------- クラウド同期フォルダの警告 ----------
def test_sync_folder_detection(tmp_path, monkeypatch):
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    synced = tmp_path / "OneDrive"
    (synced / "docs" / "app" / "data").mkdir(parents=True)
    outside = tmp_path / "local" / "data"
    outside.mkdir(parents=True)

    assert sync_folder_of(synced / "docs" / "app" / "data") is None
    monkeypatch.setenv("OneDriveConsumer", str(synced))
    assert sync_folder_of(synced / "docs" / "app" / "data") == str(synced)
    assert sync_folder_of(outside) is None


def test_warning_is_included_in_public_view(tmp_path, monkeypatch):
    synced = tmp_path / "OneDrive"
    data_dir = synced / "app" / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("OneDrive", str(synced))
    db = Database(data_dir / "chronos.db")
    db.init_schema()

    warnings = Settings(db, data_dir=data_dir, keyring_backend=FakeKeyring()).public_view()["warnings"]
    assert len(warnings) == 1
    assert "CHRONOS_DATA_DIR" in warnings[0]


def test_no_warning_outside_sync_folder(env, monkeypatch):
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        monkeypatch.delenv(name, raising=False)
    _, _, settings = env
    assert settings.public_view()["warnings"] == []


# ---------- JS 公開 API 経由 ----------
def test_api_settings_roundtrip_never_returns_plain_secret(env):
    from app.api import Api

    _, _, settings = env
    api = Api(service=None, settings=settings)

    saved = api.save_settings({"gemini_rpd": 50, "edinet_api_key": SECRET})
    assert saved["ok"] is True
    assert saved["data"]["values"]["gemini_rpd"] == 50
    assert SECRET not in repr(saved)
    assert SECRET not in repr(api.get_settings())

    assert api.reveal_secret("edinet_api_key") == {"ok": True, "data": SECRET}

    cleared = api.save_settings({"edinet_api_key": ""})
    assert cleared["data"]["secrets"]["edinet_api_key"]["masked"] == ""


def test_api_reports_validation_errors_in_the_envelope(env):
    from app.api import Api

    _, _, settings = env
    api = Api(service=None, settings=settings)
    res = api.save_settings({"scrape_interval_sec": 1})
    assert res["ok"] is False
    assert "5 以上" in res["error"]
    assert api.reveal_secret("nope")["ok"] is False


def test_api_without_settings_fails_gracefully():
    from app.api import Api

    assert Api(service=None).get_settings()["ok"] is False
