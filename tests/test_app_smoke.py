"""Smoke and integration tests for the desktop application shell."""

from __future__ import annotations

import importlib
import queue
import sys
import threading
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from net_connector.network import ConnectionCode, ConnectionResult, Credentials
from net_connector.storage import CredentialError


def test_imports_do_not_construct_tk(monkeypatch: pytest.MonkeyPatch) -> None:
    import tkinter
    import keyring

    from net_connector import network, storage

    def unexpected_access(*args, **kwargs):
        raise AssertionError("runtime dependency accessed during import")

    monkeypatch.setattr(tkinter, "Tk", unexpected_access)
    monkeypatch.setattr("threading.Thread", unexpected_access)
    monkeypatch.setattr(storage, "SettingsStore", unexpected_access)
    monkeypatch.setattr(storage, "CredentialStore", unexpected_access)
    monkeypatch.setattr(network, "NetworkService", unexpected_access)
    monkeypatch.setattr(keyring, "get_password", unexpected_access)
    monkeypatch.setitem(sys.modules, "pystray", SimpleNamespace(Icon=unexpected_access))
    previous = {
        name: sys.modules.get(name)
        for name in ("net_connector.app", "net_connector.__main__")
    }
    try:
        sys.modules.pop("net_connector.app", None)
        sys.modules.pop("net_connector.__main__", None)

        importlib.import_module("net_connector.app")
        importlib.import_module("net_connector.__main__")
    finally:
        sys.modules.pop("net_connector.app", None)
        sys.modules.pop("net_connector.__main__", None)
        for name, module in previous.items():
            if module is not None:
                sys.modules[name] = module


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
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
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


class SchedulingWorker:
    def __init__(self, operation=None) -> None:
        self.busy = operation is not None
        self.operation = operation
        self.results = []
        self.connection_starts = 1 if operation == "connect" else 0
        self.credential_load_starts = 1 if operation == "credentials" else 0

    def start_connection(self) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.operation = "connect"
        self.connection_starts += 1
        return True

    def start_credentials_load(self) -> bool:
        if self.busy:
            return False
        self.busy = True
        self.operation = "credentials"
        self.credential_load_starts += 1
        return True

    def complete(self, outcome) -> None:
        self.results.append((self.operation, outcome))

    def take_result(self):
        if not self.results:
            return None
        self.busy = False
        self.operation = None
        return self.results.pop(0)


class PollingRoot:
    def __init__(self) -> None:
        self.after_calls = []

    def after(self, delay, callback) -> None:
        self.after_calls.append((delay, callback))


def make_scheduled_app(worker, now):
    from net_connector.app import DesktopApp
    from net_connector.scheduler import DailyScheduler

    app = DesktopApp.__new__(DesktopApp)
    app._exiting = False
    app.worker = worker
    app.root = PollingRoot()
    app.scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))
    app.now_provider = lambda: now[0]
    app._scheduled_connection_pending = False
    app._credentials_load_pending = False
    app.settings_dialog = None
    app._poll_tray_events = lambda: None
    app._recover_hidden_xorg_tray_failure = lambda: None
    app._render_schedule = lambda: None
    app._handle_status_outcome = lambda outcome: None
    app._handle_connection_outcome = lambda outcome: None
    app._handle_credentials_outcome = lambda outcome: None
    app._failed = False
    app._set_busy = lambda *_args, **_kwargs: None
    return app


@pytest.mark.parametrize(
    ("operation", "due_time", "outcome"),
    [
        ("status", datetime(2026, 7, 23, 8, 30), False),
        ("connect", datetime(2026, 7, 23, 8, 30), SimpleNamespace()),
        ("status", datetime(2026, 7, 23, 14, 0), False),
    ],
    ids=["startup-status", "manual-connect", "sleep-catch-up"],
)
def test_due_schedule_waits_for_busy_worker_then_starts_exactly_once(
    operation,
    due_time,
    outcome,
) -> None:
    worker = SchedulingWorker(operation)
    initial_starts = worker.connection_starts
    now = [due_time]
    app = make_scheduled_app(worker, now)

    app._poll_schedule()

    assert app._scheduled_connection_pending is True
    assert worker.connection_starts == initial_starts
    worker.complete(outcome)
    app._poll_worker_queue()

    assert app._scheduled_connection_pending is False
    assert worker.connection_starts == initial_starts + 1
    app._poll_worker_queue()
    app._poll_schedule()
    assert worker.connection_starts == initial_starts + 1


def test_due_schedule_waits_for_credential_load_before_starting() -> None:
    from net_connector.app import CredentialLoadOutcome

    worker = SchedulingWorker("credentials")
    app = make_scheduled_app(worker, [datetime(2026, 7, 23, 8, 30)])

    app._poll_schedule()
    assert app._scheduled_connection_pending is True
    assert worker.connection_starts == 0

    worker.complete(CredentialLoadOutcome())
    app._poll_worker_queue()

    assert app._scheduled_connection_pending is False
    assert worker.connection_starts == 1


