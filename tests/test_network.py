"""Tests for the captive portal connection service."""

from __future__ import annotations

import json
import logging
from http.client import IncompleteRead
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

from net_connector.network import (
    ConnectionCode,
    ConnectionResult,
    Credentials,
    NetworkService,
    rc4_hex,
)


class FakeResponse:
    def __init__(self, body: str | bytes, status: int = 200, *, read_error: BaseException | None = None) -> None:
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.status = status
        self.read_error = read_error
        self.read_sizes = []
        self.closed = False
        self.close_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.read_error is not None:
            raise self.read_error
        return self.body if size < 0 else self.body[:size]

    def getcode(self) -> int:
        return self.status


class ScriptedOpener:
    def __init__(self, *steps: FakeResponse | BaseException) -> None:
        self.steps = list(steps)
        self.calls = []

    def open(self, request, timeout: int):
        self.calls.append((request, timeout))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class TrackingHTTPError(HTTPError):
    def __init__(self, status: int) -> None:
        super().__init__("http://1.1.1.3/ac_portal/login.php", status, "error", {}, None)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def offline_response() -> FakeResponse:
    return FakeResponse("captive portal")


def online_response() -> FakeResponse:
    return FakeResponse("Microsoft Connect Test")


def make_service(opener: ScriptedOpener, *, clock=lambda: 1, sleep=lambda _: None) -> NetworkService:
    return NetworkService(opener=opener, clock_ms=clock, sleep=sleep)


def test_decorated_connectivity_marker_is_online_with_one_get_and_no_post():
    opener = ScriptedOpener(FakeResponse("diagnostic: Microsoft Connect Test complete"))

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.ALREADY_ONLINE
    assert [(request.get_method(), request.full_url, timeout) for request, timeout in opener.calls] == [
        ("GET", "http://www.msftconnecttest.com/connecttest.txt", 5)
    ]
    assert opener.calls[0][0].get_header("User-agent") == "Mozilla/5.0"


@pytest.mark.parametrize(
    "credentials",
    [Credentials("   ", "password"), Credentials("alice", ""), Credentials("alice", "   ")],
)
def test_missing_credentials_performs_no_http(credentials: Credentials):
    opener = ScriptedOpener()

    result = make_service(opener).connect(credentials)

    assert result.code is ConnectionCode.MISSING_CREDENTIALS
    assert opener.calls == []


def test_offline_portal_login_then_online_returns_connected():
    probe_before = offline_response()
    warmup = FakeResponse("portal page")
    login = FakeResponse('{"success": true}')
    probe_after = online_response()
    opener = ScriptedOpener(probe_before, warmup, login, probe_after)
    sleeps = []

    result = make_service(opener, clock=lambda: 1700000000000, sleep=sleeps.append).connect(
        Credentials("  alice  ", "secret-value")
    )

    assert result.code is ConnectionCode.CONNECTED
    assert sleeps == [3]
    assert len(opener.calls) == 4
    assert all(response.closed for response in (probe_before, warmup, login, probe_after))


def test_portal_request_contains_exact_form_headers_urls_and_timeouts():
    opener = ScriptedOpener(
        offline_response(),
        FakeResponse("portal page"),
        FakeResponse('{"success": true}'),
        online_response(),
    )
    tag = "1700000000000"

    result = make_service(opener, clock=lambda: int(tag)).connect(Credentials("  alice  ", "secret-value"))

    assert result.code is ConnectionCode.CONNECTED
    assert [(request.full_url, timeout) for request, timeout in opener.calls] == [
        ("http://www.msftconnecttest.com/connecttest.txt", 5),
        (
            "http://1.1.1.3/ac_portal/default/pc.html?template=default&tabs=pwd&vlanid=0&"
            "url=http://www.msftconnecttest.com%2fredirect",
            8,
        ),
        ("http://1.1.1.3/ac_portal/login.php", 10),
        ("http://www.msftconnecttest.com/connecttest.txt", 5),
    ]
    warmup, post = opener.calls[1][0], opener.calls[2][0]
    assert warmup.get_method() == "GET"
    assert warmup.get_header("User-agent") == "Mozilla/5.0"
    assert post.get_method() == "POST"
    assert post.get_header("User-agent") == "Mozilla/5.0"
    assert post.get_header("Referer") == (
        "http://1.1.1.3/ac_portal/default/pc.html?template=default&tabs=pwd&vlanid=0&"
        "url=http://www.msftconnecttest.com%2fredirect"
    )
    assert post.get_header("Origin") == "http://1.1.1.3"
    assert post.get_header("Content-type") == "application/x-www-form-urlencoded"
    assert post.data == (
        "opr=pwdLogin&userName=alice&pwd=" + rc4_hex("secret-value", tag) + "&auth_tag=" + tag + "&rememberPwd=0"
    ).encode()
    assert parse_qs(post.data.decode()) == {
        "opr": ["pwdLogin"],
        "userName": ["alice"],
        "pwd": [rc4_hex("secret-value", tag)],
        "auth_tag": [tag],
        "rememberPwd": ["0"],
    }
    assert b"secret-value" not in post.data


def test_warmup_timeout_returns_portal_unreachable():
    probe = offline_response()
    opener = ScriptedOpener(probe, TimeoutError("unavailable"))

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert "secret-value" not in result.detail
    assert probe.closed


@pytest.mark.parametrize("status", [401, 403])
def test_post_auth_error_returns_portal_rejected(status: int):
    error = TrackingHTTPError(status)
    opener = ScriptedOpener(offline_response(), FakeResponse("portal page"), error)

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_REJECTED
    assert error.close_calls == 1


