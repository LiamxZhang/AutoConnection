"""Contracts for portable build scripts and GitHub release automation."""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


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


def test_ci_workflow_builds_and_uploads_each_supported_platform() -> None:
    workflow = read_file(CI_WORKFLOW)

    assert re.search(r"(?m)^on:\s*$", workflow)
    assert re.search(r"(?m)^  push:\s*$", workflow)
    assert re.search(r"(?m)^  pull_request:\s*$", workflow)
    assert "contents: read" in workflow
    assert "windows-latest" in workflow
    assert "ubuntu-22.04" in workflow
    assert 'python-version: "3.12"' in workflow
    assert "python3-tk" in workflow
    assert "xvfb" in workflow
    assert "scripts/build.ps1" in workflow
    assert "xvfb-run -a bash scripts/build.sh" in workflow
    assert "actions/checkout@v4" in workflow
    assert "actions/setup-python@v5" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "WorkNetConnector-windows-x86_64-ci" in workflow
    assert "WorkNetConnector-linux-x86_64-ci" in workflow
    assert "dist/WorkNetConnector.exe" in workflow
    assert "dist/WorkNetConnector" in workflow
    # Platform scripts own dependency installation and the single complete test run.
    assert "python -m pytest" not in workflow


def test_release_workflow_publishes_one_release_with_exact_checksums() -> None:
    workflow = read_file(RELEASE_WORKFLOW)

    assert re.search(r"(?m)^on:\s*$", workflow)
    assert re.search(r"(?m)^    tags:\s*$", workflow)
    assert re.search(r"(?m)^      - ['\"]v\*['\"]\s*$", workflow)
    assert "windows-latest" in workflow
    assert "ubuntu-22.04" in workflow
    assert 'python-version: "3.12"' in workflow
    assert "python3-tk" in workflow and "xvfb" in workflow
    assert "scripts/build.ps1" in workflow
    assert "xvfb-run -a bash scripts/build.sh" in workflow
    assert "WorkNetConnector-windows-x86_64.exe" in workflow
    assert "WorkNetConnector-linux-x86_64" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "actions/download-artifact@v4" in workflow
    assert "merge-multiple: true" in workflow
    assert "needs: build" in workflow
    assert "contents: write" in workflow
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in workflow
    assert "GH_REPO: ${{ github.repository }}" in workflow
    assert workflow.count("gh release create") == 1
    checksum_command = (
        "sha256sum WorkNetConnector-windows-x86_64.exe "
        "WorkNetConnector-linux-x86_64 > SHA256SUMS.txt"
    )
    assert checksum_command in workflow
    assert "SHA256SUMS.txt" in workflow


def test_readme_documents_safe_portable_operation_and_development() -> None:
    readme = read_file(REPOSITORY_ROOT / "README.md")

    assert readme.startswith("# 工作网络连接器")
    for phrase in (
        "Windows 10/11 x64",
        "Linux x86_64",
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
    ):
        assert phrase in readme
