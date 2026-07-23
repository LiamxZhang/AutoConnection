"""Contracts for portable build scripts and GitHub release automation."""

from __future__ import annotations

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

    ensure = workflow_step(publish, "Ensure release exists")["run"]
    assert 'if ! gh release view "$GITHUB_REF_NAME"' in ensure
    assert 'gh release create "$GITHUB_REF_NAME"' in ensure
    assert "--draft" in ensure
    synchronize = workflow_step(publish, "Synchronize release assets")["run"]
    for filename in (
        "WorkNetConnector-windows-x86_64.exe",
        "WorkNetConnector-linux-x86_64",
        "SHA256SUMS.txt",
    ):
        assert filename in synchronize
    assert "gh release delete-asset" in synchronize
    assert "gh release upload" in synchronize
    assert "--clobber" in synchronize
    publish_step = workflow_step(publish, "Publish release")["run"]
    assert 'gh release edit "$GITHUB_REF_NAME"' in publish_step
    assert "--draft=false" in publish_step

    publish_commands = "\n".join(workflow_runs(publish))
    assert publish_commands.count("gh release create") == 1
    assert publish_commands.count("gh release upload") == 1
    assert publish_commands.count("gh release edit") == 1


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
