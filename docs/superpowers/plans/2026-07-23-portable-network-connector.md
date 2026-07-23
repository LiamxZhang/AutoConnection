# Portable Network Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Build a bilingual, portable Windows/Linux desktop application that logs into the existing work-network portal manually or once per day, stores credentials in the operating-system keyring, and diagnoses likely VPN interference.

**Architecture:** A small src-layout Python package separates portal networking, VPN discovery, settings/keyring access, daily scheduling, localization, and Tkinter UI/tray orchestration. Network work runs off the Tk thread, while all UI updates return through Tk callbacks. PyInstaller produces one executable per operating system and GitHub Actions tests and packages both targets.

**Tech Stack:** Python 3.12, Tkinter/ttk, urllib, keyring, psutil, pystray, Pillow, pytest, PyInstaller, GitHub Actions

---

## File Map

- Create pyproject.toml: package metadata, dependencies, pytest configuration, and console entry point.
- Create src/net_connector/__init__.py: package version.
- Create src/net_connector/__main__.py: GUI process entry point.
- Create src/net_connector/network.py: RC4 compatibility, HTTP portal client, connectivity probing, and classified results.
- Create src/net_connector/vpn.py: active-interface inspection and VPN heuristics.
- Create src/net_connector/storage.py: validated settings, atomic JSON writes, and keyring credentials.
- Create src/net_connector/scheduler.py: one-future-trigger daily scheduler.
- Create src/net_connector/i18n.py: Chinese/English catalogs and system-language resolution.
- Create src/net_connector/app.py: Tkinter windows, worker coordination, tray, and UI state.
- Create tests/: focused tests for each non-visual module plus a GUI import smoke test.
- Create scripts/build.ps1 and scripts/build.sh: reproducible local single-file builds.
- Create .github/workflows/ci.yml and .github/workflows/release.yml: cross-platform verification and release artifacts.
- Create README.md: safe end-user and developer instructions.
- Delete lab_net_login_no_requests.py and 上网脚本使用命令.txt after the compatible algorithm is covered by tests; neither legacy file may be committed.

### Task 1: Project Skeleton and RC4 Compatibility

**Files:**
- Create: pyproject.toml
- Create: src/net_connector/__init__.py
- Create: src/net_connector/network.py
- Create: tests/test_network_crypto.py
- Create: README.md
- Delete: lab_net_login_no_requests.py
- Delete: 上网脚本使用命令.txt

- [ ] **Step 1: Write the failing encryption tests**

~~~python
from net_connector.network import rc4_hex


def test_rc4_hex_matches_legacy_algorithm():
    assert rc4_hex("password", "1700000000000") == "78cdea3da257edba"


def test_rc4_hex_rejects_empty_key():
    import pytest

    with pytest.raises(ValueError, match="key"):
        rc4_hex("password", "")
~~~

- [ ] **Step 2: Create package metadata and run the focused test**

pyproject.toml must declare Python 3.12, the src package directory, runtime dependencies keyring, psutil, pystray and Pillow, and dev dependencies pytest, pytest-cov and pyinstaller. Configure pytest with pythonpath = ["src"] and testpaths = ["tests"].

Run: python -m pytest tests/test_network_crypto.py -v

Expected: FAIL because net_connector.network does not exist.

- [ ] **Step 3: Port the compatible encryption function**

~~~python
def rc4_hex(source: str, key_text: str) -> str:
    source = str(source).strip()
    key_text = str(key_text)
    if not key_text:
        raise ValueError("key must not be empty")

    key = [ord(key_text[index % len(key_text)]) for index in range(256)]
    sbox = list(range(256))
    position = 0
    for index in range(256):
        position = (position + sbox[index] + key[index]) % 256
        sbox[index], sbox[position] = sbox[position], sbox[index]

    left = right = 0
    encrypted: list[str] = []
    for character in source:
        left = (left + 1) % 256
        right = (right + sbox[left]) % 256
        sbox[left], sbox[right] = sbox[right], sbox[left]
        stream_index = (sbox[left] + sbox[right]) % 256
        encrypted.append(f"{ord(character) ^ sbox[stream_index]:02x}")
    return "".join(encrypted)