@pytest.mark.parametrize(
    ("enabled", "schedule_time"),
    [(False, "08:30"), (True, "10:30")],
    ids=["disable", "reschedule"],
)
def test_schedule_edit_cancels_a_stale_pending_occurrence(enabled, schedule_time) -> None:
    from net_connector.storage import Settings

    worker = SchedulingWorker("connect")
    app = make_scheduled_app(worker, [datetime(2026, 7, 23, 8, 30)])
    app.settings = Settings(schedule_enabled=True, schedule_time="08:30")
    app.settings_store = RecordingSettingsStore()
    app.schedule_enabled_var = ScalarVar(enabled)
    app.schedule_time_var = ScalarVar(schedule_time)
    app._show_error = lambda *_args, **_kwargs: None

    app._poll_schedule()
    assert app._scheduled_connection_pending is True

    app._apply_schedule_edit()

    assert app._scheduled_connection_pending is False
    worker.complete(SimpleNamespace())
    app._poll_worker_queue()
    assert worker.connection_starts == 1


def test_tray_callback_only_queues_for_main_thread() -> None:
    from net_connector.app import DesktopApp

    calls = []
    app = DesktopApp.__new__(DesktopApp)
    app._tray_events = queue.Queue()

    def ui_callback():
        calls.append("ui")

    callback = app._queue_tray_callback(ui_callback)
    tray_thread = threading.Thread(target=callback, args=("tray-icon", "menu-item"))
    tray_thread.start()
    tray_thread.join(timeout=1)

    assert calls == []
    assert app._tray_events.empty() is False
    app._poll_tray_events()
    assert calls == ["ui"]


def test_queued_exit_stops_worker_polling_and_rescheduling() -> None:
    from net_connector.app import DesktopApp

    class ForbiddenWorker:
        def take_result(self):
            raise AssertionError("worker polled after exit")

    app = DesktopApp.__new__(DesktopApp)
    app._exiting = False
    app._tray_events = queue.Queue()
    app._tray_events.put(("callback", lambda: setattr(app, "_exiting", True), None))
    app._tray_events.put(
        ("callback", lambda: (_ for _ in ()).throw(AssertionError("callback ran after exit")), None)
    )
    app.worker = ForbiddenWorker()
    app.root = SimpleNamespace(
        after=lambda *_args: (_ for _ in ()).throw(AssertionError("poller rescheduled after exit"))
    )

    app._poll_worker_queue()

    assert app._exiting is True


def test_tray_polling_error_does_not_block_hidden_window_recovery() -> None:
    from net_connector.app import TrayLifecycle

    events = []
    app = make_headless_tray_app(events)
    app.root.withdraw()
    events.clear()
    icon = SimpleNamespace(visible=True, _running=False)
    state = TrayLifecycle()
    state.runner_finished.set()
    app._tray = icon
    app._tray_available = True
    app._tray_states[id(icon)] = state
    app._tray_events.put(
        ("callback", lambda: (_ for _ in ()).throw(RuntimeError("tray command failed")), None)
    )
    app._tray_events.put(("failed", icon, state))

    app._poll_tray_events()

    assert app.root.state() == "normal"
    assert events.count("deiconify") == 1
    assert events.count(("warning", "window.tray_unavailable")) == 1
    assert app._tray_events.empty() is True


class ScalarVar:
    def __init__(self, value) -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


class TracedVar(ScalarVar):
    def __init__(self, value) -> None:
        super().__init__(value)
        self.callbacks = []

    def trace_add(self, mode, callback):
        assert mode == "write"
        self.callbacks.append(callback)

    def set(self, value) -> None:
        super().set(value)
        for callback in self.callbacks:
            callback("variable", "", "write")


@pytest.mark.parametrize(
    ("dirty_field", "expected_username", "expected_password"),
    [
        ("username", "typed-user", "saved-password"),
        ("password", "saved-user", "typed-password"),
    ],
)
def test_slow_credential_load_preserves_dirty_field_and_populates_untouched(
    dirty_field,
    expected_username,
    expected_password,
) -> None:
    from net_connector.app import CredentialLoadOutcome, DesktopApp

    app = DesktopApp.__new__(DesktopApp)
    app.settings_dialog = SimpleNamespace(winfo_exists=lambda: True)
    app.username_var = ScalarVar("typed-user" if dirty_field == "username" else "")
    app.password_var = ScalarVar("typed-password" if dirty_field == "password" else "")
    app._credential_fields_dirty = {
        "username": dirty_field == "username",
        "password": dirty_field == "password",
    }

    committed = Credentials("saved-user", "saved-password")
    app._handle_credentials_outcome(CredentialLoadOutcome(committed))

    assert app.username_var.get() == expected_username
    assert app.password_var.get() == expected_password
    assert app._committed_credentials is committed


@pytest.mark.parametrize("dirty_field", ["username", "password"])
def test_credential_write_events_mark_only_the_user_edited_field(dirty_field) -> None:
    from net_connector.app import CredentialLoadOutcome, DesktopApp

    app = DesktopApp.__new__(DesktopApp)
    app.settings_dialog = SimpleNamespace(winfo_exists=lambda: True)
    app.username_var = TracedVar("")
    app.password_var = TracedVar("")
    app._credential_fields_dirty = {"username": False, "password": False}
    app._track_credential_edits()

    edited_var = app.username_var if dirty_field == "username" else app.password_var
    edited_var.set(f"typed-{dirty_field}")
    app._handle_credentials_outcome(
        CredentialLoadOutcome(Credentials("saved-user", "saved-password"))
    )

    assert app._credential_fields_dirty == {
        "username": dirty_field == "username",
        "password": dirty_field == "password",
    }


class FakeSettingsDialog:
    def __init__(self) -> None:
        self.exists = True
        self.released = False

    def winfo_exists(self) -> bool:
        return self.exists

    def grab_release(self) -> None:
        self.released = True

    def destroy(self) -> None:
        self.exists = False


