"""Tests for the bilingual message catalog and translation helper."""

from __future__ import annotations

import string

import pytest

from net_connector import i18n


REQUIRED_KEYS = {
    "app.title",
    "tooltip.settings",
    "action.connect", "action.retry", "action.save", "action.cancel",
    "action.show_password", "action.hide_password", "action.exit",
    "status.waiting", "status.checking", "status.connecting", "status.connected",
    "status.already_online", "status.offline", "status.failed",
    "status.last_check.never", "status.last_check",
    "schedule.title", "schedule.tray_hint", "schedule.every_day", "schedule.next_run",
    "schedule.disabled", "schedule.invalid_time",
    "window.close_to_tray", "window.tray_unavailable",
    "settings.title", "settings.username", "settings.password", "settings.language",
    "settings.language.system", "settings.language.zh", "settings.language.en",
    "settings.credential_hint",
    "tray.show", "tray.connect", "tray.exit",
    "error.missing_credentials", "error.credential_store", "error.portal_unreachable",
    "error.portal_rejected", "error.verification_failed", "error.vpn_detected",
    "error.internal", "error.settings_recovered", "error.settings_save",
    "error.settings_rollback", "error.busy",
    "dialog.error", "dialog.info", "dialog.credentials_required",
}


def test_catalogs_have_identical_nonempty_string_keys_and_values() -> None:
    assert set(i18n.CATALOGS) == {"zh", "en"}
    assert set(i18n.CATALOGS["zh"]) == set(i18n.CATALOGS["en"])
    assert set(i18n.CATALOGS["zh"]) == REQUIRED_KEYS
    for catalog in i18n.CATALOGS.values():
        for key, value in catalog.items():
            assert isinstance(key, str)
            assert isinstance(value, str)
            assert value


def test_catalogs_have_matching_format_placeholders() -> None:
    formatter = string.Formatter()

    def fields(template: str) -> set[str]:
        return {
            name.split(".", 1)[0].split("[", 1)[0]
            for _, name, _, _ in formatter.parse(template)
            if name is not None
        }

    for key in REQUIRED_KEYS:
        assert fields(i18n.CATALOGS["zh"][key]) == fields(i18n.CATALOGS["en"][key])