~~~

- [ ] **Step 4: Verify the test vector against the legacy function, then remove sensitive legacy files**

Run the old and new functions locally with the non-secret fixed vector before deleting the legacy files. Replace the unsafe command document with README.md that explains UI usage without sample credentials.

Run: python -m pytest tests/test_network_crypto.py -v

Expected: 2 passed.

- [ ] **Step 5: Commit the scaffold**

~~~powershell
git add pyproject.toml src/net_connector tests/test_network_crypto.py README.md
git add -u -- lab_net_login_no_requests.py 上网脚本使用命令.txt
git commit -m "feat: scaffold portable network connector"
~~~

### Task 2: Portal Network Service

**Files:**
- Modify: src/net_connector/network.py
- Create: tests/test_network.py

- [ ] **Step 1: Write failing tests for online, success, timeout, and verification failure**

Use a scripted opener that records Request objects and returns deterministic response context managers:

~~~python
from urllib.error import URLError
from urllib.parse import parse_qs

from net_connector.network import (
    ConnectionCode,
    Credentials,
    NetworkService,
)


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class ScriptedOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeResponse(*outcome)


def make_service(*outcomes):
    opener = ScriptedOpener(*outcomes)
    service = NetworkService(
        opener=opener,
        clock_ms=lambda: 1700000000000,
        sleep=lambda _seconds: None,
        vpn_detector=lambda: (),
    )
    return service, opener


def test_connect_skips_login_when_already_online():
    service, opener = make_service((200, "Microsoft Connect Test"))
    result = service.connect(Credentials("u", "p"))
    assert result.code is ConnectionCode.ALREADY_ONLINE
    assert len(opener.requests) == 1


def test_connect_posts_required_fields_then_verifies_online():
    service, opener = make_service(
        (200, "captive portal"),
        (200, "<html>login</html>"),
        (200, '{"success": true}'),
        (200, "Microsoft Connect Test"),
    )
    result = service.connect(Credentials("u", "p"))
    assert result.code is ConnectionCode.CONNECTED
    post = [request for request in opener.requests if request.get_method() == "POST"][0]
    posted_form = parse_qs(post.data.decode("utf-8"))
    assert posted_form["opr"] == ["pwdLogin"]
    assert posted_form["userName"] == ["u"]
    assert posted_form["auth_tag"] == ["1700000000000"]
    assert posted_form["pwd"] != ["p"]


def test_connect_classifies_timeout_without_leaking_password():
    service, _opener = make_service(URLError("timed out"))
    result = service.connect(Credentials("u", "secret-value"))
    assert result.code is ConnectionCode.PORTAL_UNREACHABLE
    assert "secret-value" not in result.detail
~~~

- [ ] **Step 2: Run the focused tests**

Run: python -m pytest tests/test_network.py -v

Expected: FAIL because Credentials, ConnectionCode, ConnectionResult and NetworkService are not defined.

- [ ] **Step 3: Implement the public network contract**

~~~python
@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


class ConnectionCode(Enum):
    ALREADY_ONLINE = auto()
    CONNECTED = auto()
    MISSING_CREDENTIALS = auto()
    PORTAL_UNREACHABLE = auto()
    PORTAL_REJECTED = auto()
    VERIFICATION_FAILED = auto()
    INTERNAL_ERROR = auto()


@dataclass(frozen=True)
class ConnectionResult:
    code: ConnectionCode
    detail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.code in {ConnectionCode.ALREADY_ONLINE, ConnectionCode.CONNECTED}
~~~

NetworkService must receive an opener, millisecond clock and sleep function for deterministic tests. Build the default opener with urllib.request.ProxyHandler({}) so portal traffic bypasses configured HTTP proxies. Preserve the existing portal URL, form field names, Referer and Origin. Decode response text with UTF-8 replacement, cap internal detail to 500 characters, and never include request form values in a result.

- [ ] **Step 4: Implement the exact connection sequence**

is_online checks for HTTP 200 and the text Microsoft Connect Test. connect validates stripped credentials, checks online, warms the portal page, creates auth_tag from the injected clock, posts the encoded form, waits three seconds, and probes again. Map HTTP 401/403 and explicit failure markers in the portal body to PORTAL_REJECTED; map URL and timeout failures to PORTAL_UNREACHABLE; map a completed post followed by failed probing to VERIFICATION_FAILED.

