import json
from pathlib import Path

import pytest
from keyring.errors import PasswordSetError

from net_connector.network import Credentials
from net_connector.storage import (
    CREDENTIAL_ACCOUNT,
    CREDENTIAL_SERVICE,
    CredentialError,
    CredentialStore,
    Settings,
    SettingsError,
    SettingsStore,
    default_settings_path,
)


class FakeKeyring:
    def __init__(self):
        self.entries = {}
        self.set_calls = []
        self.get_error = None
        self.set_error = None

    def set_password(self, service, account, value):
        self.set_calls.append((service, account, value))
        if self.set_error:
            raise self.set_error
        self.entries[(service, account)] = value

    def get_password(self, service, account):
        if self.get_error:
            raise self.get_error
        return self.entries.get((service, account))


class ZeroPriorityKeyring(FakeKeyring):
    class Backend:
        priority = 0

    def get_keyring(self):
        return self.Backend()


class PriorityKeyring(FakeKeyring):
    def __init__(self, priority):
        super().__init__()
        self._priority = priority

    def get_keyring(self):
        return type("Backend", (), {"priority": self._priority})()


class ExplodingGetKeyring(FakeKeyring):
    @property
    def get_keyring(self):
        raise RuntimeError("backend inspection detail")


class InvokingErrorKeyring(FakeKeyring):
    def get_keyring(self):
        raise RuntimeError("backend invocation detail")


class PriorityAccessErrorKeyring(FakeKeyring):
    class Backend:
        @property
        def priority(self):
            raise RuntimeError("backend priority detail")

    def get_keyring(self):
        return self.Backend()


class BackendKeyring(FakeKeyring):
    def __init__(self, backend):
        super().__init__()
        self._backend = backend

    def get_keyring(self):
        return self._backend


class UnhashableString(str):
    __hash__ = None


def backend_for_module(module_name):
    backend_type = type("SecureBackend", (), {"priority": 1})
    backend_type.__module__ = module_name
    return backend_type()


@pytest.mark.parametrize(
    ("kwargs",),
    [
        ({"language": "de"},),
        ({"schedule_enabled": 1},),
        ({"schedule_time": "8:30"},),
        ({"schedule_time": "24:00"},),
        ({"schedule_time": "ab:cd"},),
        ({"schedule_time": 830},),
        ({"language": ["en"]},),
        ({"language": UnhashableString("en")},),
        ({"schedule_time": UnhashableString("08:30")},),
        ({"version": True},),
        ({"version": 2},),
    ],
)
def test_settings_validate_rejects_invalid_values(kwargs):
    with pytest.raises(SettingsError):
        Settings(**kwargs).validate()


def test_missing_settings_returns_defaults_without_recovery(tmp_path):
    result = SettingsStore(tmp_path / "settings.json").load()

    assert result.settings == Settings()
    assert result.recovered is False


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "[]",
        json.dumps({"version": 1, "language": "de", "schedule_enabled": False, "schedule_time": "08:30"}),
        json.dumps({"version": 1, "language": "system", "schedule_enabled": 1, "schedule_time": "08:30"}),
        json.dumps({"version": 1, "language": "system", "schedule_enabled": False, "schedule_time": "8:30"}),
        json.dumps({"version": 1, "language": "system", "schedule_enabled": False, "schedule_time": "24:00"}),
        json.dumps({"version": True, "language": "system", "schedule_enabled": False, "schedule_time": "08:30"}),
        json.dumps({"version": 1, "language": "system", "schedule_enabled": False}),
    ],
)
def test_invalid_settings_file_recovers_to_defaults(tmp_path, content):
    path = tmp_path / "settings.json"
    path.write_text(content, encoding="utf-8")

    result = SettingsStore(path).load()

    assert result == type(result)(Settings(), recovered=True)
    assert path.read_text(encoding="utf-8") == content


def test_invalid_utf8_settings_file_recovers_without_output_or_mutation(tmp_path, capsys):
    path = tmp_path / "settings.json"
    contents = b'\xff{"private":"config-content"}'
    path.write_bytes(contents)

    result = SettingsStore(path).load()

    captured = capsys.readouterr()
    assert result.settings == Settings()
    assert result.recovered is True
    assert path.read_bytes() == contents
    assert captured.out == ""
    assert captured.err == ""


def test_settings_read_error_recovers_without_output_or_mutation(tmp_path, monkeypatch, capsys):
    path = tmp_path / "settings.json"
    contents = b'{"private":"config-content"}'
    path.write_bytes(contents)

    def failing_read_text(self, *args, **kwargs):
        raise OSError("file operation detail")

    monkeypatch.setattr("net_connector.storage.Path.read_text", failing_read_text)
    result = SettingsStore(path).load()

    captured = capsys.readouterr()
    assert result.settings == Settings()
    assert result.recovered is True
    assert path.read_bytes() == contents
    assert captured.out == ""
    assert captured.err == ""


