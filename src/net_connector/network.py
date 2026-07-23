"""Legacy-compatible network protocol helpers and portal login support."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from http.client import HTTPException
import json
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from net_connector.vpn import find_active_vpn_interfaces


PORTAL_HOST = "1.1.1.3"
LOGIN_API = "http://1.1.1.3/ac_portal/login.php"
LOGIN_PAGE = (
    "http://1.1.1.3/ac_portal/default/pc.html?template=default&tabs=pwd&vlanid=0&"
    "url=http://www.msftconnecttest.com%2fredirect"
)
CONNECTIVITY_URL = "http://www.msftconnecttest.com/connecttest.txt"
_USER_AGENT = "Mozilla/5.0"
_REJECTED_VALUES = frozenset({"fail", "failed", "error", "rejected"})
_MAX_RESPONSE_BYTES = 64 * 1024


@dataclass(frozen=True)
class Credentials:
    """Portal credentials provided by the caller."""

    username: str
    password: str


class ConnectionCode(Enum):
    """Classified outcomes of a portal connection attempt."""

    ALREADY_ONLINE = auto()
    CONNECTED = auto()
    MISSING_CREDENTIALS = auto()
    PORTAL_UNREACHABLE = auto()
    PORTAL_REJECTED = auto()
    VERIFICATION_FAILED = auto()
    INTERNAL_ERROR = auto()


@dataclass(frozen=True)
class ConnectionResult:
    """A connection outcome suitable for presentation by a caller."""

    code: ConnectionCode
    detail: str = ""
    vpn_interfaces: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.code in {ConnectionCode.ALREADY_ONLINE, ConnectionCode.CONNECTED}


def rc4_hex(source: str, key_text: str) -> str:
    """Return the legacy RC4 transformation as lowercase hexadecimal."""
    source = str(source).strip()
    key_text = str(key_text)
    if not key_text:
        raise ValueError("key must not be empty")

    state = list(range(256))
    key_index = 0
    for index in range(256):
        key_index = (key_index + state[index] + ord(key_text[index % len(key_text)])) % 256
        state[index], state[key_index] = state[key_index], state[index]

    index = key_index = 0
    encrypted = []
    for character in source:
        index = (index + 1) % 256
        key_index = (key_index + state[index]) % 256
        state[index], state[key_index] = state[key_index], state[index]
        encrypted.append(f"{ord(character) ^ state[(state[index] + state[key_index]) % 256]:02x}")
    return "".join(encrypted)


class NetworkService:
    """Connect to the internal captive portal and verify external access."""

    def __init__(
        self,
        opener=None,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
        vpn_detector: Callable[[], object] | None = None,
    ) -> None:
        self._opener = opener if opener is not None else build_opener(ProxyHandler({}))
        self._clock_ms = clock_ms if clock_ms is not None else lambda: int(time.time() * 1000)
        self._sleep = sleep if sleep is not None else time.sleep
        self._vpn_detector = vpn_detector if vpn_detector is not None else find_active_vpn_interfaces

    def is_online(self) -> bool:
        """Return whether the expected connectivity probe content is reachable."""
        request = Request(CONNECTIVITY_URL, headers={"User-Agent": _USER_AGENT})
        try:
            response = self._opener.open(request, timeout=5)
            with response:
                status = self._response_status(response)
                body = self._read_response(response)
            return status == 200 and body is not None and "Microsoft Connect Test" in body
        except HTTPError as error:
            try:
                return False
            finally:
                self._close_safely(error)
        except (URLError, TimeoutError, OSError, UnicodeError, HTTPException):
            return False

    def connect(self, credentials: Credentials) -> ConnectionResult:
        """Attempt portal authentication and verify the resulting connectivity."""
        username = credentials.username.strip()
        if not username or not credentials.password.strip():
            return ConnectionResult(ConnectionCode.MISSING_CREDENTIALS, "Credentials are required.")

        attempt_started = False
        try:
            if self.is_online():
                return ConnectionResult(ConnectionCode.ALREADY_ONLINE)

            attempt_started = True
            warmup = Request(LOGIN_PAGE, headers={"User-Agent": _USER_AGENT})
            try:
                response = self._opener.open(warmup, timeout=8)
                with response:
                    if not self._is_success_status(self._response_status(response)):
                        return self._failure(ConnectionCode.PORTAL_UNREACHABLE, "Portal is unavailable.")
            except HTTPError as error:
                try:
                    return self._failure(ConnectionCode.PORTAL_UNREACHABLE, "Portal is unavailable.")
                finally:
                    self._close_safely(error)
            except (URLError, TimeoutError, OSError, HTTPException):
                return self._failure(ConnectionCode.PORTAL_UNREACHABLE, "Portal is unavailable.")

            auth_tag = str(self._clock_ms())
            form = urlencode(
                {
                    "opr": "pwdLogin",
                    "userName": username,
                    "pwd": rc4_hex(credentials.password, auth_tag),
                    "auth_tag": auth_tag,
                    "rememberPwd": "0",
                }
            ).encode("ascii")
            request = Request(
                LOGIN_API,
                data=form,
                headers={
                    "User-Agent": _USER_AGENT,
                    "Referer": LOGIN_PAGE,
                    "Origin": f"http://{PORTAL_HOST}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            try:
                response = self._opener.open(request, timeout=10)
                with response:
                    status = self._response_status(response)
                    if status in {401, 403}:
                        return self._failure(ConnectionCode.PORTAL_REJECTED, "Portal rejected the login.")
                    if not self._is_success_status(status):
                        return self._failure(ConnectionCode.PORTAL_UNREACHABLE, "Portal is unavailable.")
                    body = self._read_response(response)
                    if body is None:
                        return self._failure(ConnectionCode.PORTAL_UNREACHABLE, "Portal is unavailable.")
            except HTTPError as error:
                try:
                    if error.code in {401, 403}:
                        return self._failure(ConnectionCode.PORTAL_REJECTED, "Portal rejected the login.")
                    return self._failure(ConnectionCode.PORTAL_UNREACHABLE, "Portal is unavailable.")
                finally:
                    self._close_safely(error)
            except (URLError, TimeoutError, OSError, HTTPException):
                return self._failure(ConnectionCode.PORTAL_UNREACHABLE, "Portal is unavailable.")

            if self._is_rejection(body):
                return self._failure(ConnectionCode.PORTAL_REJECTED, "Portal rejected the login.")

            self._sleep(3)
            if self.is_online():
                return ConnectionResult(ConnectionCode.CONNECTED)
            return self._failure(ConnectionCode.VERIFICATION_FAILED, "Connection could not be verified.")
        except Exception:
            if attempt_started:
                return self._failure(ConnectionCode.INTERNAL_ERROR, "Connection failed unexpectedly.")
            return ConnectionResult(ConnectionCode.INTERNAL_ERROR, "Connection failed unexpectedly.")

    def _failure(self, code: ConnectionCode, detail: str) -> ConnectionResult:
        """Return a failed result enriched with optional, non-fatal VPN evidence."""
        try:
            detected_interfaces = self._vpn_detector()
            interfaces = (
                ()
                if isinstance(detected_interfaces, (str, bytes))
                else tuple(name for name in detected_interfaces if isinstance(name, str))
            )
        except Exception:
            interfaces = ()
        return ConnectionResult(code, detail, interfaces)

    @staticmethod
    def _response_status(response) -> int:
        status = getattr(response, "status", None)
        return response.getcode() if status is None else status

    @staticmethod
    def _is_success_status(status: int) -> bool:
        return 200 <= status < 300

    @staticmethod
    def _read_response(response) -> str | None:
        body = response.read(_MAX_RESPONSE_BYTES + 1)
        if len(body) > _MAX_RESPONSE_BYTES:
            return None
        return body.decode("utf-8", errors="replace")

    @staticmethod
    def _close_safely(response) -> None:
        try:
            response.close()
        except Exception:
            pass

    @staticmethod
    def _is_rejection(body: str) -> bool:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("success") is False:
            return True
        return any(
            isinstance(payload.get(field), str) and payload[field].strip().casefold() in _REJECTED_VALUES
            for field in ("result", "status")
        )