- [ ] **Step 5: Run tests and commit**

Run: python -m pytest tests/test_network_crypto.py tests/test_network.py -v

Expected: all focused tests pass.

~~~powershell
git add src/net_connector/network.py tests/test_network.py
git commit -m "feat: add classified portal login service"
~~~

### Task 3: VPN Detection and Result Decoration

**Files:**
- Create: src/net_connector/vpn.py
- Create: tests/test_vpn.py
- Modify: src/net_connector/network.py
- Modify: tests/test_network.py

- [ ] **Step 1: Write failing interface-classification tests**

~~~python
import pytest
from types import SimpleNamespace
from net_connector.vpn import find_active_vpn_interfaces, looks_like_vpn


@pytest.mark.parametrize(
    "name",
    ["WireGuard Tunnel", "tun0", "TAP-Windows Adapter V9", "ppp0", "Cisco AnyConnect", "Tailscale"],
)
def test_known_vpn_names(name):
    assert looks_like_vpn(name)


@pytest.mark.parametrize("name", ["Ethernet", "Wi-Fi", "lo", "isatap", "Teredo"])
def test_normal_or_system_interfaces_are_not_vpn(name):
    assert not looks_like_vpn(name)


def test_only_active_matching_interfaces_are_returned():
    stats = {
        "tun0": SimpleNamespace(isup=True),
        "Ethernet": SimpleNamespace(isup=True),
        "wg0": SimpleNamespace(isup=False),
    }
    assert find_active_vpn_interfaces(lambda: stats) == ("tun0",)
~~~

- [ ] **Step 2: Run tests and implement the heuristics**

Run: python -m pytest tests/test_vpn.py -v

Expected: FAIL because net_connector.vpn does not exist.

Implement case-insensitive token and vendor matching for tun, tap, wireguard, wg plus digits, ppp, vpn, openvpn, anyconnect, globalprotect, fortinet, pulse, protonvpn, tailscale and zerotier. Explicitly exclude isatap, teredo and 6to4. Default enumeration uses psutil.net_if_stats.

- [ ] **Step 3: Attach VPN evidence only to failed connections**

Add vpn_interfaces: tuple[str, ...] to ConnectionResult. NetworkService must call the injected VPN detector only after a failed login or failed final probe. Successful and already-online results retain an empty tuple.

- [ ] **Step 4: Verify and commit**

Run: python -m pytest tests/test_vpn.py tests/test_network.py -v

Expected: all tests pass and the failure test contains the detected interface name.

~~~powershell
git add src/net_connector/vpn.py src/net_connector/network.py tests/test_vpn.py tests/test_network.py
git commit -m "feat: diagnose active VPN interfaces"
~~~

### Task 4: Safe Settings and Credential Storage

**Files:**
- Create: src/net_connector/storage.py
- Create: tests/test_storage.py

- [ ] **Step 1: Write failing settings tests**

~~~python
from net_connector.storage import Settings, SettingsError, SettingsStore


def test_missing_settings_use_defaults(tmp_path):
    result = SettingsStore(tmp_path / "settings.json").load()
    assert result.settings == Settings(language="system", schedule_enabled=False, schedule_time="08:30")
    assert not result.recovered


