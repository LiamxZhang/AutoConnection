"""Smoke and integration tests for the desktop application shell."""

from __future__ import annotations

import importlib
import queue
import sys
import threading
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


class ScalarVar:
    def __init__(self, value) -> None:
        self.value = value

    def get(self):
        return self.value

    def set(self, value) -> None:
        self.value = value


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

    def after(self, delay, callback) -> None:
        self.after_calls.append((delay, callback))

    def withdraw(self) -> None:
        self.events.append("withdraw")

    def iconify(self) -> None:
        self.events.append("iconify")

    def destroy(self) -> None:
        self.events.append("destroy")


class HeadlessLabel:
    def __init__(self, events) -> None:
        self.events = events
        self.text = None

    def configure(self, *, text) -> None:
        self.text = text
        self.events.append(("label", text))


def install_fake_pystray(monkeypatch, *, start_error=False, menu_error=False):
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
            self._backend_thread = None
            setattr(self, "_Icon__queue", queue.Queue())
            self.__class__.instances.append(self)

        def run_detached(self, *, setup) -> None:
            self.setup = setup
            self._setup_thread = threading.Thread(target=self._wait_for_ready, daemon=False)
            self._setup_thread.start()
            if start_error:
                raise RuntimeError("backend unavailable")
            self._backend_thread = threading.Thread(target=lambda: None, daemon=False)
            self._backend_thread.start()

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

        def force_release(self) -> None:
            getattr(self, "_Icon__queue").put(True)
            if self._setup_thread is not None:
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
    app.settings_dialog = None
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
    run_root_callbacks(app.root)
    assert icon.visible is True
    assert app._tray_available is True

    app.close_window()
    assert events[-1] == "withdraw"
    assert events.count(("warning", "window.tray_unavailable")) == 1


def test_tray_synchronous_startup_failure_never_hides_window(monkeypatch) -> None:
    fake_icon_type = install_fake_pystray(monkeypatch, start_error=True)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = fake_icon_type.instances[-1]
    try:
        assert icon._setup_thread.daemon is False
        assert icon._setup_thread.is_alive() is False
        assert app._tray_states[id(icon)].cleanup_thread is None
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
    assert app._tray_available is False
    app.close_window()
    assert "withdraw" not in events
    icon._mark_ready()
    icon._setup_thread.join(timeout=1)
    run_root_callbacks(app.root)

    assert app._tray is None
    assert app._tray_available is False
    assert icon.stopped is True
    app.close_window()
    assert "withdraw" not in events


def test_tray_pre_readiness_backend_death_is_timed_out_and_joined(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        assert icon._setup_thread.is_alive() is True
        run_root_callbacks(app.root)

        assert app._tray is None
        assert app._tray_available is False
        assert icon._setup_thread.is_alive() is False
        cleanup_thread = app._tray_states[id(icon)].cleanup_thread
        cleanup_thread.join(timeout=1)
        assert cleanup_thread.is_alive() is False
    finally:
        icon.force_release()


def test_exit_before_tray_readiness_joins_setup_waiter(monkeypatch) -> None:
    install_fake_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
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
            setattr(self, "_Icon__queue", queue.Queue())
            self.__class__.instances.append(self)

        def run_detached(self, *, setup) -> None:
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
                if thread is not None:
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
        run_root_callback(app.root, 0)
        assert app._tray_available is True
    finally:
        icon.emergency_cleanup()


def test_watchdog_cancellation_never_fabricates_readiness_and_stops_late_backend(monkeypatch) -> None:
    install_late_backend_pystray(monkeypatch)
    events = []
    app = make_headless_tray_app(events)

    app._start_tray()
    icon = app._tray
    try:
        run_root_callback(app.root, 5000)
        assert icon._setup_thread.is_alive() is False
        assert icon.false_ready_calls == 0

        icon.initialize_backend()
        assert icon._backend_entered.wait(1) is True
        icon._backend_thread.join(timeout=1)

        assert icon.stopped is True
        assert icon._backend_thread.is_alive() is False
        cleanup_thread = app._tray_states[id(icon)].cleanup_thread
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
        assert icon._setup_thread.is_alive() is False
        assert icon.false_ready_calls == 0

        icon.initialize_backend()
        assert icon._backend_entered.wait(1) is True
        icon._backend_thread.join(timeout=1)

        assert icon.stopped is True
        assert icon._backend_thread.is_alive() is False
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