def make_save_settings_app(
    events,
    *,
    credential_error=None,
    settings_error=None,
    rollback_error=None,
    previous_credentials=Credentials("previous-user", "previous-secret"),
):
    from net_connector.app import DesktopApp
    from net_connector.i18n import Translator
    from net_connector.storage import Settings

    class SavingCredentialStore:
        def __init__(self) -> None:
            self.saved = []
            self.deleted = 0

        def save(self, credentials) -> None:
            restoring = bool(self.saved)
            events.append("credentials:restore" if restoring else "credentials:new")
            if not restoring and credential_error is not None:
                raise credential_error
            if restoring and rollback_error is not None:
                raise rollback_error
            self.saved.append(credentials)

        def delete(self) -> None:
            events.append("credentials:delete")
            if rollback_error is not None:
                raise rollback_error
            self.deleted += 1

    class SavingSettingsStore:
        def __init__(self) -> None:
            self.saved = []

        def save(self, settings) -> None:
            events.append("settings")
            if settings_error is not None:
                raise settings_error
            self.saved.append(settings)

    credential_store = SavingCredentialStore()
    settings_store = SavingSettingsStore()
    dialog = FakeSettingsDialog()
    errors = []
    app = DesktopApp.__new__(DesktopApp)
    app.settings = Settings(language="en")
    app.settings_store = settings_store
    app.credential_store_factory = lambda: credential_store
    app.username_var = ScalarVar("  demo-user  ")
    app.password_var = ScalarVar("save-secret")
    app.settings_dialog = dialog
    app._dialog_original_mode = "en"
    app._dialog_selected_mode = "zh"
    app._credentials_load_pending = False
    app._committed_credentials = previous_credentials
    app._credential_snapshot_failed = False
    app.translator = Translator("zh")
    app._show_error = lambda key, **_kwargs: errors.append(key)
    app.refresh_text = lambda: events.append("refresh")
    return app, credential_store, settings_store, dialog, errors


@pytest.mark.parametrize(
    ("snapshot_failed", "expected_error"),
    [(False, "error.busy"), (True, "error.credential_store")],
)
def test_save_settings_waits_for_a_known_committed_credential_snapshot(
    snapshot_failed,
    expected_error,
) -> None:
    from net_connector.app import _CREDENTIALS_UNLOADED

    events = []
    app, _credential_store, _settings_store, dialog, errors = make_save_settings_app(events)
    app._committed_credentials = _CREDENTIALS_UNLOADED
    app._credential_snapshot_failed = snapshot_failed

    app.save_settings()

    assert events == []
    assert errors == [expected_error]
    assert app.settings_dialog is dialog
    assert dialog.winfo_exists() is True


@pytest.mark.parametrize(
    ("failure_point", "expected_events", "expected_error"),
    [
        ("credentials", ["credentials:new"], "error.credential_store"),
        (
            "settings",
            ["credentials:new", "settings", "credentials:restore"],
            "error.settings_save",
        ),
    ],
)
def test_save_settings_failures_are_redacted_and_keep_dialog_state(
    failure_point,
    expected_events,
    expected_error,
    capsys,
    caplog,
) -> None:
    secret = "backend-save-secret"
    error = RuntimeError(secret)
    events = []
    app, _credential_store, _settings_store, dialog, errors = make_save_settings_app(
        events,
        credential_error=error if failure_point == "credentials" else None,
        settings_error=error if failure_point == "settings" else None,
    )

    app.save_settings()

    assert events == expected_events
    assert errors == [expected_error]
    assert app.settings.language == "en"
    assert app.translator.language == "zh"
    assert app._dialog_selected_mode == "zh"
    assert app.settings_dialog is dialog
    assert dialog.winfo_exists() is True
    captured = capsys.readouterr()
    assert secret not in repr(errors) + captured.out + captured.err + caplog.text

    app.cancel_settings()
    assert app.translator.language == "en"
    assert dialog.winfo_exists() is False


def test_save_settings_persists_credentials_before_settings_and_closes_dialog() -> None:
    events = []
    app, credential_store, settings_store, dialog, errors = make_save_settings_app(events)

    app.save_settings()

    assert events == ["credentials:new", "settings", "refresh"]
    assert credential_store.saved == [Credentials("demo-user", "save-secret")]
    assert settings_store.saved == [app.settings]
    assert app.settings.language == "zh"
    assert app.translator.language == "zh"
    assert app.settings_dialog is None
    assert app._dialog_original_mode is None
    assert app._dialog_selected_mode is None
    assert dialog.released is True
    assert dialog.winfo_exists() is False
    assert errors == []


def test_settings_failure_restores_previously_committed_credentials() -> None:
    previous = Credentials("previous-user", "previous-secret")
    events = []
    app, credential_store, settings_store, dialog, errors = make_save_settings_app(
        events,
        settings_error=RuntimeError("disk unavailable"),
        previous_credentials=previous,
    )

    app.save_settings()

    assert events == ["credentials:new", "settings", "credentials:restore"]
    assert credential_store.saved == [Credentials("demo-user", "save-secret"), previous]
    assert settings_store.saved == []
    assert app._committed_credentials is previous
    assert errors == ["error.settings_save"]
    assert app.settings_dialog is dialog
    assert dialog.winfo_exists() is True