def test_corrupt_settings_are_recovered(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{broken", encoding="utf-8")
    result = SettingsStore(path).load()
    assert result.recovered
    assert result.settings.schedule_time == "08:30"


def test_invalid_time_is_rejected(tmp_path):
    import pytest

    with pytest.raises(SettingsError, match="HH:MM"):
        Settings(schedule_time="25:00").validate()
~~~

- [ ] **Step 2: Write failing keyring tests**

~~~python
import pytest
from keyring.errors import PasswordSetError

from net_connector.network import Credentials
from net_connector.storage import CredentialError, CredentialStore


class FakeKeyring:
    def __init__(self):
        self.value = None
        self.raise_on_write = False
        self.last_service = None
        self.last_username = None

    def set_password(self, service, username, value):
        if self.raise_on_write:
            raise PasswordSetError("backend unavailable")
        self.last_service = service
        self.last_username = username
        self.value = value

    def get_password(self, service, username):
        return self.value


def test_credentials_round_trip_without_plaintext_settings():
    fake_keyring = FakeKeyring()
    store = CredentialStore(fake_keyring)
    store.save(Credentials("employee", "passphrase"))
    assert store.load() == Credentials("employee", "passphrase")
    assert fake_keyring.last_service == "portable-network-connector"
    assert fake_keyring.last_username == "network-login"


def test_keyring_failure_is_classified():
    fake_keyring = FakeKeyring()
    fake_keyring.raise_on_write = True
    with pytest.raises(CredentialError):
        CredentialStore(fake_keyring).save(Credentials("employee", "passphrase"))
~~~

- [ ] **Step 3: Implement Settings, SettingsStore and platform config paths**

Settings validates language against system, zh and en, and validates time with datetime.strptime("%H:%M"). SettingsStore writes JSON to a same-directory temporary file, flushes it, and replaces the destination atomically. Windows uses APPDATA/PortableNetworkConnector/settings.json; Linux uses XDG_CONFIG_HOME when set and otherwise ~/.config/portable-network-connector/settings.json.

- [ ] **Step 4: Implement keyring storage**

CredentialStore stores one compact JSON object containing username and password under service portable-network-connector and key network-login. Empty fields raise CredentialError before calling keyring. Wrap keyring.errors.KeyringError and a backend with priority zero in CredentialError; do not provide a plaintext fallback.

- [ ] **Step 5: Verify and commit**

Run: python -m pytest tests/test_storage.py -v

Expected: all storage tests pass.

~~~powershell
git add src/net_connector/storage.py tests/test_storage.py
git commit -m "feat: store settings and credentials safely"
~~~

### Task 5: Daily Scheduler

**Files:**
- Create: src/net_connector/scheduler.py
- Create: tests/test_scheduler.py

- [ ] **Step 1: Write failing scheduling tests**

~~~python
from datetime import datetime

from net_connector.scheduler import DailyScheduler


def test_new_scheduler_selects_next_future_time():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 9, 0))
    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)


def test_sleep_resume_fires_once_and_advances():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 8, 0))
    assert scheduler.poll(datetime(2026, 7, 23, 10, 0))
    assert not scheduler.poll(datetime(2026, 7, 23, 10, 1))
    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)


def test_restart_after_time_does_not_backfill():
    scheduler = DailyScheduler("08:30", datetime(2026, 7, 23, 10, 0))
    assert scheduler.next_run == datetime(2026, 7, 24, 8, 30)
~~~

- [ ] **Step 2: Run tests and implement the scheduler**

Run: python -m pytest tests/test_scheduler.py -v

Expected: FAIL because net_connector.scheduler does not exist.

DailyScheduler parses HH:MM, chooses today only when the target is strictly later than the initial instant, returns True from poll when now reaches next_run, and advances by calendar days until next_run is in the future. reschedule replaces the configured time and recomputes from the supplied current instant.

- [ ] **Step 3: Cover midnight and rescheduling, then commit**

Add tests for 23:59 to next day, leap-day calendar arithmetic, disabling in the application layer, and changing an enabled schedule to an earlier time.

Run: python -m pytest tests/test_scheduler.py -v

Expected: all scheduler tests pass.

~~~powershell
git add src/net_connector/scheduler.py tests/test_scheduler.py
git commit -m "feat: add once-daily connection scheduler"
~~~

### Task 6: Complete Bilingual Catalog

**Files:**
- Create: src/net_connector/i18n.py
- Create: tests/test_i18n.py

- [ ] **Step 1: Write failing catalog and locale tests**

~~~python
from net_connector.i18n import CATALOGS, Translator, resolve_language


def test_catalogs_have_identical_keys():
    assert set(CATALOGS["zh"]) == set(CATALOGS["en"])


def test_system_chinese_locale_selects_chinese():
    assert resolve_language("system", "zh_CN") == "zh"


def test_non_chinese_system_locale_selects_english():
    assert resolve_language("system", "de_DE") == "en"


