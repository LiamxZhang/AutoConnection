"""Smoke and integration tests for the desktop application shell."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime

import pytest

from net_connector.network import ConnectionCode, ConnectionResult, Credentials
from net_connector.storage import CredentialError


def test_imports_do_not_construct_tk(monkeypatch: pytest.MonkeyPatch) -> None:
    import tkinter

    def unexpected_tk(*args, **kwargs):
        raise AssertionError("Tk constructed during import")

    monkeypatch.setattr(tkinter, "Tk", unexpected_tk)
    sys.modules.pop("net_connector.app", None)
    sys.modules.pop("net_connector.__main__", None)

    importlib.import_module("net_connector.app")
    importlib.import_module("net_connector.__main__")


@pytest.mark.parametrize(
    ("code", "key"),
    [
        (ConnectionCode.ALREADY_ONLINE, "status.already_online"),
        (ConnectionCode.CONNECTED, "status.connected"),
        (ConnectionCode.MISSING_CREDENTIALS, "error.missing_credentials"),
        (ConnectionCode.PORTAL_UNREACHABLE, "error.portal_unreachable"),
        (ConnectionCode.PORTAL_REJECTED, "error.portal_rejected"),
        (ConnectionCode.VERIFICATION_FAILED, "error.verification_failed"),
        (ConnectionCode.INTERNAL_ERROR, "error.internal"),
    ],
)
def test_result_message_key_maps_every_connection_code(code: ConnectionCode, key: str) -> None:
    from net_connector.app import result_message_key

    assert result_message_key(ConnectionResult(code)) == key


def test_result_message_key_prioritizes_vpn_for_failures_only() -> None:
    from net_connector.app import result_message_key

    failed = ConnectionResult(ConnectionCode.PORTAL_REJECTED, "secret detail", ("tun0",))
    succeeded = ConnectionResult(ConnectionCode.CONNECTED, vpn_interfaces=("tun0",))

    assert result_message_key(failed) == "error.vpn_detected"
    assert result_message_key(succeeded) == "status.connected"


class FakeCredentialStore:
    def __init__(self, value=None, error: Exception | None = None) -> None:
        self.value = value
        self.error = error

    def load(self):
        if self.error is not None:
            raise self.error
        return self.value


class FakeNetwork:
    def __init__(self, result=None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.credentials = []

    def connect(self, credentials):
        self.credentials.append(credentials)
        if self.error is not None:
            raise self.error
        return self.result

    def is_online(self):
        return True


def test_perform_connection_handles_missing_credentials() -> None:
    from net_connector.app import perform_connection

    network = FakeNetwork()
    outcome = perform_connection(lambda: FakeCredentialStore(), network)

    assert outcome.result == ConnectionResult(ConnectionCode.MISSING_CREDENTIALS)
    assert outcome.credential_store_failed is False
    assert network.credentials == []


def test_perform_connection_classifies_credential_error_without_leaking(capsys) -> None:
    from net_connector.app import perform_connection

    secret = "credential-backend-secret"
    network = FakeNetwork()
    outcome = perform_connection(
        lambda: FakeCredentialStore(error=CredentialError(secret)),
        network,
    )

    assert outcome.result == ConnectionResult(ConnectionCode.INTERNAL_ERROR)
    assert outcome.credential_store_failed is True
    assert secret not in repr(outcome)
    assert secret not in capsys.readouterr().out + capsys.readouterr().err
    assert network.credentials == []


def test_perform_connection_returns_network_result() -> None:
    from net_connector.app import perform_connection

    credentials = Credentials("demo", "dummy-secret")
    expected = ConnectionResult(ConnectionCode.CONNECTED)
    network = FakeNetwork(expected)

    outcome = perform_connection(lambda: FakeCredentialStore(credentials), network)

    assert outcome.result is expected
    assert outcome.credential_store_failed is False
    assert network.credentials == [credentials]


@pytest.mark.parametrize("failure_point", ["factory", "network"])
def test_perform_connection_converts_unexpected_errors_safely(failure_point: str, capsys) -> None:
    from net_connector.app import perform_connection

    secret = "unexpected-secret-detail"
    network = FakeNetwork(error=RuntimeError(secret))

    def factory():
        if failure_point == "factory":
            raise RuntimeError(secret)
        return FakeCredentialStore(Credentials("demo", "dummy-secret"))

    outcome = perform_connection(factory, network)

    assert outcome.result == ConnectionResult(ConnectionCode.INTERNAL_ERROR)
    assert outcome.credential_store_failed is False
    assert secret not in repr(outcome)
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err


class DeferredThread:
    instances = []

    def __init__(self, *, target, daemon) -> None:
        self.target = target
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True


def test_connection_worker_suppresses_busy_request_and_hands_result_through_queue() -> None:
    from net_connector.app import ConnectionWorker

    DeferredThread.instances.clear()
    expected = ConnectionResult(ConnectionCode.CONNECTED)
    network = FakeNetwork(expected)
    worker = ConnectionWorker(
        lambda: FakeCredentialStore(Credentials("demo", "dummy-secret")),
        network,
        thread_factory=DeferredThread,
    )

    assert worker.start_connection() is True
    assert worker.start_connection() is False
    assert worker.busy is True
    assert len(DeferredThread.instances) == 1
    assert DeferredThread.instances[0].daemon is True
    assert DeferredThread.instances[0].started is True

    DeferredThread.instances[0].target()
    assert worker.busy is True


def test_connection_worker_returns_safe_outcome_and_clears_busy() -> None:
    from net_connector.app import ConnectionWorker

    DeferredThread.instances.clear()
    expected = ConnectionResult(ConnectionCode.CONNECTED)
    worker = ConnectionWorker(
        lambda: FakeCredentialStore(Credentials("demo", "dummy-secret")),
        FakeNetwork(expected),
        thread_factory=DeferredThread,
    )
    worker.start_connection()
    DeferredThread.instances[0].target()

    operation, outcome = worker.take_result()

    assert operation == "connect"
    assert outcome.result is expected
    assert worker.busy is False
    assert worker.take_result() is None


def test_startup_status_worker_calls_only_is_online() -> None:
    from net_connector.app import ConnectionWorker

    DeferredThread.instances.clear()

    def forbidden_credentials():
        raise AssertionError("status check accessed credentials")

    worker = ConnectionWorker(forbidden_credentials, FakeNetwork(), thread_factory=DeferredThread)

    assert worker.start_status_check() is True
    DeferredThread.instances[0].target()

    assert worker.take_result() == ("status", True)
    assert worker.busy is False


def test_worker_thread_start_failure_restores_idle_state() -> None:
    from net_connector.app import ConnectionWorker

    class BrokenThread:
        def __init__(self, **kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread unavailable")

    worker = ConnectionWorker(lambda: FakeCredentialStore(), FakeNetwork(), thread_factory=BrokenThread)

    assert worker.start_connection() is False
    assert worker.busy is False


def test_localized_language_options_round_trip_modes() -> None:
    from net_connector.app import language_options, language_mode_for_label
    from net_connector.i18n import Translator

    for language in ("en", "zh"):
        translator = Translator(language)
        options = language_options(translator)
        assert len(options) == 3
        assert len(set(options)) == 3
        assert [language_mode_for_label(label, translator) for label in options] == ["system", "zh", "en"]


class RecordingSettingsStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.saved = []
        self.error = error

    def save(self, settings) -> None:
        if self.error is not None:
            raise self.error
        self.saved.append(settings)


def test_persist_schedule_validates_before_saving_and_returns_scheduler() -> None:
    from net_connector.app import persist_schedule
    from net_connector.storage import Settings

    store = RecordingSettingsStore()
    now = datetime(2026, 7, 23, 9, 0)

    updated, scheduler = persist_schedule(store, Settings(), True, "10:30", now)

    assert updated == Settings(schedule_enabled=True, schedule_time="10:30")
    assert store.saved == [updated]
    assert scheduler is not None
    assert scheduler.schedule_time == "10:30"


def test_persist_schedule_invalid_input_does_not_save() -> None:
    from net_connector.app import persist_schedule
    from net_connector.scheduler import ScheduleError
    from net_connector.storage import Settings

    store = RecordingSettingsStore()

    with pytest.raises(ScheduleError):
        persist_schedule(store, Settings(), True, "9:00", datetime(2026, 7, 23, 9, 0))

    assert store.saved == []


def test_persist_schedule_save_failure_does_not_mutate_existing_settings() -> None:
    from net_connector.app import persist_schedule
    from net_connector.storage import Settings, SettingsError

    original = Settings(schedule_enabled=True, schedule_time="08:30")
    store = RecordingSettingsStore(SettingsError("safe failure"))

    with pytest.raises(SettingsError):
        persist_schedule(store, original, False, "10:30", datetime(2026, 7, 23, 9, 0))

    assert original == Settings(schedule_enabled=True, schedule_time="08:30")
    assert store.saved == []


def test_tray_callback_only_marshals_to_root_after() -> None:
    from net_connector.app import marshal_to_tk

    calls = []

    class FakeRoot:
        def after(self, delay, callback):
            calls.append((delay, callback))

    def ui_callback():
        calls.append("ui")

    callback = marshal_to_tk(FakeRoot(), ui_callback)
    callback("tray-icon", "menu-item")

    assert calls == [(0, ui_callback)]


def test_gui_smoke_builds_compact_bilingual_app(tmp_path) -> None:
    import tkinter as tk

    from net_connector.app import DesktopApp
    from net_connector.storage import SettingsStore

    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")

    root.withdraw()
    app = None
    try:
        app = DesktopApp(
            root,
            settings_store=SettingsStore(tmp_path / "settings.json"),
            credential_store_factory=lambda: FakeCredentialStore(),
            network_service=FakeNetwork(ConnectionResult(ConnectionCode.CONNECTED)),
            auto_status_check=False,
            enable_tray=False,
        )
        root.update_idletasks()

        assert app.settings_button.cget("text") == "⚙"
        assert app.settings_button.cget("takefocus")
        assert int(app.connect_button.cget("width")) == 18
        assert app.schedule_checkbutton.winfo_exists()
        assert app.schedule_time_entry.winfo_exists()
        assert root.minsize()[0] >= 420

        app.open_settings()
        root.update_idletasks()
        assert app.settings_dialog is not None
        assert str(app.language_combobox.cget("state")) == "readonly"
        assert len(app.language_combobox.cget("values")) == 3
        assert str(app.password_entry.cget("show")) == "•"

        app.preview_language("zh")
        root.update_idletasks()
        assert root.title() == "网络连接器"
        assert app.connect_button.cget("text") == "连接"
        assert app.settings_dialog.title() == "设置"

        app.cancel_settings()
        root.update_idletasks()
        assert root.title() == "Network Connector"
        assert app.settings.language == "system"
    finally:
        if app is not None:
            app.exit()
        else:
            root.destroy()


def test_settings_credential_load_waits_for_busy_startup_probe(tmp_path) -> None:
    import tkinter as tk

    from net_connector.app import DesktopApp
    from net_connector.storage import SettingsStore

    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")

    root.withdraw()
    DeferredThread.instances.clear()
    credentials = Credentials("saved-user", "saved-password")
    app = DesktopApp(
        root,
        settings_store=SettingsStore(tmp_path / "settings.json"),
        credential_store_factory=lambda: FakeCredentialStore(credentials),
        network_service=FakeNetwork(),
        thread_factory=DeferredThread,
        auto_status_check=True,
        enable_tray=False,
    )
    try:
        assert len(DeferredThread.instances) == 1
        app.open_settings()
        assert len(DeferredThread.instances) == 1

        DeferredThread.instances[0].target()
        app._poll_worker_queue()

        assert len(DeferredThread.instances) == 2
        DeferredThread.instances[1].target()
        app._poll_worker_queue()
        assert app.username_var.get() == "saved-user"
        assert app.password_var.get() == "saved-password"
    finally:
        app.exit()


def test_recovered_settings_are_reported_as_warning(monkeypatch) -> None:
    import tkinter as tk

    from net_connector import app as app_module
    from net_connector.storage import LoadResult, Settings

    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")
    root.withdraw()

    class RecoveredStore:
        def load(self):
            return LoadResult(Settings(), recovered=True)

    warnings = []
    monkeypatch.setattr(app_module.messagebox, "showwarning", lambda title, message, **kwargs: warnings.append(message))
    monkeypatch.setattr(
        app_module.messagebox,
        "showerror",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("recovery shown as error")),
    )
    app = app_module.DesktopApp(
        root,
        settings_store=RecoveredStore(),
        credential_store_factory=lambda: FakeCredentialStore(),
        network_service=FakeNetwork(),
        auto_status_check=False,
        enable_tray=False,
    )
    try:
        root.update()
        assert warnings == ["Settings were restored to defaults"]
    finally:
        app.exit()


def test_tray_unavailable_notice_reuses_fixed_bottom_area(tmp_path) -> None:
    import tkinter as tk

    from net_connector.app import DesktopApp
    from net_connector.storage import SettingsStore

    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")
    root.withdraw()
    app = DesktopApp(
        root,
        settings_store=SettingsStore(tmp_path / "settings.json"),
        credential_store_factory=lambda: FakeCredentialStore(),
        network_service=FakeNetwork(),
        auto_status_check=False,
        enable_tray=False,
    )
    try:
        app.close_window()
        assert app.close_hint_label.cget("text") == app.text("window.tray_unavailable")
    finally:
        app.exit()