def test_settings_failure_deletes_new_credentials_when_none_existed() -> None:
    events = []
    app, credential_store, settings_store, dialog, errors = make_save_settings_app(
        events,
        settings_error=RuntimeError("disk unavailable"),
        previous_credentials=None,
    )

    app.save_settings()

    assert events == ["credentials:new", "settings", "credentials:delete"]
    assert credential_store.deleted == 1
    assert settings_store.saved == []
    assert app._committed_credentials is None
    assert errors == ["error.settings_save"]
    assert app.settings_dialog is dialog


@pytest.mark.parametrize("previous_credentials", [Credentials("previous-user", "previous-secret"), None])
def test_settings_rollback_failure_is_reported_safely_and_keeps_dialog(
    previous_credentials,
    capsys,
    caplog,
) -> None:
    secret = "rollback-diagnostic-secret"
    events = []
    app, _credential_store, _settings_store, dialog, errors = make_save_settings_app(
        events,
        settings_error=RuntimeError("settings diagnostic"),
        rollback_error=RuntimeError(secret),
        previous_credentials=previous_credentials,
    )

    app.save_settings()

    expected_rollback = "credentials:restore" if previous_credentials is not None else "credentials:delete"
    assert events == ["credentials:new", "settings", expected_rollback]
    assert errors == ["error.settings_rollback"]
    assert app._credential_snapshot_failed is True
    assert app.settings_dialog is dialog
    assert dialog.winfo_exists() is True
    captured = capsys.readouterr()
    assert secret not in repr(errors) + captured.out + captured.err + caplog.text


def test_apply_schedule_edit_rolls_back_ui_store_and_scheduler(tmp_path, monkeypatch) -> None:
    from net_connector.app import DesktopApp
    from net_connector.scheduler import DailyScheduler
    from net_connector.storage import Settings, SettingsError, SettingsStore

    path = tmp_path / "settings.json"
    original = Settings(schedule_enabled=True, schedule_time="08:30")
    store = SettingsStore(path)
    store.save(original)
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 7, 0))
    app = DesktopApp.__new__(DesktopApp)
    app.settings = original
    app.scheduler = scheduler
    app.settings_store = store
    app.schedule_enabled_var = ScalarVar(False)
    app.schedule_time_var = ScalarVar("10:30")
    app.now_provider = lambda: datetime(2026, 7, 23, 7, 0)
    rendered = []
    errors = []
    app._render_schedule = lambda: rendered.append(app.scheduler)
    app._show_error = errors.append

    monkeypatch.setattr(store, "save", lambda settings: (_ for _ in ()).throw(SettingsError("disk unavailable")))
    app._apply_schedule_edit()

    assert app.settings is original
    assert app.scheduler is scheduler
    assert scheduler.schedule_time == "08:30"
    assert app.schedule_enabled_var.get() is True
    assert app.schedule_time_var.get() == "08:30"
    assert SettingsStore(path).load().settings == original
    assert rendered == [scheduler]
    assert errors == ["error.settings_save"]


class FakeTooltipWidget:
    def __init__(self) -> None:
        self.bindings = {}

    def bind(self, event, callback, add=None) -> None:
        self.bindings[event] = (callback, add)


def test_tooltip_supports_mouse_and_keyboard_focus() -> None:
    from net_connector.app import Tooltip

    widget = FakeTooltipWidget()
    tooltip = Tooltip(widget, lambda: "Settings")

    assert widget.bindings["<Enter>"] == (tooltip._show, True)
    assert widget.bindings["<Leave>"] == (tooltip._hide, True)
    assert widget.bindings["<FocusIn>"] == (tooltip._show, True)
    assert widget.bindings["<FocusOut>"] == (tooltip._hide, True)


def test_accessible_name_metadata_is_localized_and_testable() -> None:
    from net_connector.app import set_accessible_name

    widget = SimpleNamespace()
    set_accessible_name(widget, "设置")

    assert widget.accessible_name == "设置"


class HeadlessRoot:
    def __init__(self, events) -> None:
        self.events = events
        self.after_calls = []
        self.window_state = "normal"
        self.fail_deiconify = False
        self.owner_thread = threading.get_ident()
        self.reject_background_after = False
        self.background_after_calls = 0

    def after(self, delay, callback) -> None:
        if threading.get_ident() != self.owner_thread:
            self.background_after_calls += 1
            if self.reject_background_after:
                raise RuntimeError("Tk accessed outside owner thread")
        self.after_calls.append((delay, callback))

    def withdraw(self) -> None:
        self.window_state = "withdrawn"
        self.events.append("withdraw")

    def deiconify(self) -> None:
        self.events.append("deiconify")
        if self.fail_deiconify:
            raise RuntimeError("window restoration unavailable")
        self.window_state = "normal"

    def iconify(self) -> None:
        self.window_state = "iconic"
        self.events.append("iconify")

    def state(self) -> str:
        return self.window_state

    def destroy(self) -> None:
        self.events.append("destroy")


class HeadlessLabel:
    def __init__(self, events) -> None:
        self.events = events
        self.text = None

    def configure(self, *, text) -> None:
        self.text = text
        self.events.append(("label", text))