def test_unknown_key_is_visible_during_development():
    assert Translator("en").text("missing.key") == "[missing.key]"
~~~

- [ ] **Step 2: Implement stable message keys and translator**

Run: python -m pytest tests/test_i18n.py -v

Expected: FAIL because net_connector.i18n does not exist.

Define every label and message required by the approved UI: title, settings tooltip, connect, status states, schedule labels, next-run formatting, tray commands, credential fields, language choices, save/cancel/show, all ConnectionCode errors, VPN advice, keyring failure, corrupt settings warning, tray fallback, and confirmation dialogs. Translator.text accepts formatting keyword arguments and never exposes raw exceptions.

- [ ] **Step 3: Verify and commit**

Run: python -m pytest tests/test_i18n.py -v

Expected: all i18n tests pass.

~~~powershell
git add src/net_connector/i18n.py tests/test_i18n.py
git commit -m "feat: add complete Chinese and English catalogs"
~~~

### Task 7: Tkinter UI, Background Worker, and Tray

**Files:**
- Create: src/net_connector/app.py
- Create: src/net_connector/__main__.py
- Create: tests/test_app_smoke.py

- [ ] **Step 1: Write an import and construction smoke test**

~~~python
from net_connector.app import main, result_message_key
from net_connector.network import ConnectionCode, ConnectionResult


def test_application_module_imports_without_starting_ui():
    assert callable(main)


def test_connection_message_maps_vpn_failure_to_vpn_key():
    result = ConnectionResult(ConnectionCode.VERIFICATION_FAILED, vpn_interfaces=("tun0",))
    assert result_message_key(result) == "error.vpn_detected"
~~~

- [ ] **Step 2: Run the smoke test**

Run: python -m pytest tests/test_app_smoke.py -v

Expected: FAIL because net_connector.app does not exist.

- [ ] **Step 3: Build the approved main window**

Application creates a 420 x 390 resizable-to-content Tk window with ttk styles in neutral gray, white and restrained green. The header has the localized title and a 34 x 34 gear-character button with a Tooltip. The status section has a stable color dot, state text and last-check text. The centered connect button is 160 pixels wide. The schedule section has a Checkbutton, validated HH:MM Entry, daily label and next-run label. Do not place network calls in any widget callback.

- [ ] **Step 4: Build the settings dialog**

Use ttk.Entry for username, masked password Entry with show/hide control, and a readonly ttk.Combobox containing translated labels for system, zh and en. Selecting a language calls apply_language immediately. Cancel restores the prior language. Save validates both credential fields, writes CredentialStore first, then SettingsStore, keeps the dialog open on failure, and redacts exception details.

- [ ] **Step 5: Add worker and daily polling**

start_connection disables the button, starts one daemon threading.Thread, and posts ConnectionResult into queue.Queue. root.after polls the queue and updates widgets on the Tk thread. A one-second root.after callback polls DailyScheduler only while enabled and starts the same connection method. A busy worker causes a due schedule to be skipped without starting a second request.

- [ ] **Step 6: Add generated tray icon and safe fallback**

Create the tray bitmap at runtime with Pillow so no asset file is required. Import pystray lazily and run it in its supported thread. Menu callbacks return to Tk through root.after. Window close withdraws only after tray startup succeeds; otherwise it iconifies and shows the localized fallback once. Exit stops the tray and destroys Tk.

- [ ] **Step 7: Verify UI behavior and commit**

Run: python -m pytest tests/test_app_smoke.py tests/test_i18n.py tests/test_scheduler.py -v

Expected: all focused tests pass. Then run python -m net_connector and manually verify the window, gear tooltip, shortened button, language dropdown, close behavior, and that no login starts until clicked or scheduled.

~~~powershell
git add src/net_connector/app.py src/net_connector/__main__.py tests/test_app_smoke.py
git commit -m "feat: add bilingual desktop UI and tray"
~~~

### Task 8: Packaging, CI, and Release Workflow

**Files:**
- Create: scripts/build.ps1
- Create: scripts/build.sh
- Create: .github/workflows/ci.yml
- Create: .github/workflows/release.yml
- Modify: README.md