def test_catalogs_are_read_only() -> None:
    with pytest.raises(TypeError):
        i18n.CATALOGS["zh"]["app.title"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        i18n.CATALOGS["fr"] = {}  # type: ignore[index]
    assert not hasattr(i18n, "_CATALOGS")


@pytest.mark.parametrize("mode", ["zh", "en"])
def test_resolve_language_returns_explicit_modes(mode: str) -> None:
    assert i18n.resolve_language(mode, "de_DE") == mode


@pytest.mark.parametrize("system_locale", ["zh_CN", "zh-CN", "zh_Hans", "zh-TW"])
def test_resolve_language_recognizes_chinese_system_locales(system_locale: str) -> None:
    assert i18n.resolve_language("system", system_locale) == "zh"


@pytest.mark.parametrize("system_locale", ["de_DE", "en_US", None, 42, object()])
def test_resolve_language_defaults_other_or_invalid_locales_to_english(system_locale: object) -> None:
    assert i18n.resolve_language("system", system_locale) == "en"


def test_resolve_language_handles_locale_lookup_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_lookup() -> tuple[str, str]:
        raise RuntimeError("unavailable")

    monkeypatch.setattr(i18n.locale, "getlocale", raise_lookup)
    assert i18n.resolve_language("system") == "en"


@pytest.mark.parametrize("mode", ["SYSTEM", "fr", "", None, 1])
def test_resolve_language_rejects_invalid_modes(mode: object) -> None:
    with pytest.raises(i18n.I18nError):
        i18n.resolve_language(mode)  # type: ignore[arg-type]


def test_language_validation_rejects_hostile_string_subclasses() -> None:
    class HostileString(str):
        def __eq__(self, other: object) -> bool:
            raise RuntimeError("must not compare")

        def __hash__(self) -> int:
            raise RuntimeError("must not hash")

    hostile = HostileString("zh")
    for operation in (
        lambda: i18n.resolve_language(hostile),
        lambda: i18n.Translator(hostile),
        lambda: i18n.Translator("en").set_language(hostile),
    ):
        with pytest.raises(i18n.I18nError) as error:
            operation()
        assert str(error.value) == "Invalid language configuration."


def test_translator_returns_localized_and_formatted_text() -> None:
    zh = i18n.Translator("zh")
    en = i18n.Translator("en")
    assert zh.text("action.connect") == "连接"
    assert en.text("action.connect") == "Connect"
    assert zh.text("status.last_check", time="10:30") == "上次检查：10:30"
    assert en.text("schedule.next_run", time="10:30") == "Next run: 10:30"
    assert "tun0" in zh.text("error.vpn_detected", interfaces="tun0")
    assert "tun0" in en.text("error.vpn_detected", interfaces="tun0")


def test_translator_returns_visible_marker_for_unknown_or_nonstring_keys() -> None:
    translator = i18n.Translator("en")
    assert translator.text("not.a.key") == "[not.a.key]"
    assert translator.text(None) == "[missing]"  # type: ignore[arg-type]


def test_translator_rejects_hostile_string_subclass_keys_safely() -> None:
    class HostileKey(str):
        def __hash__(self) -> int:
            raise RuntimeError("must not hash")

        def __eq__(self, other: object) -> bool:
            raise RuntimeError("must not compare")

        def __format__(self, spec: str) -> str:
            raise RuntimeError("must not format")

    assert i18n.Translator("en").text(HostileKey("action.connect")) == "[missing]"


def test_set_language_switches_immediately_and_invalid_change_is_transactional() -> None:
    translator = i18n.Translator("en")
    translator.set_language("zh")
    assert translator.language == "zh"
    assert translator.text("action.retry") == "重试"
    with pytest.raises(i18n.I18nError):
        translator.set_language("system")
    assert translator.language == "zh"
    assert translator.text("action.retry") == "重试"


def test_formatting_errors_are_generic_and_do_not_expose_values() -> None:
    translator = i18n.Translator("en")
    secret = "dont-leak-this-password"
    with pytest.raises(i18n.I18nError) as error:
        translator.text("status.last_check", time=secret, unexpected="value")
    assert secret not in str(error.value)
    assert secret not in repr(error.value)
    assert error.value.__context__ is None
    with pytest.raises(i18n.I18nError) as missing:
        translator.text("status.last_check")
    assert "time" not in str(missing.value).lower()


@pytest.mark.parametrize(
    "kwargs",
    [
        {"time": "validation-secret", "extra": "ignored"},
        {"unexpected": "validation-secret"},
    ],
)
def test_validation_failures_clear_secret_values_before_raising(kwargs: dict[str, str]) -> None:
    secret = "validation-secret"
    translator = i18n.Translator("zh")
    with pytest.raises(i18n.I18nError) as error:
        translator.text("status.last_check", **kwargs)

    assert error.value.__context__ is None
    assert translator.language == "zh"
    traceback = error.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename == i18n.__file__:
            assert secret not in repr(frame.f_locals)
        traceback = traceback.tb_next


@pytest.mark.parametrize("exception_type", [ValueError, RuntimeError])
def test_hostile_formatting_errors_are_generic_and_leave_no_secret_in_i18n_frames(
    exception_type: type[Exception], caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "format-secret-must-not-leak"

    class HostileValue:
        def __format__(self, spec: str) -> str:
            raise exception_type(f"hostile formatter: {secret}")

    translator = i18n.Translator("zh")
    with pytest.raises(i18n.I18nError) as error:
        translator.text("status.last_check", time=HostileValue())

    assert str(error.value) == "Invalid message formatting."
    assert secret not in repr(error.value)
    assert error.value.__context__ is None
    assert translator.language == "zh"
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err + caplog.text

    traceback = error.value.__traceback__
    while traceback is not None:
        frame = traceback.tb_frame
        if frame.f_code.co_filename == i18n.__file__:
            assert secret not in repr(frame.f_locals)
        traceback = traceback.tb_next


def test_vpn_messages_express_uncertainty_and_offer_retry_advice() -> None:
    zh = i18n.CATALOGS["zh"]["error.vpn_detected"]
    en = i18n.CATALOGS["en"]["error.vpn_detected"].lower()
    assert "可能" in zh
    assert "重试" in zh
    assert "may" in en or "could" in en
    assert "retry" in en


def test_tray_unavailable_messages_keep_the_application_running() -> None:
    zh = i18n.CATALOGS["zh"]["window.tray_unavailable"]
    en = i18n.CATALOGS["en"]["window.tray_unavailable"].lower()
    assert "托盘不可用" in zh
    assert "最小化" in zh or "任务栏" in zh
    assert "退出" not in zh
    assert "tray is unavailable" in en
    assert "minimiz" in en or "taskbar" in en
    assert "remain" in en or "continu" in en
    assert "exit" not in en