def install_fake_pystray(monkeypatch, *, start_error=False, menu_error=False, backend_returns=False):
    class FakeIcon:
        instances = []

        def __init__(self, name, image, title) -> None:
            self.name = name
            self.image = image
            self.title = title
            self.menu = None
            self.setup = None
            self.stopped = False
            self.visible = False
            self._running = False
            self._setup_thread = None
            self._runner_stop = threading.Event()
            self._backend_error = False
            setattr(self, "_Icon__queue", queue.Queue())
            self.__class__.instances.append(self)

        def run(self, *, setup) -> None:
            self.setup = setup
            self._setup_thread = threading.Thread(target=self._wait_for_ready, daemon=False)
            self._setup_thread.start()
            if start_error:
                raise RuntimeError("backend unavailable")
            if not backend_returns:
                self._runner_stop.wait()
            if self._backend_error:
                raise RuntimeError("backend terminated")

        def run_detached(self, *, setup) -> None:
            raise AssertionError("run_detached must not be used")

        def _wait_for_ready(self) -> None:
            getattr(self, "_Icon__queue").get()
            self.setup(self)

        def _mark_ready(self) -> None:
            self._running = True
            getattr(self, "_Icon__queue").put(True)

        def update_menu(self) -> None:
            if menu_error:
                raise RuntimeError("menu unavailable")

        def stop(self) -> None:
            if not self._running:
                return
            self._running = False
            self.stopped = True
            self._runner_stop.set()

        def finish_backend(self, *, error=False) -> None:
            self._backend_error = error
            self._runner_stop.set()

        def force_release(self) -> None:
            getattr(self, "_Icon__queue").put(True)
            self._runner_stop.set()
            if self._setup_thread is not None and self._setup_thread.ident is not None:
                self._setup_thread.join(timeout=1)

    module = SimpleNamespace(
        Icon=FakeIcon,
        Menu=lambda *items: tuple(items),
        MenuItem=lambda text, callback, **kwargs: (text, callback, kwargs),
    )
    monkeypatch.setitem(sys.modules, "pystray", module)
    return FakeIcon


def make_headless_tray_app(events):
    from net_connector.app import DesktopApp
    from net_connector.i18n import Translator

    app = DesktopApp.__new__(DesktopApp)
    app.root = HeadlessRoot(events)
    app.translator = Translator("en")
    app._exiting = False
    app._tray_requested = True
    app._tray = None
    app._tray_available = False
    app._tray_notice_shown = False
    app._tray_states = {}
    app._tray_events = queue.Queue()
    app.settings_dialog = None
    app._scheduled_connection_pending = False
    app.close_hint_label = HeadlessLabel(events)
    app._show_warning = lambda key: events.append(("warning", key))
    return app


def run_root_callbacks(root) -> None:
    while root.after_calls:
        next_index = min(range(len(root.after_calls)), key=lambda index: root.after_calls[index][0])
        _delay, callback = root.after_calls.pop(next_index)
        callback()