def test_save_round_trips_only_supported_settings_fields(tmp_path):
    path = tmp_path / "nested" / "settings.json"
    settings = Settings(language="en", schedule_enabled=True, schedule_time="23:59")

    SettingsStore(path).save(settings)

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert set(payload) == {"version", "language", "schedule_enabled", "schedule_time"}
    assert "username" not in payload
    assert "password" not in payload
    assert SettingsStore(path).load().settings == settings


def test_save_replaces_from_a_temporary_file_in_target_directory(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    calls = []
    real_replace = __import__("os").replace

    def tracking_replace(source, destination):
        calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr("net_connector.storage.os.replace", tracking_replace)
    SettingsStore(path).save(Settings())

    assert len(calls) == 1
    assert calls[0][0].parent == path.parent
    assert calls[0][1] == path
    assert list(tmp_path.glob("*.tmp")) == []


def test_save_replace_failure_preserves_destination_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    old_contents = '{"prior":"config-secret-should-not-leak"}'
    path.write_text(old_contents, encoding="utf-8")

    def failing_replace(source, destination):
        raise OSError("disk backend detail")

    monkeypatch.setattr("net_connector.storage.os.replace", failing_replace)

    with pytest.raises(SettingsError) as raised:
        SettingsStore(path).save(Settings(language="en"))

    assert path.read_text(encoding="utf-8") == old_contents
    assert list(tmp_path.glob("*.tmp")) == []
    assert "config-secret-should-not-leak" not in str(raised.value)
    assert str(path) not in str(raised.value)


def test_default_settings_path_uses_windows_appdata_and_fallback(tmp_path):
    appdata = tmp_path / "appdata"
    assert default_settings_path("Windows", {"APPDATA": str(appdata)}, tmp_path / "home") == (
        appdata / "PortableNetworkConnector" / "settings.json"
    )
    assert default_settings_path("Windows", {}, tmp_path / "home") == (
        tmp_path / "home" / "AppData" / "Roaming" / "PortableNetworkConnector" / "settings.json"
    )


def test_default_settings_path_uses_linux_xdg_and_fallback(tmp_path):
    config_home = tmp_path / "xdg"
    assert default_settings_path("Linux", {"XDG_CONFIG_HOME": str(config_home)}, tmp_path / "home") == (
        config_home / "portable-network-connector" / "settings.json"
    )
    assert default_settings_path("Linux", {}, tmp_path / "home") == (
        tmp_path / "home" / ".config" / "portable-network-connector" / "settings.json"
    )


@pytest.mark.parametrize("xdg_config_home", ["relative-config", "   "])
def test_default_settings_path_rejects_relative_or_blank_xdg_config_home(tmp_path, xdg_config_home):
    home = tmp_path / "home"

    result = default_settings_path("Linux", {"XDG_CONFIG_HOME": xdg_config_home}, home)

    assert result == home / ".config" / "portable-network-connector" / "settings.json"


def test_credential_store_round_trips_fixed_json_entry():
    backend = FakeKeyring()
    credentials = Credentials("  demo-user  ", "demo-password")

    CredentialStore(backend).save(credentials)

    assert backend.set_calls[0][:2] == (CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT)
    assert json.loads(backend.set_calls[0][2]) == {"username": "demo-user", "password": "demo-password"}
    assert CredentialStore(backend).load() == Credentials("demo-user", "demo-password")


def test_missing_credential_entry_returns_none():
    assert CredentialStore(FakeKeyring()).load() is None


def test_empty_credentials_do_not_call_backend():
    backend = FakeKeyring()

    with pytest.raises(CredentialError):
        CredentialStore(backend).save(Credentials("  ", "   "))

    assert backend.set_calls == []


@pytest.mark.parametrize(
    ("username", "password"),
    [
        (b"demo-user", "demo-password"),
        (1, "demo-password"),
        (object(), "demo-password"),
        ("demo-user", b"demo-password"),
        ("demo-user", 1),
        ("demo-user", object()),
    ],
)
def test_non_string_credentials_are_rejected_before_backend_call(username, password):
    backend = FakeKeyring()

    with pytest.raises(CredentialError):
        CredentialStore(backend).save(Credentials(username, password))

    assert backend.set_calls == []


@pytest.mark.parametrize("operation", ["set", "get"])
def test_keyring_errors_are_generic_and_do_not_leak_secrets(operation, capsys):
    backend = FakeKeyring()
    secret = "dummy-secret-must-not-leak"
    if operation == "set":
        backend.set_error = PasswordSetError("backend diagnostic")
        action = lambda: CredentialStore(backend).save(Credentials("demo-user", secret))
    else:
        backend.get_error = RuntimeError("backend diagnostic")
        action = lambda: CredentialStore(backend).load()

    with pytest.raises(CredentialError) as raised:
        action()

    captured = capsys.readouterr()
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert secret not in captured.out + captured.err
    assert "backend diagnostic" not in str(raised.value)


def test_set_backend_exception_has_no_context_or_credential_leak(capsys, caplog):
    backend = FakeKeyring()
    secret = "dummy-secret-in-serialized-entry"
    serialized = json.dumps({"username": "demo-user", "password": secret}, separators=(",", ":"))
    backend.set_error = RuntimeError(serialized)

    with pytest.raises(CredentialError) as raised:
        CredentialStore(backend).save(Credentials("demo-user", secret))

    captured = capsys.readouterr()
    assert raised.value.__context__ is None
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert secret not in captured.out + captured.err + caplog.text


def test_get_backend_exception_has_no_context_or_credential_leak(capsys, caplog):
    backend = FakeKeyring()
    secret = "dummy-secret-from-backend"
    backend.get_error = RuntimeError(secret)

    with pytest.raises(CredentialError) as raised:
        CredentialStore(backend).load()

    captured = capsys.readouterr()
    assert raised.value.__context__ is None
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert secret not in captured.out + captured.err + caplog.text


def test_malformed_credential_json_has_no_context_or_credential_leak(capsys, caplog):
    backend = FakeKeyring()
    secret = "dummy-secret-in-json-document"
    backend.entries[(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT)] = f'{{"username":"demo-user","password":"{secret}"'

    with pytest.raises(CredentialError) as raised:
        CredentialStore(backend).load()

    captured = capsys.readouterr()
    assert raised.value.__context__ is None
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert secret not in captured.out + captured.err + caplog.text


def test_save_error_traceback_storage_frames_do_not_retain_credentials():
    # Caller frames retain their own arguments; inspect only frames owned by storage.py.
    def storage_frame_reprs(error):
        frame_count = 0
        values = []
        traceback = error.__traceback__
        while traceback is not None:
            frame = traceback.tb_frame
            if Path(frame.f_code.co_filename).name == "storage.py":
                frame_count += 1
                for value in frame.f_locals.values():
                    try:
                        values.append(repr(value))
                    except Exception:
                        values.append("<unrepresentable>")
            traceback = traceback.tb_next
        return frame_count, values

    secret = "dummy-password-not-retained-in-storage-traceback"
    validation_backend = FakeKeyring()
    with pytest.raises(CredentialError) as validation_error:
        CredentialStore(validation_backend).save(Credentials("  ", secret))

    serialized = json.dumps({"username": "demo-user", "password": secret}, separators=(",", ":"))
    failing_backend = FakeKeyring()
    failing_backend.set_error = RuntimeError(serialized)
    with pytest.raises(CredentialError) as backend_error:
        CredentialStore(failing_backend).save(Credentials("demo-user", secret))

    assert validation_error.value.__context__ is None
    assert backend_error.value.__context__ is None
    validation_frames, validation_values = storage_frame_reprs(validation_error.value)
    backend_frames, backend_values = storage_frame_reprs(backend_error.value)
    assert validation_frames > 0
    assert backend_frames > 0
    assert all(secret not in value for value in validation_values)
    assert all(secret not in value for value in backend_values)


def test_priority_zero_keyring_backend_is_rejected():
    with pytest.raises(CredentialError):
        CredentialStore(ZeroPriorityKeyring())


@pytest.mark.parametrize("priority", [0, -1, None, "1", True, float("nan"), float("inf"), float("-inf")])
def test_unusable_keyring_priorities_are_rejected(priority):
    with pytest.raises(CredentialError):
        CredentialStore(PriorityKeyring(priority))


def test_positive_priority_custom_backend_is_rejected(monkeypatch):
    monkeypatch.setattr("net_connector.storage.platform.system", lambda: "Windows")

    with pytest.raises(CredentialError):
        CredentialStore(BackendKeyring(backend_for_module("example.insecure_backend")))


@pytest.mark.parametrize(
    ("system_name", "module_name"),
    [("Windows", "keyring.backends.Windows"), ("Linux", "keyring.backends.SecretService")],
)
def test_secure_platform_keyring_backend_is_accepted(monkeypatch, system_name, module_name):
    monkeypatch.setattr("net_connector.storage.platform.system", lambda: system_name)

    CredentialStore(BackendKeyring(backend_for_module(module_name)))


def test_unsupported_platform_backend_is_rejected(monkeypatch):
    monkeypatch.setattr("net_connector.storage.platform.system", lambda: "Darwin")

    with pytest.raises(CredentialError):
        CredentialStore(BackendKeyring(backend_for_module("keyring.backends.SecretService")))


@pytest.mark.parametrize(
    "backend",
    [ExplodingGetKeyring(), InvokingErrorKeyring(), PriorityAccessErrorKeyring()],
)
def test_keyring_backend_inspection_error_is_generic(backend, capsys):
    with pytest.raises(CredentialError) as raised:
        CredentialStore(backend)

    captured = capsys.readouterr()
    assert "backend" not in str(raised.value)
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.parametrize(
    "stored_value",
    [
        "not json",
        "[]",
        json.dumps({"username": "", "password": "demo-password"}),
        json.dumps({"username": "demo-user", "password": ""}),
        json.dumps({"username": 7, "password": "demo-password"}),
        json.dumps({"username": "demo-user", "password": 7}),
    ],
)
def test_corrupt_or_invalid_credentials_are_rejected(stored_value):
    backend = FakeKeyring()
    backend.entries[(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT)] = stored_value

    with pytest.raises(CredentialError):
        CredentialStore(backend).load()
