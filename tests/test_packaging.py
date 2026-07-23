"""Contracts for portable build scripts and GitHub release automation."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

import pytest
from ruamel.yaml import YAML


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL_BUILD = REPOSITORY_ROOT / "scripts" / "build.ps1"
BASH_BUILD = REPOSITORY_ROOT / "scripts" / "build.sh"
CI_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"
PYINSTALLER_ARGUMENTS = (
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--name",
    "WorkNetConnector",
    "--collect-all",
    "keyring",
    "--collect-all",
    "pystray",
    "--hidden-import",
    "PIL._tkinter_finder",
    "src/net_connector/__main__.py",
)


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_workflow(path: Path):
    yaml = YAML(typ="rt")
    yaml.version = (1, 2)
    workflow = yaml.load(read_file(path))
    assert "on" in workflow
    return workflow


def workflow_step(job, name: str):
    matches = [step for step in job["steps"] if step.get("name") == name]
    assert len(matches) == 1
    return matches[0]


def workflow_runs(job) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


ALLOWED_RELEASE_ASSETS = {
    "WorkNetConnector-windows-x86_64.exe",
    "WorkNetConnector-linux-x86_64",
    "SHA256SUMS.txt",
}

FAKE_GH_PROGRAM = r'''import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["FAKE_GH_STATE"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
state["calls"].append(args)


def save():
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")


def event(kind, **values):
    state["events"].append({"kind": kind, **values})


if args[:2] == ["release", "view"]:
    save()
    raise SystemExit(0 if state["release"] is not None else 1)

if args[:2] == ["release", "create"]:
    if "--draft" not in args:
        raise SystemExit("release was not created as a draft")
    state["release"] = "draft"
    event("create")
    save()
    raise SystemExit(0)

if args[:2] == ["release", "edit"]:
    draft_flag = next((value for value in args if value.startswith("--draft=")), None)
    if draft_flag == "--draft=true":
        state["release"] = "draft"
        event("draft")
    elif draft_flag == "--draft=false":
        state["release"] = "published"
        event("publish")
    else:
        raise SystemExit("missing draft transition")
    save()
    raise SystemExit(0)

if args and args[0] == "api":
    if "--method" in args:
        method_index = args.index("--method")
        if args[method_index + 1] != "DELETE":
            raise SystemExit("unexpected API method")
        endpoint = args[-1]
        asset_id = endpoint.rsplit("/", 1)[-1]
        if not asset_id.isdigit():
            raise SystemExit("non-numeric delete endpoint")
        numeric_id = int(asset_id)
        state["assets"] = [asset for asset in state["assets"] if asset["id"] != numeric_id]
        event("delete", id=numeric_id)
        save()
        raise SystemExit(0)
    rows = "".join(f'{asset["id"]}\t{asset["name"]}\n' for asset in state["assets"])
    sys.stdout.buffer.write(rows.encode("utf-8"))
    save()
    raise SystemExit(0)

if args[:2] == ["release", "upload"]:
    event("upload")
    if state.get("upload_fail"):
        save()
        raise SystemExit(42)
    uploaded = [value for value in args[3:] if value != "--clobber"]
    allowed = {
        "WorkNetConnector-windows-x86_64.exe",
        "WorkNetConnector-linux-x86_64",
        "SHA256SUMS.txt",
    }
    if set(uploaded) != allowed or "--clobber" not in args:
        raise SystemExit("unexpected upload contract")
    state["assets"] = [asset for asset in state["assets"] if asset["name"] not in allowed]
    next_id = max([asset["id"] for asset in state["assets"] if isinstance(asset["id"], int)] + [1000]) + 1
    for offset, name in enumerate(sorted(allowed)):
        state["assets"].append({"id": next_id + offset, "name": name})
    save()
    raise SystemExit(0)

save()
raise SystemExit(f"unexpected fake gh command: {args!r}")
'''


def find_test_bash() -> str:
    candidates = [shutil.which("bash")]
    if os.name == "nt":
        candidates.extend(
            [
                r"C:\Program Files\Git\bin\bash.exe",
                r"C:\Program Files\Git\usr\bin\bash.exe",
            ]
        )
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        probe = subprocess.run(
            [candidate, "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    pytest.skip("A usable POSIX Bash is unavailable")


def make_fake_release_runner(tmp_path: Path, initial_state: dict):
    workflow = load_workflow(RELEASE_WORKFLOW)
    publish = workflow["jobs"]["publish"]
    release_script = workflow_step(publish, "Synchronize and publish release")["run"]
    fake_program = tmp_path / "fake_gh.py"
    fake_program.write_text(FAKE_GH_PROGRAM, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        '#!/usr/bin/env bash\nexec python "$FAKE_GH_SCRIPT" "$@"\n',
        encoding="utf-8",
    )
    fake_gh.chmod(0o755)
    state_path = tmp_path / "state.json"
    state = {
        "release": None,
        "assets": [],
        "events": [],
        "calls": [],
        "upload_fail": False,
        **initial_state,
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    release_assets = tmp_path / "release-assets"
    release_assets.mkdir()
    for filename in ALLOWED_RELEASE_ASSETS:
        (release_assets / filename).write_bytes(b"portable-artifact")
    wrapper = tmp_path / "run-release.sh"
    wrapper.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if command -v cygpath >/dev/null 2>&1; then
  export PATH="$(cygpath -u "$FAKE_GH_BIN"):$PATH"
else
  export PATH="$FAKE_GH_BIN:$PATH"
fi
"""
        + release_script,
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_GH_BIN": str(fake_bin),
            "FAKE_GH_SCRIPT": str(fake_program),
            "FAKE_GH_STATE": str(state_path),
            "GITHUB_REF_NAME": "v1.2.3",
            "GH_REPO": "example/work-net-connector",
            "GH_TOKEN": "fake-token-for-offline-test",
        }
    )
    bash = find_test_bash()

    def run():
        result = subprocess.run(
            [bash, wrapper.as_posix()],
            cwd=release_assets,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        return result, json.loads(state_path.read_text(encoding="utf-8"))

    return run


def target_metadata() -> dict[str, tuple[bool, int | None]]:
    targets = (
        REPOSITORY_ROOT / ".venv-build",
        REPOSITORY_ROOT / "build",
        REPOSITORY_ROOT / "dist",
        REPOSITORY_ROOT / "WorkNetConnector.spec",
    )
    return {
        str(path): (path.exists(), path.stat().st_mtime_ns if path.exists() else None)
        for path in targets
    }


def assert_validation_output(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    expected_root = f"Repository root: {REPOSITORY_ROOT.resolve()}"
    expected_arguments = f"PyInstaller arguments: {' '.join(PYINSTALLER_ARGUMENTS)}"
    assert expected_root.casefold() in result.stdout.casefold()
    assert expected_arguments in result.stdout


def test_powershell_build_script_is_fail_fast_and_cleanup_is_allowlisted() -> None:
    script = read_file(POWERSHELL_BUILD)

    assert "$ErrorActionPreference = \"Stop\"" in script
    assert "Set-StrictMode -Version Latest" in script
    assert "$PSScriptRoot" in script
    assert "$LASTEXITCODE" in script
    assert 'Join-Path $repoRoot ".venv-build"' in script
    assert 'Join-Path $repoRoot "build"' in script
    assert 'Join-Path $repoRoot "dist"' in script
    assert 'Join-Path $repoRoot "WorkNetConnector.spec"' in script
    assert "Assert-AllowedRemoval" in script
    assert "[System.IO.FileAttributes]::ReparsePoint" in script
    remove_lines = [line.strip() for line in script.splitlines() if "Remove-Item" in line]
    assert remove_lines
    assert all("-LiteralPath" in line and "*" not in line for line in remove_lines)


def test_bash_build_script_is_fail_fast_and_cleanup_is_allowlisted() -> None:
    script = read_file(BASH_BUILD)

    assert "set -euo pipefail" in script
    assert '${BASH_SOURCE[0]}' in script
    assert 'BUILD_VENV="$REPO_ROOT/.venv-build"' in script
    assert 'BUILD_DIR="$REPO_ROOT/build"' in script
    assert 'DIST_DIR="$REPO_ROOT/dist"' in script
    assert 'SPEC_PATH="$REPO_ROOT/WorkNetConnector.spec"' in script
    assert 'BASE_PREFIX="$("$VENV_PYTHON" -c ' in script
    assert "assert_allowed_removal" in script
    remove_lines = [line.strip() for line in script.splitlines() if re.match(r"^rm\s", line.strip())]
    assert remove_lines == ['rm -rf -- "$path"']


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell behavior is tested on Windows")
def test_powershell_validate_only_works_from_an_unrelated_directory(tmp_path: Path) -> None:
    before = target_metadata()
    powershell = shutil.which("powershell")
    assert powershell is not None

    result = subprocess.run(
        [
            powershell,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(POWERSHELL_BUILD),
            "-ValidateOnly",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert_validation_output(result)
    assert target_metadata() == before


@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell behavior is tested on Windows")
def test_powershell_python_probe_handles_a_rejected_native_candidate() -> None:
    powershell = shutil.which("powershell")
    assert powershell is not None
    quoted_script = str(POWERSHELL_BUILD).replace("'", "''")
    command = (
        f"$null = . '{quoted_script}' -ValidateOnly; "
        "$candidate = Join-Path $env:SystemRoot 'System32\\where.exe'; "
        "if (Test-Python312 -Executable $candidate) { exit 9 }; "
        "Write-Output 'Rejected candidate handled'"
    )

    result = subprocess.run(
        [powershell, "-NoLogo", "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert result.returncode == 0, (result.stdout or "") + (result.stderr or "")
    assert "Rejected candidate handled" in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell behavior is tested on Linux")
def test_bash_validate_only_works_from_an_unrelated_directory(tmp_path: Path) -> None:
    before = target_metadata()
    bash = shutil.which("bash")
    assert bash is not None

    result = subprocess.run(
        [bash, str(BASH_BUILD), "--validate-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
        check=False,
    )

    assert_validation_output(result)
    assert target_metadata() == before


@pytest.mark.parametrize("script_path", [POWERSHELL_BUILD, BASH_BUILD])
def test_build_scripts_install_test_and_use_the_exact_pyinstaller_contract(script_path: Path) -> None:
    script = read_file(script_path)

    assert ".[dev]" in script
    assert "-m" in script and "pytest" in script
    assert ".venv-build" in script
    if script_path.suffix == ".ps1":
        match = re.search(r"\$pyinstallerArguments = @\((.*?)\n\)", script, re.DOTALL)
        assert match is not None
        configured_arguments = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    else:
        match = re.search(r"PYINSTALLER_ARGUMENTS=\((.*?)\n\)", script, re.DOTALL)
        assert match is not None
        configured_arguments = tuple(
            line.strip() for line in match.group(1).splitlines() if line.strip()
        )
    assert configured_arguments == PYINSTALLER_ARGUMENTS


def test_build_virtual_environment_is_ignored() -> None:
    assert ".venv-build/" in read_file(REPOSITORY_ROOT / ".gitignore").splitlines()


def test_dev_dependencies_include_yaml_1_2_parser() -> None:
    project = tomllib.loads(read_file(REPOSITORY_ROOT / "pyproject.toml"))

    assert any(
        dependency.startswith("ruamel.yaml")
        for dependency in project["project"]["optional-dependencies"]["dev"]
    )


def test_ci_workflow_builds_and_uploads_each_supported_platform() -> None:
    workflow = load_workflow(CI_WORKFLOW)

    assert workflow["on"] == {
        "push": {"branches": ["main"]},
        "pull_request": None,
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"build"}
    build = workflow["jobs"]["build"]
    assert build["strategy"]["fail-fast"] is False
    assert build["strategy"]["matrix"]["include"] == [
        {
            "platform": "windows-x86_64",
            "os": "windows-latest",
            "artifact": "WorkNetConnector-windows-x86_64-ci",
            "executable": "dist/WorkNetConnector.exe",
        },
        {
            "platform": "linux-x86_64",
            "os": "ubuntu-22.04",
            "artifact": "WorkNetConnector-linux-x86_64-ci",
            "executable": "dist/WorkNetConnector",
        },
    ]
    assert workflow_step(build, "Set up Python 3.12")["with"]["python-version"] == "3.12"
    linux_dependencies = workflow_step(build, "Install Linux desktop build dependencies")["run"]
    assert "python3-tk" in linux_dependencies and "xvfb" in linux_dependencies
    assert workflow_step(build, "Install, test, and build on Windows")["run"].endswith(
        "scripts/build.ps1"
    )
    assert workflow_step(build, "Install, test, and build on Linux")["run"] == (
        "xvfb-run -a bash scripts/build.sh"
    )
    upload = workflow_step(build, "Upload portable executable")["with"]
    assert upload["name"] == "${{ matrix.artifact }}"
    assert upload["path"] == "${{ matrix.executable }}"
    # Platform scripts own dependency installation and the single complete test run.
    assert all("python -m pytest" not in command for command in workflow_runs(build))


def test_official_actions_use_expected_major_refs_until_shas_can_be_resolved() -> None:
    expected_majors = {
        "actions/checkout": "v4",
        "actions/setup-python": "v5",
        "actions/upload-artifact": "v4",
        "actions/download-artifact": "v4",
    }
    observed = set()

    for path in (CI_WORKFLOW, RELEASE_WORKFLOW):
        workflow = load_workflow(path)
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses")
                if not uses or not uses.startswith("actions/"):
                    continue
                repository, reference = uses.split("@", 1)
                assert repository in expected_majors
                assert reference == expected_majors[repository]
                observed.add(repository)

    assert observed == set(expected_majors)


@pytest.mark.xfail(
    strict=True,
    reason=(
        "official action tag SHAs unresolved: two bounded git ls-remote attempts "
        "could not connect to github.com:443"
    ),
)
def test_official_actions_are_pinned_to_commented_commit_shas() -> None:
    expected_majors = {
        "actions/checkout": "v4",
        "actions/setup-python": "v5",
        "actions/upload-artifact": "v4",
        "actions/download-artifact": "v4",
    }
    observed = set()

    for path in (CI_WORKFLOW, RELEASE_WORKFLOW):
        workflow = load_workflow(path)
        for job in workflow["jobs"].values():
            for step in job["steps"]:
                uses = step.get("uses")
                if not uses or not uses.startswith("actions/"):
                    continue
                repository, reference = uses.split("@", 1)
                assert repository in expected_majors
                assert re.fullmatch(r"[0-9a-f]{40}", reference)
                comment_items = step.ca.items.get("uses")
                comment = comment_items[2].value.strip() if comment_items and comment_items[2] else ""
                major = expected_majors[repository]
                assert re.fullmatch(
                    rf"#\s*{re.escape(repository)}\s+{major}(?:\.\d+\.\d+)?",
                    comment,
                )
                observed.add(repository)

    assert observed == set(expected_majors)


def test_release_workflow_publishes_one_release_with_exact_checksums() -> None:
    workflow = load_workflow(RELEASE_WORKFLOW)

    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"] == {
        "group": "release-${{ github.ref }}",
        "cancel-in-progress": False,
    }
    assert set(workflow["jobs"]) == {"build", "publish"}
    build = workflow["jobs"]["build"]
    publish = workflow["jobs"]["publish"]
    assert build["strategy"]["matrix"]["include"] == [
        {
            "platform": "windows-x86_64",
            "os": "windows-latest",
            "artifact": "release-windows-x86_64",
            "release_path": "release-assets/WorkNetConnector-windows-x86_64.exe",
        },
        {
            "platform": "linux-x86_64",
            "os": "ubuntu-22.04",
            "artifact": "release-linux-x86_64",
            "release_path": "release-assets/WorkNetConnector-linux-x86_64",
        },
    ]
    assert all("gh release" not in command for command in workflow_runs(build))
    assert publish["needs"] == "build"
    assert "strategy" not in publish
    assert publish["permissions"] == {"contents": "write"}
    assert publish["env"] == {
        "GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}",
        "GH_REPO": "${{ github.repository }}",
    }

    download = workflow_step(publish, "Download release binaries")["with"]
    assert download == {
        "pattern": "release-*",
        "path": "release-assets",
        "merge-multiple": True,
    }
    checksum = workflow_step(publish, "Create checksum manifest")["run"]
    assert checksum == (
        "sha256sum WorkNetConnector-windows-x86_64.exe "
        "WorkNetConnector-linux-x86_64 > SHA256SUMS.txt"
    )

    synchronize = workflow_step(publish, "Synchronize and publish release")["run"]
    assert 'if ! gh release view "$GITHUB_REF_NAME"' in synchronize
    assert 'gh release create "$GITHUB_REF_NAME"' in synchronize
    assert "--draft" in synchronize
    draft_transition = 'gh release edit "$GITHUB_REF_NAME" --draft=true'
    assert draft_transition in synchronize
    assert synchronize.index(draft_transition) < synchronize.index("gh api")
    assert "[0-9]+" in synchronize
    assert 'gh api --method DELETE "repos/$GH_REPO/releases/assets/$asset_id"' in synchronize
    assert "gh release delete-asset" not in synchronize
    for filename in (
        "WorkNetConnector-windows-x86_64.exe",
        "WorkNetConnector-linux-x86_64",
        "SHA256SUMS.txt",
    ):
        assert filename in synchronize
    assert "gh api --method DELETE" in synchronize
    assert "gh release upload" in synchronize
    assert "--clobber" in synchronize
    publish_transition = 'gh release edit "$GITHUB_REF_NAME" --draft=false'
    assert publish_transition in synchronize
    assert synchronize.index("gh release upload") < synchronize.index(publish_transition)

    publish_commands = "\n".join(workflow_runs(publish))
    assert publish_commands.count("gh release create") == 1
    assert publish_commands.count("gh release upload") == 1
    assert publish_commands.count("gh release edit") == 2


def test_published_release_is_drafted_before_adversarial_assets_are_deleted(tmp_path: Path) -> None:
    adversarial_assets = [
        {"id": 11, "name": "--help"},
        {"id": 12, "name": "name with spaces"},
        {"id": 13, "name": "$(touch injected)"},
        {"id": 14, "name": "quote'\"; echo injected"},
    ]
    run = make_fake_release_runner(
        tmp_path,
        {
            "release": "published",
            "assets": adversarial_assets
            + [{"id": 15 + index, "name": name} for index, name in enumerate(ALLOWED_RELEASE_ASSETS)],
        },
    )

    result, state = run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert state["events"][0] == {"kind": "draft"}
    assert [event["id"] for event in state["events"] if event["kind"] == "delete"] == [
        11,
        12,
        13,
        14,
    ]
    assert state["events"][-1] == {"kind": "publish"}
    assert state["release"] == "published"
    assert {asset["name"] for asset in state["assets"]} == ALLOWED_RELEASE_ASSETS
    command_arguments = "\0".join(argument for call in state["calls"] for argument in call)
    for asset in adversarial_assets:
        assert asset["name"] not in command_arguments
    delete_calls = [call for call in state["calls"] if call[:3] == ["api", "--method", "DELETE"]]
    assert all(re.fullmatch(r"repos/example/work-net-connector/releases/assets/\d+", call[-1]) for call in delete_calls)


def test_upload_failure_leaves_existing_release_as_draft(tmp_path: Path) -> None:
    run = make_fake_release_runner(
        tmp_path,
        {"release": "published", "upload_fail": True},
    )

    result, state = run()

    assert result.returncode != 0
    assert state["events"][0] == {"kind": "draft"}
    assert state["events"][-1] == {"kind": "upload"}
    assert all(event["kind"] != "publish" for event in state["events"])
    assert state["release"] == "draft"


def test_missing_release_is_created_as_draft_then_published(tmp_path: Path) -> None:
    run = make_fake_release_runner(tmp_path, {"release": None})

    result, state = run()

    assert result.returncode == 0, result.stdout + result.stderr
    assert [event["kind"] for event in state["events"]] == [
        "create",
        "draft",
        "upload",
        "publish",
    ]
    assert state["release"] == "published"
    assert {asset["name"] for asset in state["assets"]} == ALLOWED_RELEASE_ASSETS


def test_release_publish_rerun_is_idempotent(tmp_path: Path) -> None:
    run = make_fake_release_runner(tmp_path, {"release": "published"})

    first_result, first_state = run()
    first_event_count = len(first_state["events"])
    second_result, second_state = run()

    assert first_result.returncode == 0, first_result.stdout + first_result.stderr
    assert second_result.returncode == 0, second_result.stdout + second_result.stderr
    assert [event["kind"] for event in second_state["events"][first_event_count:]] == [
        "draft",
        "upload",
        "publish",
    ]
    assert second_state["release"] == "published"
    assert {asset["name"] for asset in second_state["assets"]} == ALLOWED_RELEASE_ASSETS


def test_non_numeric_asset_id_stops_before_delete_or_upload(tmp_path: Path) -> None:
    run = make_fake_release_runner(
        tmp_path,
        {"release": "published", "assets": [{"id": "not-numeric", "name": "--help"}]},
    )

    result, state = run()

    assert result.returncode != 0
    assert state["release"] == "draft"
    assert state["events"] == [{"kind": "draft"}]
    assert all(call[:3] != ["api", "--method", "DELETE"] for call in state["calls"])
    assert all(call[:2] != ["release", "upload"] for call in state["calls"])


def test_readme_documents_safe_portable_operation_and_development() -> None:
    readme = read_file(REPOSITORY_ROOT / "README.md")

    assert readme.startswith("# 工作网络连接器")
    for phrase in (
        "Windows 10/11 x64",
        "兼容 Ubuntu 22.04 的、基于 glibc 的 x86_64 桌面 Linux",
        "旧版 glibc 或非 glibc 发行版不保证兼容",
        "WorkNetConnector-windows-x86_64.exe",
        "WorkNetConnector-linux-x86_64",
        "系统密钥环",
        "明文回退",
        "Secret Service",
        "HH:MM",
        "睡眠",
        "托盘",
        "可能",
        "跟随系统 / 简体中文 / English",
        "build.ps1",
        "build.sh",
        "python -m pytest",
        "SHA256SUMS.txt",
        "GitHub 账户密码",
        "网络凭据",
        "不支持 macOS",
        "不会自动启动",
        "进程保持运行",
        "## English",
        "Ubuntu 22.04-compatible, glibc-based x86_64 desktop Linux",
        "Older glibc and non-glibc distributions are not guaranteed",
    ):
        assert phrase in readme