def test_tray_readiness_controls_when_window_may_withdraw(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    assert icon is not None
    assert app._tray_available is False
    assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True

    app.close_window()
    assert events == [
        ("warning", "window.tray_unavailable"),
        ("label", app.text("window.tray_unavailable")),
        "iconify",
    ]
    icon._mark_ready()
    icon._setup_thread.join(timeout=1)
    assert app._tray_available is False
    assert icon.visible is False
    app._poll_tray_events()
    run_root_callbacks(app.root)
    assert icon.visible is True
    assert app._tray_available is True

    app.close_window()
    assert events[-1] == "withdraw"
    assert events.count(("warning", "window.tray_unavailable")) == 1


def test_xorg_without_tray_manager_never_becomes_available(monkeypatch) -> None:
    fake_icon_type = install_fake_pystray(monkeypatch)
    fake_icon_type.__module__ = "pystray._xorg"
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    icon._systray_manager = None
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        run_root_callbacks(app.root)

        assert icon.visible is True
        assert app._tray is None
        assert app._tray_available is False
        app.close_window()
        assert "withdraw" not in events
    finally:
        icon.force_release()


def test_xorg_tray_manager_loss_before_close_uses_fallback(monkeypatch) -> None:
    fake_icon_type = install_fake_pystray(monkeypatch)
    fake_icon_type.__module__ = "pystray._xorg"
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    icon._systray_manager = object()
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        assert app._tray_available is True

        icon._systray_manager = None
        app.close_window()

        assert app._tray is None
        assert app._tray_available is False
        assert events == [
            ("warning", "window.tray_unavailable"),
            ("label", app.text("window.tray_unavailable")),
            "iconify",
        ]
        assert "withdraw" not in events
    finally:
        icon.force_release()


def test_xorg_tray_manager_loss_after_withdraw_is_recovered_by_main_poll(monkeypatch) -> None:
    fake_icon_type = install_fake_pystray(monkeypatch)
    fake_icon_type.__module__ = "pystray._xorg"
    events = []
    app = make_headless_tray_app(events)
    app.worker = SimpleNamespace(take_result=lambda: None, busy=False)
    app._credentials_load_pending = False

    app._start_tray()
    icon = app._tray
    icon._systray_manager = object()
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        app.close_window()
        state = app._tray_states[id(icon)]
        assert app.root.state() == "withdrawn"
        assert state.runner_thread.is_alive() is True

        icon._systray_manager = None
        app._poll_worker_queue()

        assert app.root.state() == "normal"
        assert app._tray is None
        assert app._tray_available is False
        assert app.close_hint_label.text == app.text("window.tray_unavailable")
        assert events.count("deiconify") == 1
        assert events.count(("warning", "window.tray_unavailable")) == 1

        app._poll_worker_queue()
        assert events.count("deiconify") == 1
        assert events.count(("warning", "window.tray_unavailable")) == 1
    finally:
        icon.force_release()


@pytest.mark.parametrize("backend_module", ["pystray._win32", "pystray._appindicator"])
def test_non_xorg_backends_keep_existing_close_behavior(monkeypatch, backend_module) -> None:
    fake_icon_type = install_fake_pystray(monkeypatch)
    fake_icon_type.__module__ = backend_module
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        assert app._tray_available is True

        icon.visible = False
        app.close_window()

        assert app._tray is icon
        assert app._tray_available is True
        assert events == ["withdraw"]

        app.worker = SimpleNamespace(take_result=lambda: None, busy=False)
        app._credentials_load_pending = False
        app._tray_icon_is_usable = lambda _icon: (_ for _ in ()).throw(
            AssertionError("non-Xorg backend health was probed")
        )
        app._poll_worker_queue()
        assert app.root.state() == "withdrawn"
        assert app._tray_available is True
    finally:
        icon.force_release()


def test_ready_backend_normal_return_disables_tray_withdrawal(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        assert app._tray_available is True

        icon.finish_backend()
        state = app._tray_states[id(icon)]
        assert state.runner_finished.wait(1) is True
        app._poll_tray_events()
        run_root_callbacks(app.root)

        assert app._tray is None
        assert app._tray_available is False
        app.close_window()
        assert "withdraw" not in events
    finally:
        icon.force_release()


def test_close_after_runner_finishes_before_failure_callback_uses_fallback(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        assert app._tray_available is True

        icon.finish_backend()
        state = app._tray_states[id(icon)]
        assert state.runner_finished.wait(1) is True
        assert wait_until(lambda: app._tray_events.empty() is False) is True

        app.close_window()

        assert events == [
            ("warning", "window.tray_unavailable"),
            ("label", app.text("window.tray_unavailable")),
            "iconify",
        ]
        assert app._tray is None
        assert app._tray_available is False

        app._poll_tray_events()
        run_root_callbacks(app.root)
        assert events.count(("warning", "window.tray_unavailable")) == 1
        assert "withdraw" not in events
    finally:
        icon.force_release()


@pytest.mark.parametrize("backend_error", [False, True])
def test_backend_termination_is_queued_without_background_tk_access(backend_error) -> None:
    from net_connector.app import TrayLifecycle

    class TerminatingIcon:
        visible = True

        def __init__(self) -> None:
            self._running = True

        def run(self, *, setup) -> None:
            if backend_error:
                raise RuntimeError("backend terminated")

        def stop(self) -> None:
            self._running = False

    events = []
    app = make_headless_tray_app(events)
    app.root.reject_background_after = True
    app.root.withdraw()
    events.clear()
    icon = TerminatingIcon()
    state = TrayLifecycle()
    app._tray = icon
    app._tray_available = True
    app._tray_states[id(icon)] = state
    state.runner_thread = threading.Thread(target=app._run_tray_icon, args=(icon, state), daemon=True)

    state.runner_thread.start()
    state.runner_thread.join(timeout=1)

    assert state.runner_finished.is_set() is True
    assert app.root.background_after_calls == 0
    assert app.root.state() == "withdrawn"
    assert app._tray_events.empty() is False

    app._poll_tray_events()

    assert app.root.state() == "normal"
    assert events.count("deiconify") == 1
    assert events.count(("warning", "window.tray_unavailable")) == 1
    assert app._tray_events.empty() is True


def test_hidden_window_is_restored_when_ready_backend_returns(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        app.close_window()
        assert app.root.state() == "withdrawn"

        icon.finish_backend()
        state = app._tray_states[id(icon)]
        assert state.runner_finished.wait(1) is True
        app._poll_tray_events()
        run_root_callbacks(app.root)

        assert app.root.state() == "normal"
        assert events.count("deiconify") == 1
        assert events.count(("warning", "window.tray_unavailable")) == 1
        assert app.close_hint_label.text == app.text("window.tray_unavailable")
        app.close_window()
        assert events.count(("warning", "window.tray_unavailable")) == 1
    finally:
        icon.force_release()


def test_hidden_window_is_recoverable_when_ready_backend_raises(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        app.close_window()
        app.root.fail_deiconify = True

        icon.finish_backend(error=True)
        state = app._tray_states[id(icon)]
        assert state.runner_finished.wait(1) is True
        app._poll_tray_events()
        run_root_callbacks(app.root)

        assert app.root.state() == "iconic"
        assert events.count("deiconify") == 1
        assert events.count("iconify") == 1
        assert events.count(("warning", "window.tray_unavailable")) == 1
        assert app.close_hint_label.text == app.text("window.tray_unavailable")
        app.close_window()
        assert events.count(("warning", "window.tray_unavailable")) == 1
    finally:
        icon.force_release()


def test_intentional_exit_from_hidden_window_does_not_warn(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        icon._mark_ready()
        icon._setup_thread.join(timeout=1)
        app._poll_tray_events()
        app.close_window()

        app.exit()

        assert events.count("deiconify") == 0
        assert events.count(("warning", "window.tray_unavailable")) == 0
        assert events[-1] == "destroy"
    finally:
        icon.force_release()


def test_tray_synchronous_startup_failure_never_hides_window(monkeypatch) -> None:
    fake_icon_type = install_fake_pystray(monkeypatch, start_error=True)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = fake_icon_type.instances[-1]
    try:
        state = app._tray_states[id(icon)]
        assert state.runner_finished.wait(1) is True
        app._poll_tray_events()
        run_root_callbacks(app.root)
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        assert icon._setup_thread.daemon is False
        assert icon._setup_thread.is_alive() is False
        assert state.cleanup_thread is None
        app.close_window()
        app.close_window()

        assert app._tray is None
        assert app._tray_available is False
        assert "withdraw" not in events
        assert events.count(("warning", "window.tray_unavailable")) == 1
        assert events.count("iconify") == 2
    finally:
        icon.force_release()


def test_tray_setup_menu_failure_never_marks_ready(monkeypatch) -> None:
    install_fake_pystray(monkeypatch, menu_error=True)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
    assert app._tray_available is False
    app.close_window()
    assert "withdraw" not in events
    icon._mark_ready()
    icon._setup_thread.join(timeout=1)
    app._poll_tray_events()
    run_root_callbacks(app.root)

    assert app._tray is None
    assert app._tray_available is False
    assert icon.stopped is True
    app.close_window()
    assert "withdraw" not in events


def test_tray_pre_readiness_backend_death_is_timed_out_and_joined(monkeypatch) -> None:
    install_fake_pystray(monkeypatch, backend_returns=True)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        assert icon._setup_thread.is_alive() is True
        app._poll_tray_events()
        run_root_callbacks(app.root)

        assert app._tray is None
        assert app._tray_available is False
        assert icon._setup_thread.is_alive() is False
        state = app._tray_states[id(icon)]
        assert state.runner_finished.is_set() is True
        assert state.runner_thread.is_alive() is False
        assert state.cleanup_thread is None
    finally:
        icon.force_release()


def test_exit_before_tray_readiness_joins_setup_waiter(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert wait_until(lambda: icon._setup_thread is not None and icon._setup_thread.ident is not None) is True
        assert icon._setup_thread.is_alive() is True
        app.exit()

        assert icon._setup_thread.is_alive() is False
        assert events[-1] == "destroy"
    finally:
        icon.force_release()


def install_late_backend_pystray(monkeypatch):
    class LateBackendIcon:
        instances = []

        def __init__(self, name, image, title) -> None:
            self.name = name
            self.image = image
            self.title = title
            self.menu = None
            self.visible = False
            self.stopped = False
            self.false_ready_calls = 0
            self._running = False
            self._hwnd = None
            self._setup = None
            self._setup_thread = None
            self._backend_thread = None
            self._backend_initialize = threading.Event()
            self._backend_entered = threading.Event()
            self._backend_stop = threading.Event()
            self._setup_finished = threading.Event()
            self.run_called = False
            self.run_detached_called = False
            setattr(self, "_Icon__queue", queue.Queue())
            self.__class__.instances.append(self)

        def run(self, *, setup) -> None:
            self.run_called = True
            self._setup = setup
            self._setup_thread = threading.Thread(target=self._wait_for_ready, daemon=False)
            self._setup_thread.start()
            self._run_backend()

        def run_detached(self, *, setup) -> None:
            self.run_detached_called = True
            self._setup = setup
            self._setup_thread = threading.Thread(target=self._wait_for_ready, daemon=False)
            self._backend_thread = threading.Thread(target=self._run_backend, daemon=False)
            self._setup_thread.start()
            self._backend_thread.start()

        def _wait_for_ready(self) -> None:
            getattr(self, "_Icon__queue").get()
            self._setup(self)
            self._setup_finished.set()

        def _run_backend(self) -> None:
            self._backend_initialize.wait()
            self._hwnd = object()
            self._mark_ready()
            self._backend_entered.set()
            self._backend_stop.wait()

        def _mark_ready(self) -> None:
            if self._hwnd is None:
                self.false_ready_calls += 1
            self._running = True
            try:
                self.update_menu()
            finally:
                getattr(self, "_Icon__queue").put(True)

        def update_menu(self) -> None:
            pass

        def stop(self) -> None:
            if not self._running:
                return
            if self._hwnd is None:
                raise RuntimeError("uninitialized window handle")
            self.stopped = True
            self._running = False
            self._backend_stop.set()

        def initialize_backend(self) -> None:
            self._backend_initialize.set()

        def emergency_cleanup(self) -> None:
            getattr(self, "_Icon__queue").put(True)
            self._backend_initialize.set()
            self._backend_stop.set()
            for thread in (self._setup_thread, self._backend_thread):
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=1)

    module = SimpleNamespace(
        Icon=LateBackendIcon,
        Menu=lambda *items: tuple(items),
        MenuItem=lambda text, callback, **kwargs: (text, callback, kwargs),
    )
    monkeypatch.setitem(sys.modules, "pystray", module)
    return LateBackendIcon


def run_root_callback(root, delay) -> None:
    index = next(index for index, item in enumerate(root.after_calls) if item[0] == delay)
    _delay, callback = root.after_calls.pop(index)
    callback()


def wait_until(predicate, timeout=1.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


def test_watchdog_honors_backend_readiness_before_tk_callback(monkeypatch) -> None:
    install_late_backend_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        icon.initialize_backend()
        assert icon._setup_finished.wait(1) is True
        assert app._tray_available is False

        run_root_callback(app.root, 5000)

        assert app._tray is icon
        assert icon.stopped is False
        app._poll_tray_events()
        assert app._tray_available is True
    finally:
        icon.emergency_cleanup()


def test_tray_backend_run_is_owned_by_application_daemon(monkeypatch) -> None:
    install_late_backend_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        state = app._tray_states[id(icon)]
        assert icon.run_called is True
        assert icon.run_detached_called is False
        assert state.runner_thread is not None
        assert state.runner_thread.daemon is True
        assert state.runner_thread.is_alive() is True
    finally:
        icon.emergency_cleanup()


def test_watchdog_cancellation_never_fabricates_readiness_and_stops_late_backend(monkeypatch) -> None:
    from net_connector import app as app_module

    assert not hasattr(app_module, "_TRAY_DEFERRED_STOP_SECONDS")
    install_late_backend_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        run_root_callback(app.root, 5000)
        assert wait_until(lambda: icon._setup_thread is not None and not icon._setup_thread.is_alive()) is True
        assert icon.false_ready_calls == 0
        state = app._tray_states[id(icon)]
        cleanup_thread = state.cleanup_thread
        cleanup_thread.join(timeout=0.05)
        assert cleanup_thread.is_alive() is True

        icon.initialize_backend()
        assert icon._backend_entered.wait(1) is True
        state.runner_thread.join(timeout=1)

        assert icon.stopped is True
        assert state.runner_thread.is_alive() is False
        cleanup_thread.join(timeout=1)
        assert cleanup_thread.is_alive() is False
    finally:
        icon.emergency_cleanup()


def test_exit_before_readiness_stops_and_joins_late_backend(monkeypatch) -> None:
    install_late_backend_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        app.exit()
        assert wait_until(lambda: icon._setup_thread is not None and not icon._setup_thread.is_alive()) is True
        assert icon.false_ready_calls == 0
        state = app._tray_states[id(icon)]
        assert state.runner_thread.daemon is True
        assert state.runner_thread.is_alive() is True

        icon.initialize_backend()
        assert icon._backend_entered.wait(1) is True
        state.runner_thread.join(timeout=1)

        assert icon.stopped is True
        assert state.runner_thread.is_alive() is False
        assert events[-1] == "destroy"
        cleanup_thread = app._tray_states[id(icon)].cleanup_thread
        cleanup_thread.join(timeout=1)
        assert cleanup_thread.is_alive() is False
    finally:
        icon.emergency_cleanup()


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
        assert app.settings_button.accessible_name == "Settings"
        assert int(app.connect_button.cget("width")) == 18
        assert app.schedule_checkbutton.winfo_exists()
        assert app.schedule_time_entry.winfo_exists()
        assert root.minsize()[0] >= 420
        assert tuple(root.resizable()) == (1, 1)
        window_size = root.geometry().split("+", 1)[0]
        window_width, window_height = (int(value) for value in window_size.split("x", 1))
        assert window_width >= 420
        assert window_height >= 390

        app.open_settings()
        root.update_idletasks()
        assert app.settings_dialog is not None
        assert tuple(app.settings_dialog.resizable()) == (1, 1)
        assert app.settings_dialog.minsize()[0] >= 420
        assert app.settings_dialog.minsize()[1] >= 340
        assert str(app.language_combobox.cget("state")) == "readonly"
        assert len(app.language_combobox.cget("values")) == 3
        assert str(app.password_entry.cget("show")) == "•"
        assert int(app.show_password_button.cget("width")) >= len(
            app.text("action.show_password")
        )

        app.preview_language("zh")
        root.update_idletasks()
        assert root.title() == "网络连接器"
        assert app.connect_button.cget("text") == "连接"
        assert app.settings_button.accessible_name == "设置"
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


def test_scaled_windows_expand_to_fit_long_status_and_settings(tmp_path) -> None:
    import tkinter as tk

    from net_connector.app import DesktopApp
    from net_connector.storage import SettingsStore

    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f"Tk display unavailable: {error}")

    root.withdraw()
    original_scaling = float(root.tk.call("tk", "scaling"))
    root.tk.call("tk", "scaling", original_scaling * 1.5)
    app = None
    try:
        app = DesktopApp(
            root,
            settings_store=SettingsStore(tmp_path / "settings.json"),
            credential_store_factory=lambda: FakeCredentialStore(),
            network_service=FakeNetwork(),
            auto_status_check=False,
            enable_tray=False,
        )
        app._set_status(
            "error.vpn_detected",
            "failed",
            interfaces=(
                "Corporate VPN Connection With A Long Interface Name",
                "Secondary Encrypted Tunnel Adapter",
            ),
        )
        root.deiconify()
        root.update_idletasks()
        root.update()

        assert tuple(root.resizable()) == (1, 1)
        assert root.winfo_width() >= root.winfo_reqwidth()
        assert root.winfo_height() >= root.winfo_reqheight()
        assert root.minsize()[0] >= root.winfo_reqwidth()
        assert root.minsize()[1] >= root.winfo_reqheight()
        assert app.status_label.winfo_height() >= app.status_label.winfo_reqheight()

        app.open_settings()
        root.update_idletasks()
        root.update()
        dialog = app.settings_dialog
        assert dialog is not None
        assert tuple(dialog.resizable()) == (1, 1)
        assert dialog.winfo_width() >= dialog.winfo_reqwidth()
        assert dialog.winfo_height() >= dialog.winfo_reqheight()
        assert dialog.minsize()[0] >= dialog.winfo_reqwidth()
        assert dialog.minsize()[1] >= dialog.winfo_reqheight()

        app.preview_language("zh")
        app._close_settings_dialog()
        root.update_idletasks()
        root.update()
        assert root.winfo_height() >= root.winfo_reqheight()
        assert root.minsize()[1] >= root.winfo_reqheight()
    finally:
        root.tk.call("tk", "scaling", original_scaling)
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
        warnings = []
        app._show_warning = warnings.append
        app.close_window()
        app.close_window()
        assert app.close_hint_label.cget("text") == app.text("window.tray_unavailable")
        assert warnings == ["window.tray_unavailable"]
    finally:
        app.exit()