- [ ] **Step 1: Add reproducible local build scripts**

Both scripts create an isolated .venv-build, install the current project with dev dependencies, run pytest, and call:

~~~text
pyinstaller --noconfirm --clean --onefile --windowed --name WorkNetConnector --collect-all keyring --collect-all pystray --hidden-import PIL._tkinter_finder src/net_connector/__main__.py
~~~

PowerShell exits on every failed native command. Bash uses set -euo pipefail. Neither script deletes anything outside .venv-build, build, dist, or the generated spec file inside the repository.

- [ ] **Step 2: Build locally on Windows**

Run: powershell -ExecutionPolicy Bypass -File scripts/build.ps1

Expected: tests pass and dist/WorkNetConnector.exe exists. Launch it, open settings, verify the language dropdown and close it without entering credentials.

- [ ] **Step 3: Add CI**

ci.yml runs on pushes and pull requests using windows-latest and ubuntu-22.04 with Python 3.12. Ubuntu installs python3-tk and xvfb. The matrix installs the dev dependencies, runs pytest, and runs the platform build script under xvfb where needed. Upload the executable as a workflow artifact.

- [ ] **Step 4: Add tagged release workflow**

release.yml triggers on tags matching v*. Build on Windows and Ubuntu, rename files to WorkNetConnector-windows-x86_64.exe and WorkNetConnector-linux-x86_64, create SHA256SUMS.txt, and attach all three files to one GitHub Release using the repository GITHUB_TOKEN.

- [ ] **Step 5: Document safe usage and commit**

README.md must include supported systems, download/run instructions, initial credential setup, manual connect, daily schedule semantics, tray behavior, VPN wording, Linux keyring prerequisite, build commands, and a statement that GitHub account passwords and network credentials must never be placed in commands or tracked files.

~~~powershell
git add scripts .github README.md
git commit -m "build: add portable cross-platform releases"
~~~

### Task 9: Full Verification, Security Gate, and Remote Publication

**Files:**
- Modify only files needed to fix failures discovered by this task.

- [ ] **Step 1: Run the complete automated suite**

Run: python -m pytest -v --cov=net_connector --cov-report=term-missing

Expected: all tests pass with no unhandled thread warnings. Core network, storage, scheduler, VPN and i18n modules must each have at least 90 percent statement coverage.

- [ ] **Step 2: Run repository hygiene checks**

Run: git diff --check

Run a case-insensitive rg scan for password assignments, the exposed account identifier, the exposed password value, Authorization headers, tokens and private keys across tracked files. Expected: no real secret matches. Test fixtures may use obvious dummy strings only.

Run: git status --short

Expected: only intentional uncommitted plan checkbox updates, or a clean worktree after committing them.

- [ ] **Step 3: Exercise failure states without real credentials**

Use injected test doubles or a local mock server to verify missing credentials, keyring unavailable, portal timeout, portal rejection, verification failure, VPN detected and duplicate-click suppression. Confirm screenshots at 420 x 390 in Chinese and English have no clipped text.

- [ ] **Step 4: Verify the Windows artifact**

Run the built executable from a temporary directory that contains no source files. Confirm it starts without Python on PATH, creates only the documented user configuration, stores credentials only through CredentialStore, remains active after window close when tray succeeds, and exits from the tray.

- [ ] **Step 5: Create and push the private GitHub repository**

Use GitHub browser/device authorization or a Personal Access Token, never the account password. Create private repository LiamxZhang/portable-network-connector, add it as origin, and push main. If the repository name already exists, pause for the user to choose the destination rather than overwriting it.

- [ ] **Step 6: Confirm CI and publish v0.1.0**

Wait for the main-branch CI matrix to pass. Create signed or annotated tag v0.1.0, push it, wait for the release workflow, and verify both executables plus SHA256SUMS.txt are attached. Download artifacts and confirm checksums.

- [ ] **Step 7: Commit any final non-secret corrections**

~~~powershell
git add -u
git add -A -- src tests scripts .github README.md pyproject.toml
git commit -m "chore: prepare v0.1.0 release"
~~~

Do not create an empty commit. Finish with git status --short --branch and git log --oneline --decorate -10.
