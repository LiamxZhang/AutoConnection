"""Local settings and OS-keyring credential storage."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from numbers import Real
import os
from pathlib import Path
import platform
import re
import tempfile

import keyring

from net_connector.network import Credentials


CREDENTIAL_SERVICE = "portable-network-connector"
CREDENTIAL_ACCOUNT = "network-login"
_SETTINGS_FIELDS = frozenset({"version", "language", "schedule_enabled", "schedule_time"})
_CREDENTIAL_FIELDS = frozenset({"username", "password"})
_SCHEDULE_TIME = re.compile(r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")


class SettingsError(Exception):
    """Raised when settings cannot be validated or stored safely."""


class CredentialError(Exception):
    """Raised when credentials cannot be validated or accessed safely."""


@dataclass(frozen=True)
class Settings:
    """User preferences that are safe to keep in the local settings file."""

    version: int = 1
    language: str = "system"
    schedule_enabled: bool = False
    schedule_time: str = "08:30"

    def validate(self) -> Settings:
        """Return this settings object after enforcing the supported schema."""
        if type(self.version) is not int or self.version != 1:
            raise SettingsError("Settings are invalid.")
        if type(self.language) is not str or self.language not in {"system", "zh", "en"}:
            raise SettingsError("Settings are invalid.")
        if type(self.schedule_enabled) is not bool:
            raise SettingsError("Settings are invalid.")
        if type(self.schedule_time) is not str or not _SCHEDULE_TIME.fullmatch(self.schedule_time):
            raise SettingsError("Settings are invalid.")
        return self


@dataclass(frozen=True)
class LoadResult:
    """The loaded settings and whether an invalid file was ignored."""

    settings: Settings
    recovered: bool = False


def default_settings_path(platform_name=None, environ=None, home=None) -> Path:
    """Return the platform-appropriate settings file path."""
    system_name = platform.system() if platform_name is None else platform_name
    environment = os.environ if environ is None else environ
    home_path = Path.home() if home is None else Path(home)

    if str(system_name).casefold() == "windows":
        appdata = environment.get("APPDATA")
        base = Path(appdata) if appdata else home_path / "AppData" / "Roaming"
        return base / "PortableNetworkConnector" / "settings.json"

    config_home = environment.get("XDG_CONFIG_HOME")
    if isinstance(config_home, str) and config_home.strip() and Path(config_home).is_absolute():
        base = Path(config_home)
    else:
        base = home_path / ".config"
    return base / "portable-network-connector" / "settings.json"


class SettingsStore:
    """Read and atomically replace the non-sensitive settings document."""

    def __init__(self, path=None) -> None:
        self._path = Path(path) if path is not None else default_settings_path()

    def load(self) -> LoadResult:
        """Load valid settings, falling back without overwriting bad input."""
        try:
            contents = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return LoadResult(Settings())
        except (OSError, UnicodeError):
            return LoadResult(Settings(), recovered=True)

        try:
            payload = json.loads(contents)
            if not isinstance(payload, dict) or set(payload) != _SETTINGS_FIELDS:
                raise SettingsError("Settings are invalid.")
            settings = Settings(**payload).validate()
        except (json.JSONDecodeError, TypeError, SettingsError):
            return LoadResult(Settings(), recovered=True)
        return LoadResult(settings)

    def save(self, settings: Settings) -> None:
        """Validate and atomically replace the settings document."""
        temp_name = None
        try:
            if not isinstance(settings, Settings):
                raise SettingsError("Settings are invalid.")
            settings.validate()
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": settings.version,
                "language": settings.language,
                "schedule_enabled": settings.schedule_enabled,
                "schedule_time": settings.schedule_time,
            }
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=".settings-",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_name = stream.name
                json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            # Same-directory replacement is atomic; parent-directory fsync is intentionally omitted.
            os.replace(temp_name, self._path)
            temp_name = None
        except Exception as error:
            if temp_name is not None:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
            if isinstance(error, SettingsError):
                raise
            raise SettingsError("Settings could not be saved.") from None


class CredentialStore:
    """Store portal credentials only in the operating system keyring."""

    def __init__(self, keyring_api=None) -> None:
        self._keyring = keyring if keyring_api is None else keyring_api
        self._validate_backend()

    def save(self, credentials: Credentials) -> None:
        """Save one JSON credential entry under the fixed service and account."""
        invalid_credentials = False
        username = password = None
        try:
            username = credentials.username
            password = credentials.password
            if type(username) is not str or type(password) is not str:
                raise TypeError
            username = username.strip()
            if not username or not password.strip():
                raise ValueError
        except Exception:
            invalid_credentials = True
        if invalid_credentials:
            credentials = username = password = None
            raise CredentialError("Credentials are invalid.")

        serialization_failed = False
        value = None
        try:
            value = json.dumps({"username": username, "password": password}, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            serialization_failed = True
        if serialization_failed:
            credentials = username = password = value = None
            raise CredentialError("Credentials are invalid.")

        backend_failed = False
        try:
            self._keyring.set_password(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT, value)
        except Exception:
            backend_failed = True
        if backend_failed:
            credentials = username = password = value = None
            raise CredentialError("Credential storage is unavailable.")

    def load(self) -> Credentials | None:
        """Load the fixed credential entry, rejecting malformed keyring values."""
        backend_failed = False
        value = None
        try:
            value = self._keyring.get_password(CREDENTIAL_SERVICE, CREDENTIAL_ACCOUNT)
        except Exception:
            backend_failed = True
        if backend_failed:
            value = None
            raise CredentialError("Credential storage is unavailable.")
        if value is None:
            return None

        invalid_credentials = False
        payload = username = password = None
        try:
            payload = json.loads(value)
            if not isinstance(payload, dict) or set(payload) != _CREDENTIAL_FIELDS:
                raise ValueError
            username = payload["username"]
            password = payload["password"]
            if type(username) is not str or not username.strip() or type(password) is not str or not password.strip():
                raise ValueError
        except (TypeError, ValueError, json.JSONDecodeError):
            invalid_credentials = True
        if invalid_credentials:
            value = payload = username = password = None
            raise CredentialError("Stored credentials are invalid.")
        return Credentials(username, password)

    def _validate_backend(self) -> None:
        """Reject configured keyring backends that cannot securely store secrets."""
        backend_invalid = False
        missing = object()
        try:
            get_keyring = getattr(self._keyring, "get_keyring", missing)
            if get_keyring is missing:
                # APIs without get_keyring are deliberately trusted injected test adapters.
                return
            backend = get_keyring()
            priority = getattr(backend, "priority")
            expected_module = {
                "Windows": "keyring.backends.Windows",
                "Linux": "keyring.backends.SecretService",
            }.get(platform.system())
            if (
                isinstance(priority, bool)
                or not isinstance(priority, Real)
                or not math.isfinite(priority)
                or priority <= 0
                or expected_module is None
                or type(backend).__module__ != expected_module
            ):
                raise ValueError
        except Exception:
            backend_invalid = True
        if backend_invalid:
            raise CredentialError("Credential storage is unavailable.")