def test_probe_incomplete_read_returns_offline_and_closes_response():
    response = FakeResponse("ignored", read_error=IncompleteRead(b"partial", 16))

    assert make_service(ScriptedOpener(response)).is_online() is False
    assert response.closed


def test_post_incomplete_read_returns_portal_unreachable_and_closes_responses():
    probe = offline_response()
    warmup = FakeResponse("portal page")
    login = FakeResponse("ignored", read_error=IncompleteRead(b"partial", 16))
    opener = ScriptedOpener(probe, warmup, login)

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert all(response.closed for response in (probe, warmup, login))


def test_warmup_incomplete_read_error_returns_portal_unreachable():
    probe = offline_response()
    opener = ScriptedOpener(probe, IncompleteRead(b"partial", 16))

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert probe.closed


def test_warmup_non_success_status_returns_portal_unreachable_and_closes_response():
    probe = offline_response()
    warmup = FakeResponse("portal page", status=500)
    opener = ScriptedOpener(probe, warmup)

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert probe.closed and warmup.closed


def test_post_non_success_status_returns_portal_unreachable_without_sleep_or_verify():
    probe = offline_response()
    warmup = FakeResponse("portal page")
    login = FakeResponse("failure", status=500)
    opener = ScriptedOpener(probe, warmup, login)
    sleeps = []

    result = make_service(opener, sleep=sleeps.append).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert sleeps == []
    assert len(opener.calls) == 3
    assert all(response.closed for response in (probe, warmup, login))


def test_raised_non_auth_http_error_returns_portal_unreachable_and_closes_error():
    error = TrackingHTTPError(500)
    opener = ScriptedOpener(offline_response(), FakeResponse("portal page"), error)

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert error.close_calls == 1


def test_json_success_false_returns_portal_rejected():
    opener = ScriptedOpener(offline_response(), FakeResponse("portal page"), FakeResponse('{"success": false}'))

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_REJECTED


@pytest.mark.parametrize(("field", "value"), [("result", " FaIlEd "), ("status", " ERROR ")])
def test_json_rejection_fields_are_whitespace_and_case_normalized(field: str, value: str):
    opener = ScriptedOpener(
        offline_response(),
        FakeResponse("portal page"),
        FakeResponse(json.dumps({field: value})),
    )

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_REJECTED


def test_json_success_with_empty_error_is_not_rejected():
    opener = ScriptedOpener(
        offline_response(),
        FakeResponse("portal page"),
        FakeResponse(json.dumps({"success": True, "error": ""})),
        offline_response(),
    )

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.VERIFICATION_FAILED


def test_completed_post_with_final_offline_probe_returns_verification_failed():
    opener = ScriptedOpener(
        offline_response(),
        FakeResponse("portal page"),
        FakeResponse('{"result": "ok"}'),
        offline_response(),
    )

    result = make_service(opener).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.VERIFICATION_FAILED


def test_unexpected_exception_returns_internal_error_without_secret():
    opener = ScriptedOpener(offline_response(), FakeResponse("portal page"))

    result = make_service(opener, clock=lambda: (_ for _ in ()).throw(RuntimeError("secret-value"))).connect(
        Credentials("alice", "secret-value")
    )

    assert result.code is ConnectionCode.INTERNAL_ERROR
    assert "secret-value" not in result.detail
    assert "secret-value" not in repr(result)


def test_failed_result_does_not_expose_credentials_request_or_response(capsys, caplog):
    username = "dummy-user-x17"
    password = "dummy-password-y29"
    tag = "1700000000000"
    encrypted_password = rc4_hex(password, tag)
    form = (
        f"opr=pwdLogin&userName={username}&pwd={encrypted_password}&auth_tag={tag}&rememberPwd=0"
    )
    response_body = "distinctive-portal-response-z83"
    opener = ScriptedOpener(
        offline_response(),
        FakeResponse("portal page"),
        FakeResponse(response_body),
        offline_response(),
    )
    caplog.set_level(logging.DEBUG)

    result = make_service(opener, clock=lambda: int(tag)).connect(Credentials(username, password))

    captured = capsys.readouterr()
    exposed_text = "\n".join((result.detail, repr(result), captured.out, captured.err, caplog.text))
    assert result.code is ConnectionCode.VERIFICATION_FAILED
    assert opener.calls[2][0].data.decode("ascii") == form
    for sensitive_value in (username, password, tag, encrypted_password, form, response_body):
        assert sensitive_value not in exposed_text


def test_oversized_connectivity_body_returns_offline_after_bounded_read():
    response = FakeResponse(b"x" * 65537)

    assert make_service(ScriptedOpener(response)).is_online() is False
    assert response.read_sizes == [65537]
    assert response.closed


def test_oversized_login_response_returns_portal_unreachable_after_bounded_read():
    probe = offline_response()
    warmup = FakeResponse("portal page")
    login = FakeResponse(b"x" * 65537)
    opener = ScriptedOpener(probe, warmup, login)
    sleeps = []

    result = make_service(opener, sleep=sleeps.append).connect(Credentials("alice", "secret-value"))

    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert login.read_sizes == [65537]
    assert sleeps == []
    assert all(response.closed for response in (probe, warmup, login))


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ConnectionCode.ALREADY_ONLINE, True),
        (ConnectionCode.CONNECTED, True),
        (ConnectionCode.MISSING_CREDENTIALS, False),
        (ConnectionCode.PORTAL_UNREACHABLE, False),
        (ConnectionCode.PORTAL_REJECTED, False),
        (ConnectionCode.VERIFICATION_FAILED, False),
        (ConnectionCode.INTERNAL_ERROR, False),
    ],
)
def test_connection_result_succeeded_truth_table(code: ConnectionCode, expected: bool):
    assert ConnectionResult(code).succeeded is expected
