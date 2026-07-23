"""Tk desktop application and side-effect-free worker helpers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from net_connector.i18n import Translator, resolve_language
from net_connector.network import ConnectionCode, ConnectionResult, Credentials, NetworkService
from net_connector.scheduler import DailyScheduler, ScheduleError
from net_connector.storage import CredentialError, CredentialStore, Settings, SettingsStore


_RESULT_MESSAGE_KEYS = {
    ConnectionCode.ALREADY_ONLINE: "status.already_online",
    ConnectionCode.CONNECTED: "status.connected",
    ConnectionCode.MISSING_CREDENTIALS: "error.missing_credentials",
    ConnectionCode.PORTAL_UNREACHABLE: "error.portal_unreachable",
    ConnectionCode.PORTAL_REJECTED: "error.portal_rejected",
    ConnectionCode.VERIFICATION_FAILED: "error.verification_failed",
    ConnectionCode.INTERNAL_ERROR: "error.internal",
}
_TRAY_START_TIMEOUT_MS = 5000
_TRAY_SETUP_JOIN_SECONDS = 1.0


@dataclass(frozen=True)
class WorkerOutcome:
    """Safe connection worker result with no exception or credential payload."""

    result: ConnectionResult
    credential_store_failed: bool = False


@dataclass(frozen=True)
class CredentialLoadOutcome:
    """Credential-load state whose representation never contains credentials."""

    credentials: Credentials | None = field(default=None, repr=False)
    failed: bool = False


@dataclass
class TrayLifecycle:
    """Thread-safe readiness and cancellation state for one tray icon."""

    backend_ready: threading.Event = field(default_factory=threading.Event)
    cancelled: threading.Event = field(default_factory=threading.Event)
    runner_finished: threading.Event = field(default_factory=threading.Event)
    runner_thread: threading.Thread | None = field(default=None, repr=False)
    cleanup_thread: threading.Thread | None = field(default=None, repr=False)


def result_message_key(result: ConnectionResult) -> str:
    """Return the catalog key for a classified connection result."""
    if not result.succeeded and result.vpn_interfaces:
        return "error.vpn_detected"
    return _RESULT_MESSAGE_KEYS[result.code]


def perform_connection(credential_store_factory, network_service) -> WorkerOutcome:
    """Load credentials and connect, reducing all failures to safe outcomes."""
    try:
        store = credential_store_factory()
        credentials = store.load()
    except CredentialError:
        return WorkerOutcome(ConnectionResult(ConnectionCode.INTERNAL_ERROR), True)
    except Exception:
        return WorkerOutcome(ConnectionResult(ConnectionCode.INTERNAL_ERROR))

    if credentials is None:
        return WorkerOutcome(ConnectionResult(ConnectionCode.MISSING_CREDENTIALS))

    try:
        result = network_service.connect(credentials)
    except Exception:
        return WorkerOutcome(ConnectionResult(ConnectionCode.INTERNAL_ERROR))
    return WorkerOutcome(result)


class ConnectionWorker:
    """Run one connection operation at a time and expose results via a queue."""

    def __init__(self, credential_store_factory, network_service, thread_factory=threading.Thread) -> None:
        self._credential_store_factory = credential_store_factory
        self._network_service = network_service
        self._thread_factory = thread_factory
        self._results = queue.Queue()
        self._busy = False

    @property
    def busy(self) -> bool:
        return self._busy

    def start_connection(self) -> bool:
        return self._start(self._connect)

    def start_status_check(self) -> bool:
        return self._start(self._check_status)

    def start_credentials_load(self) -> bool:
        return self._start(self._load_credentials)

    def _start(self, target) -> bool:
        if self._busy:
            return False
        self._busy = True
        try:
            thread = self._thread_factory(target=target, daemon=True)
            thread.start()
        except Exception:
            self._busy = False
            return False
        return True

    def _connect(self) -> None:
        outcome = perform_connection(self._credential_store_factory, self._network_service)
        self._results.put(("connect", outcome))

    def _check_status(self) -> None:
        try:
            online = bool(self._network_service.is_online())
        except Exception:
            online = False
        self._results.put(("status", online))

    def _load_credentials(self) -> None:
        try:
            credentials = self._credential_store_factory().load()
            outcome = CredentialLoadOutcome(credentials)
        except Exception:
            outcome = CredentialLoadOutcome(failed=True)
        self._results.put(("credentials", outcome))

    def take_result(self):
        try:
            result = self._results.get_nowait()
        except queue.Empty:
            return None
        self._busy = False
        return result


def language_options(translator: Translator) -> tuple[str, str, str]:
    """Return localized combobox labels in persisted-mode order."""
    return tuple(
        translator.text(key)
        for key in ("settings.language.system", "settings.language.zh", "settings.language.en")
    )


def language_mode_for_label(label: str, translator: Translator) -> str:
    """Translate a localized combobox label back to a persisted mode."""
    return dict(zip(language_options(translator), ("system", "zh", "en"), strict=True))[label]


def persist_schedule(settings_store, settings: Settings, enabled: bool, schedule_time: str, now):
    """Validate and atomically persist a schedule edit before exposing new state."""
    scheduler = DailyScheduler(schedule_time, now)
    updated = replace(settings, schedule_enabled=enabled, schedule_time=schedule_time)
    updated.validate()
    settings_store.save(updated)
    return updated, scheduler if enabled else None


def marshal_to_tk(root, callback):
    """Wrap a foreign-thread callback so it only schedules Tk-thread work."""
    def marshaled(*_args, **_kwargs):
        root.after(0, callback)

    return marshaled


def set_accessible_name(widget, name: str) -> None:
    """Attach localized accessibility metadata for adapters and tests."""
    # Tk has no cross-platform accessible-name option; retain explicit metadata for platform adapters.
    widget.accessible_name = name


class Tooltip:
    """Small hover tooltip for an icon-only control."""

    def __init__(self, widget, text_provider) -> None:
        self._widget = widget
        self._text_provider = text_provider
        self._window = None
        widget.bind("<Enter>", self._show, add=True)
        widget.bind("<Leave>", self._hide, add=True)
        widget.bind("<FocusIn>", self._show, add=True)
        widget.bind("<FocusOut>", self._hide, add=True)

    def _show(self, _event=None) -> None:
        if self._window is not None:
            return
        x = self._widget.winfo_rootx()
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        window = tk.Toplevel(self._widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        ttk.Label(window, text=self._text_provider(), padding=(7, 3)).pack()
        self._window = window

    def _hide(self, _event=None) -> None:
        if self._window is not None:
            self._window.destroy()
            self._window = None


class DesktopApp:
    """Compact bilingual Tk application coordinating storage, work, and tray state."""

    _DOT_COLORS = {
        "waiting": "#d69a21",
        "busy": "#d69a21",
        "connected": "#176b4d",
        "failed": "#b43b3b",
        "offline": "#68736e",
    }

    def __init__(
        self,
        root,
        *,
        settings_store=None,
        credential_store_factory=None,
        network_service=None,
        now_provider=None,
        thread_factory=threading.Thread,
        auto_status_check: bool = True,
        enable_tray: bool = True,
    ) -> None:
        self.root = root
        self.settings_store = settings_store if settings_store is not None else SettingsStore()
        self.credential_store_factory = (
            credential_store_factory if credential_store_factory is not None else CredentialStore
        )
        self.network_service = network_service if network_service is not None else NetworkService()
        self.now_provider = now_provider if now_provider is not None else datetime.now
        self.worker = ConnectionWorker(
            self.credential_store_factory,
            self.network_service,
            thread_factory=thread_factory,
        )
        try:
            loaded = self.settings_store.load()
            self.settings = loaded.settings
            recovered = loaded.recovered
        except Exception:
            self.settings = Settings()
            recovered = True

        self.translator = Translator(resolve_language(self.settings.language))
        self.scheduler = self._make_scheduler(self.settings)
        self.last_check = None
        self._status_key = "status.waiting"
        self._status_interfaces = ()
        self._failed = False
        self._exiting = False
        self._tray = None
        self._tray_available = False
        self._tray_notice_shown = False
        self._tray_requested = enable_tray
        self._tray_states = {}
        self.settings_dialog = None
        self._dialog_original_mode = None
        self._dialog_selected_mode = None
        self._credentials_load_pending = False

        self._configure_root()
        self._configure_styles()
        self._build_main_ui()
        self.refresh_text()
        self.root.protocol("WM_DELETE_WINDOW", self.close_window)
        self.root.after(75, self._poll_worker_queue)
        self.root.after(1000, self._poll_schedule)
        if recovered:
            self.root.after(0, lambda: self._show_warning("error.settings_recovered"))
        if auto_status_check:
            self._start_status_check()
        if enable_tray:
            self.root.after(0, self._start_tray)

    def _configure_root(self) -> None:
        self.root.geometry("420x390")
        self.root.minsize(420, 390)
        self.root.resizable(False, False)
        self.root.configure(background="#f7f8f7")

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        style.configure("App.TFrame", background="#f7f8f7")
        style.configure("Surface.TFrame", background="#ffffff")
        style.configure("Header.TLabel", background="#f7f8f7", foreground="#16231e", font=("Segoe UI", 15, "bold"))
        style.configure("Status.TLabel", background="#ffffff", foreground="#16231e", font=("Segoe UI", 12, "bold"))
        style.configure("Muted.TLabel", background="#ffffff", foreground="#68736e", font=("Segoe UI", 9))
        style.configure("Bottom.TLabel", background="#f7f8f7", foreground="#68736e", font=("Segoe UI", 8))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), foreground="#176b4d")

    def _build_main_ui(self) -> None:
        outer = ttk.Frame(self.root, style="App.TFrame", padding=(24, 18, 24, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x")
        self.title_label = ttk.Label(header, style="Header.TLabel")
        self.title_label.pack(side="left", fill="x", expand=True)
        self.settings_button = ttk.Button(
            header,
            text="⚙",
            width=3,
            command=self.open_settings,
            takefocus=True,
        )
        self.settings_button.pack(side="right", ipadx=2, ipady=5)
        self.settings_tooltip = Tooltip(self.settings_button, lambda: self.translator.text("tooltip.settings"))

        status = ttk.Frame(outer, style="Surface.TFrame", padding=(18, 18))
        status.pack(fill="x", pady=(18, 12))
        status_row = ttk.Frame(status, style="Surface.TFrame")
        status_row.pack(fill="x")
        self.status_dot = tk.Canvas(status_row, width=18, height=18, highlightthickness=0, background="#ffffff")
        self.status_dot.pack(side="left", padx=(0, 10))
        self._dot_item = self.status_dot.create_oval(3, 3, 15, 15, fill=self._DOT_COLORS["waiting"], outline="")
        self.status_label = ttk.Label(status_row, style="Status.TLabel", anchor="w", wraplength=320)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.last_check_label = ttk.Label(status, style="Muted.TLabel")
        self.last_check_label.pack(anchor="w", padx=(28, 0), pady=(5, 0))

        self.connect_button = ttk.Button(
            outer,
            width=18,
            style="Primary.TButton",
            command=self.start_connection,
        )
        self.connect_button.pack(pady=(2, 14))

        schedule = ttk.Frame(outer, style="Surface.TFrame", padding=(16, 13))
        schedule.pack(fill="x")
        self.schedule_enabled_var = tk.BooleanVar(value=self.settings.schedule_enabled)
        self.schedule_checkbutton = ttk.Checkbutton(
            schedule,
            variable=self.schedule_enabled_var,
            command=self._apply_schedule_edit,
        )
        self.schedule_checkbutton.grid(row=0, column=0, sticky="w", columnspan=3)
        self.schedule_every_label = ttk.Label(schedule, style="Muted.TLabel")
        self.schedule_every_label.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.schedule_time_var = tk.StringVar(value=self.settings.schedule_time)
        self.schedule_time_entry = ttk.Entry(schedule, width=7, textvariable=self.schedule_time_var, justify="center")
        self.schedule_time_entry.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(10, 0))
        self.schedule_time_entry.bind("<Return>", self._apply_schedule_edit)
        self.schedule_time_entry.bind("<FocusOut>", self._apply_schedule_edit)
        self.schedule_next_label = ttk.Label(schedule, style="Muted.TLabel", anchor="e")
        self.schedule_next_label.grid(row=1, column=2, sticky="e", padx=(10, 0), pady=(10, 0))
        schedule.columnconfigure(2, weight=1)

        self.close_hint_label = ttk.Label(outer, style="Bottom.TLabel", anchor="center", wraplength=360)
        self.close_hint_label.pack(fill="x", pady=(13, 0))

    def _make_scheduler(self, settings: Settings):
        if not settings.schedule_enabled:
            return None
        try:
            return DailyScheduler(settings.schedule_time, self.now_provider())
        except Exception:
            return None

    def text(self, key: str, **kwargs) -> str:
        return self.translator.text(key, **kwargs)

    def refresh_text(self) -> None:
        self.root.title(self.text("app.title"))
        self.title_label.configure(text=self.text("app.title"))
        set_accessible_name(self.settings_button, self.text("tooltip.settings"))
        self.connect_button.configure(text=self.text("action.retry" if self._failed else "action.connect"))
        self.schedule_checkbutton.configure(text=self.text("schedule.title"))
        self.schedule_every_label.configure(text=self.text("schedule.every_day"))
        close_key = "window.tray_unavailable" if self._tray_notice_shown else "window.close_to_tray"
        self.close_hint_label.configure(text=self.text(close_key))
        self._render_status()
        self._render_schedule()
        self._refresh_dialog_text()
        self._refresh_tray()

    def _render_status(self) -> None:
        kwargs = {}
        if self._status_key == "error.vpn_detected":
            kwargs["interfaces"] = ", ".join(self._status_interfaces)
        self.status_label.configure(text=self.text(self._status_key, **kwargs))
        if self.last_check is None:
            self.last_check_label.configure(text=self.text("status.last_check.never"))
        else:
            self.last_check_label.configure(text=self.text("status.last_check", time=self.last_check.strftime("%H:%M")))

    def _render_schedule(self) -> None:
        if self.scheduler is None:
            message = self.text("schedule.disabled")
        else:
            message = self.text("schedule.next_run", time=self.scheduler.next_run.strftime("%Y-%m-%d %H:%M"))
        self.schedule_next_label.configure(text=message)

    def _set_status(self, key: str, color: str, *, interfaces=()) -> None:
        self._status_key = key
        self._status_interfaces = tuple(interfaces)
        self.status_dot.itemconfigure(self._dot_item, fill=self._DOT_COLORS[color])
        self._render_status()

    def _start_status_check(self) -> None:
        if self.worker.start_status_check():
            self._set_busy(True, "status.checking")

    def start_connection(self) -> bool:
        if not self.worker.start_connection():
            return False
        self._failed = False
        self._set_busy(True, "status.connecting")
        return True

    def _set_busy(self, busy: bool, status_key: str | None = None) -> None:
        self.connect_button.configure(state="disabled" if busy else "normal")
        if status_key is not None:
            self._set_status(status_key, "busy")
        self.connect_button.configure(text=self.text("action.retry" if self._failed else "action.connect"))

    def _poll_worker_queue(self) -> None:
        if self._exiting:
            return
        while True:
            item = self.worker.take_result()
            if item is None:
                break
            operation, outcome = item
            if operation == "connect":
                self._handle_connection_outcome(outcome)
            elif operation == "status":
                self._handle_status_outcome(outcome)
            elif operation == "credentials":
                self._handle_credentials_outcome(outcome)
        if self._credentials_load_pending and not self.worker.busy:
            dialog = self.settings_dialog
            if dialog is not None and dialog.winfo_exists() and self.worker.start_credentials_load():
                self._credentials_load_pending = False
        self.root.after(75, self._poll_worker_queue)

    def _handle_status_outcome(self, online: bool) -> None:
        self.last_check = self.now_provider()
        self._failed = False
        self._set_status("status.connected" if online else "status.offline", "connected" if online else "offline")
        self._set_busy(False)

    def _handle_connection_outcome(self, outcome: WorkerOutcome) -> None:
        self.last_check = self.now_provider()
        result = outcome.result
        key = "error.credential_store" if outcome.credential_store_failed else result_message_key(result)
        self._failed = not result.succeeded
        self._set_status(key, "connected" if result.succeeded else "failed", interfaces=result.vpn_interfaces)
        self._set_busy(False)
        if result.code is ConnectionCode.MISSING_CREDENTIALS and self.settings_dialog is None:
            self.open_settings()

    def _handle_credentials_outcome(self, outcome: CredentialLoadOutcome) -> None:
        if self.settings_dialog is None or not self.settings_dialog.winfo_exists():
            return
        if outcome.failed:
            self._show_error("error.credential_store", parent=self.settings_dialog)
            return
        if outcome.credentials is not None:
            self.username_var.set(outcome.credentials.username)
            self.password_var.set(outcome.credentials.password)

    def _poll_schedule(self) -> None:
        if self._exiting:
            return
        if self.scheduler is not None:
            try:
                due = self.scheduler.poll(self.now_provider())
            except ScheduleError:
                due = False
            if due:
                self.start_connection()
            self._render_schedule()
        self.root.after(1000, self._poll_schedule)

    def _apply_schedule_edit(self, _event=None) -> None:
        previous_settings = self.settings
        previous_scheduler = self.scheduler
        try:
            updated, scheduler = persist_schedule(
                self.settings_store,
                self.settings,
                bool(self.schedule_enabled_var.get()),
                self.schedule_time_var.get(),
                self.now_provider(),
            )
        except ScheduleError:
            self._restore_schedule_widgets(previous_settings, previous_scheduler)
            self._show_error("schedule.invalid_time")
            return
        except Exception:
            self._restore_schedule_widgets(previous_settings, previous_scheduler)
            self._show_error("error.settings_save")
            return
        self.settings = updated
        self.scheduler = scheduler
        self._render_schedule()

    def _restore_schedule_widgets(self, settings: Settings, scheduler) -> None:
        self.settings = settings
        self.scheduler = scheduler
        self.schedule_enabled_var.set(settings.schedule_enabled)
        self.schedule_time_var.set(settings.schedule_time)
        self._render_schedule()

    def open_settings(self) -> None:
        if self.settings_dialog is not None and self.settings_dialog.winfo_exists():
            self.settings_dialog.deiconify()
            self.settings_dialog.lift()
            return
        self._dialog_original_mode = self.settings.language
        self._dialog_selected_mode = self.settings.language
        dialog = tk.Toplevel(self.root)
        self.settings_dialog = dialog
        dialog.geometry("420x340")
        dialog.minsize(420, 340)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.protocol("WM_DELETE_WINDOW", self.cancel_settings)
        body = ttk.Frame(dialog, padding=(24, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        self.username_label = ttk.Label(body)
        self.username_label.grid(row=0, column=0, sticky="w", pady=(0, 12))
        self.username_var = tk.StringVar()
        self.username_entry = ttk.Entry(body, textvariable=self.username_var)
        self.username_entry.grid(row=0, column=1, columnspan=2, sticky="ew", pady=(0, 12))
        self.password_label = ttk.Label(body)
        self.password_label.grid(row=1, column=0, sticky="w", pady=(0, 12))
        self.password_var = tk.StringVar()
        self.password_entry = ttk.Entry(body, textvariable=self.password_var, show="•")
        self.password_entry.grid(row=1, column=1, sticky="ew", pady=(0, 12))
        self.show_password_button = ttk.Button(body, width=9, command=self._toggle_password)
        self.show_password_button.grid(row=1, column=2, padx=(8, 0), pady=(0, 12))
        self.credential_hint_label = ttk.Label(body, foreground="#68736e")
        self.credential_hint_label.grid(row=2, column=1, columnspan=2, sticky="w", pady=(0, 18))
        self.language_label = ttk.Label(body)
        self.language_label.grid(row=3, column=0, sticky="w")
        self.language_var = tk.StringVar()
        self.language_combobox = ttk.Combobox(body, textvariable=self.language_var, state="readonly")
        self.language_combobox.grid(row=3, column=1, columnspan=2, sticky="ew")
        self.language_combobox.bind("<<ComboboxSelected>>", self._on_language_selected)

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, columnspan=3, sticky="e", pady=(38, 0))
        self.cancel_button = ttk.Button(actions, width=10, command=self.cancel_settings)
        self.cancel_button.pack(side="left", padx=(0, 8))
        self.save_button = ttk.Button(actions, width=10, command=self.save_settings)
        self.save_button.pack(side="left")
        self._refresh_dialog_text()
        dialog.grab_set()
        self.username_entry.focus_set()
        self._credentials_load_pending = not self.worker.start_credentials_load()

    def _refresh_dialog_text(self) -> None:
        dialog = self.settings_dialog
        if dialog is None or not dialog.winfo_exists():
            return
        dialog.title(self.text("settings.title"))
        self.username_label.configure(text=self.text("settings.username"))
        self.password_label.configure(text=self.text("settings.password"))
        self.credential_hint_label.configure(text=self.text("settings.credential_hint"))
        self.language_label.configure(text=self.text("settings.language"))
        self.show_password_button.configure(
            text=self.text("action.hide_password" if not self.password_entry.cget("show") else "action.show_password")
        )
        self.cancel_button.configure(text=self.text("action.cancel"))
        self.save_button.configure(text=self.text("action.save"))
        options = language_options(self.translator)
        self.language_combobox.configure(values=options)
        mode = self._dialog_selected_mode or self.settings.language
        self.language_var.set(options[("system", "zh", "en").index(mode)])

    def _toggle_password(self) -> None:
        self.password_entry.configure(show="" if self.password_entry.cget("show") else "•")
        self._refresh_dialog_text()

    def _on_language_selected(self, _event=None) -> None:
        try:
            mode = language_mode_for_label(self.language_var.get(), self.translator)
        except KeyError:
            return
        self.preview_language(mode)

    def preview_language(self, mode: str) -> None:
        self._dialog_selected_mode = mode
        self.translator.set_language(resolve_language(mode))
        self.refresh_text()

    def cancel_settings(self) -> None:
        if self._dialog_original_mode is not None:
            self.translator.set_language(resolve_language(self._dialog_original_mode))
        self._close_settings_dialog()
        self.refresh_text()

    def save_settings(self) -> None:
        username = self.username_var.get().strip()
        password = self.password_var.get()
        if not username or not password.strip():
            self._show_error("error.missing_credentials", parent=self.settings_dialog)
            return
        mode = self._dialog_selected_mode or self.settings.language
        updated = replace(self.settings, language=mode)
        try:
            self.credential_store_factory().save(Credentials(username, password))
        except Exception:
            self._show_error("error.credential_store", parent=self.settings_dialog)
            return
        try:
            self.settings_store.save(updated)
        except Exception:
            self._show_error("error.settings_save", parent=self.settings_dialog)
            return
        self.settings = updated
        self._close_settings_dialog()
        self.refresh_text()

    def _close_settings_dialog(self) -> None:
        dialog = self.settings_dialog
        self.settings_dialog = None
        self._dialog_original_mode = None
        self._dialog_selected_mode = None
        self._credentials_load_pending = False
        if dialog is not None and dialog.winfo_exists():
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

    def _show_error(self, key: str, *, parent=None) -> None:
        messagebox.showerror(self.text("dialog.error"), self.text(key), parent=parent or self.root)

    def _show_warning(self, key: str, *, parent=None) -> None:
        messagebox.showwarning(self.text("dialog.info"), self.text(key), parent=parent or self.root)

    def _start_tray(self) -> None:
        if self._exiting or not self._tray_requested:
            return
        icon = None
        try:
            import pystray
            from PIL import Image, ImageDraw

            image = Image.new("RGBA", (64, 64), "#f7f8f7")
            draw = ImageDraw.Draw(image)
            draw.ellipse((9, 9, 55, 55), fill="#176b4d")
            draw.ellipse((25, 25, 39, 39), fill="#ffffff")
            icon = pystray.Icon("net-connector", image, self.text("app.title"))
            self._tray = icon
            state = TrayLifecycle()
            self._tray_states[id(icon)] = state
            self._configure_tray(icon, update_backend=False)
            # Icon.run may run off the main thread on the supported Windows/Linux targets.
            state.runner_thread = threading.Thread(
                target=self._run_tray_icon,
                args=(icon, state),
                daemon=True,
            )
            state.runner_thread.start()
            self.root.after(_TRAY_START_TIMEOUT_MS, lambda: self._expire_tray_startup(icon))
        except Exception:
            self._finish_tray_failure(icon, wait_for_backend=False)

    def _run_tray_icon(self, icon, state: TrayLifecycle) -> None:
        try:
            if not state.cancelled.is_set():
                icon.run(setup=self._on_tray_ready)
        except Exception:
            pass
        finally:
            state.runner_finished.set()
        if not state.cancelled.is_set():
            try:
                self.root.after(0, lambda: self._finish_tray_failure(icon, wait_for_backend=False))
            except Exception:
                self._finish_tray_failure(icon, wait_for_backend=False, recover_hidden_window=False)

    def _on_tray_ready(self, icon) -> None:
        state = self._tray_states.get(id(icon))
        if state is None or state.cancelled.is_set():
            return
        state.backend_ready.set()
        try:
            self.root.after(0, lambda: self._complete_tray_ready(icon, state))
        except Exception:
            self._finish_tray_failure(icon)

    def _complete_tray_ready(self, icon, state: TrayLifecycle) -> None:
        if state.cancelled.is_set() or icon is not self._tray or self._exiting:
            return
        try:
            self._configure_tray(icon, update_backend=True)
            icon.visible = True
        except Exception:
            self._finish_tray_failure(icon)
            return
        self._finish_tray_ready(icon)

    def _expire_tray_startup(self, icon) -> None:
        state = self._tray_states.get(id(icon))
        if icon is self._tray and not self._tray_available and not (state and state.backend_ready.is_set()):
            self._finish_tray_failure(icon)

    def _finish_tray_ready(self, icon) -> None:
        if self._exiting or icon is not self._tray:
            self._stop_tray(icon)
            return
        self._tray_available = True

    def _finish_tray_failure(
        self,
        icon,
        *,
        wait_for_backend: bool = True,
        recover_hidden_window: bool = True,
    ) -> None:
        state = self._tray_states.get(id(icon)) if icon is not None else None
        restore_window = (
            recover_hidden_window
            and not self._exiting
            and icon is self._tray
            and self._tray_available
        )
        if state is not None:
            state.cancelled.set()
        if icon is self._tray:
            self._tray = None
        self._tray_available = False
        self._release_tray_setup_waiter(icon)
        if icon is None:
            return
        if getattr(icon, "_running", False):
            self._stop_and_join_tray_backend(icon, state)
        elif wait_for_backend and state is not None and not state.runner_finished.is_set():
            self._start_deferred_tray_stop(icon, state)
        if restore_window:
            self._restore_window_after_tray_failure()

    def _restore_window_after_tray_failure(self) -> None:
        try:
            if self.root.state() != "withdrawn":
                return
            self.root.deiconify()
        except Exception:
            try:
                self.root.iconify()
            except Exception:
                pass
        self._show_tray_unavailable_notice()

    def _show_tray_unavailable_notice(self) -> None:
        if self._tray_notice_shown:
            return
        self._tray_notice_shown = True
        self._show_warning("window.tray_unavailable")
        self.close_hint_label.configure(text=self.text("window.tray_unavailable"))

    def _release_tray_setup_waiter(self, icon) -> None:
        if icon is None:
            return
        setup_thread = getattr(icon, "_setup_thread", None)
        if not isinstance(setup_thread, threading.Thread) or not setup_thread.is_alive():
            return

        # pystray 0.19.5 exposes no public pre-ready cancellation. Release only
        # its setup queue; calling _mark_ready() would fabricate backend state.
        try:
            getattr(icon, "_Icon__queue").put_nowait(True)
        except Exception:
            return
        if setup_thread is not threading.current_thread():
            setup_thread.join(_TRAY_SETUP_JOIN_SECONDS)

    def _start_deferred_tray_stop(self, icon, state: TrayLifecycle) -> None:
        if state.cleanup_thread is not None and state.cleanup_thread.is_alive():
            return
        state.cleanup_thread = threading.Thread(
            target=self._wait_for_real_tray_readiness,
            args=(icon, state),
            daemon=True,
        )
        state.cleanup_thread.start()

    def _wait_for_real_tray_readiness(self, icon, state: TrayLifecycle) -> None:
        while not state.runner_finished.is_set():
            self._release_tray_setup_waiter(icon)
            if getattr(icon, "_running", False):
                self._stop_and_join_tray_backend(icon, state)
                return
            state.runner_finished.wait(0.01)
        self._release_tray_setup_waiter(icon)

    def _stop_and_join_tray_backend(self, icon, state: TrayLifecycle | None) -> None:
        self._stop_tray(icon)
        runner_thread = state.runner_thread if state is not None else None
        if isinstance(runner_thread, threading.Thread) and runner_thread is not threading.current_thread():
            runner_thread.join(_TRAY_SETUP_JOIN_SECONDS)

    @staticmethod
    def _stop_tray(icon) -> None:
        if icon is None:
            return
        try:
            icon.stop()
        except Exception:
            pass

    def _configure_tray(self, icon, *, update_backend: bool) -> None:
        import pystray

        icon.title = self.text("app.title")
        icon.menu = pystray.Menu(
            pystray.MenuItem(self.text("tray.show"), marshal_to_tk(self.root, self.show_window), default=True),
            pystray.MenuItem(self.text("tray.connect"), marshal_to_tk(self.root, self.start_connection)),
            pystray.MenuItem(self.text("tray.exit"), marshal_to_tk(self.root, self.exit)),
        )
        if update_backend:
            icon.update_menu()

    def _refresh_tray(self) -> None:
        icon = self._tray
        if icon is None:
            return
        try:
            self._configure_tray(icon, update_backend=self._tray_available)
        except Exception:
            self._finish_tray_failure(icon)

    def show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()

    def close_window(self) -> None:
        if self._tray_available:
            self.root.withdraw()
            return
        self._show_tray_unavailable_notice()
        self.root.iconify()

    def exit(self) -> None:
        if self._exiting:
            return
        self._exiting = True
        tray = self._tray
        self._finish_tray_failure(tray)
        try:
            self._close_settings_dialog()
            self.root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    """Start the desktop application."""
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()
