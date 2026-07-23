# 工作网络连接器 / WorkNetConnector

工作网络连接器是一个便携式桌面程序，用于手动或每日一次连接兼容的工作网络门户。项目支持 Windows 10/11 x64，以及兼容 Ubuntu 22.04 的、基于 glibc 的 x86_64 桌面 Linux；每个平台发布一个无需安装的单文件程序。旧版 glibc 或非 glibc 发行版不保证兼容。

## 下载与运行

从 GitHub Release 下载对应文件：

- Windows：`WorkNetConnector-windows-x86_64.exe`，双击运行，或在文件所在目录执行 `./WorkNetConnector-windows-x86_64.exe`。
- Linux：`WorkNetConnector-linux-x86_64`，执行 `chmod +x WorkNetConnector-linux-x86_64` 后运行 `./WorkNetConnector-linux-x86_64`。

首次运行时点击右上角齿轮，填写工作网络用户名和密码并保存。用户名和密码只存入操作系统密钥环，没有明文回退；普通设置文件只保存语言和定时配置。Linux 必须有可用且已解锁的 Secret Service 密钥环，例如 GNOME Keyring 或提供 Secret Service 的兼容桌面服务。

## 使用方式

- 点击“连接”可立即发起一次连接。程序启动时只检查当前网络状态，不会自动提交登录。
- 可启用一个每天执行一次的 `HH:MM` 定时任务。任务仅在程序进程保持运行（包括托盘运行）时有效；若程序运行期间因系统睡眠错过时间，唤醒后会补执行一次。程序未运行时不会补任务。
- 系统托盘可用于显示窗口、连接和退出。关闭主窗口通常会隐藏到托盘；若 Linux 桌面或其他系统环境没有可用托盘，程序会给出提示并退回任务栏/窗口行为。
- VPN 诊断仅根据活动接口判断“可能存在 VPN 或隧道干扰”，这是概率性提示，不是 VPN 已开启或故障原因的证明。
- 设置中的语言下拉选项为：跟随系统 / 简体中文 / English。

## 校验发布文件

Release 同时提供 `SHA256SUMS.txt`。Linux 下载两个程序和校验文件后可运行：

```bash
sha256sum -c SHA256SUMS.txt
```

Windows 可查看清单并单独计算文件摘要进行比对：

```powershell
Get-Content .\SHA256SUMS.txt
Get-FileHash .\WorkNetConnector-windows-x86_64.exe -Algorithm SHA256
```

## 本地开发与构建

需要 Python 3.12。开发者测试命令：

```powershell
python -m pip install ".[dev]"
python -m pytest
```

构建脚本会解析自身位置，因此可从任意当前目录调用；它们只使用仓库内的 `.venv-build`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build.ps1
```

```bash
bash ./scripts/build.sh
```

输出位于 `dist/WorkNetConnector.exe`（Windows）或 `dist/WorkNetConnector`（Linux）。

## 安全与限制

绝不要把 GitHub 账户密码、网络凭据、访问令牌或其他秘密写入命令或受 Git 跟踪的文件。应用不会提供明文凭据存储作为密钥环失败时的降级方案。

当前不支持 macOS，不会自动启动或注册系统启动项。每日调度器不是系统服务，必须由程序进程保持运行。

## English

WorkNetConnector is a portable, one-file desktop connector for Windows 10/11 x64 and Ubuntu 22.04-compatible, glibc-based x86_64 desktop Linux. Older glibc and non-glibc distributions are not guaranteed to work. Download the platform binary from GitHub Releases, verify it against `SHA256SUMS.txt`, and run it without an installer.

On first run, open the gear settings and save the network username and password. Credentials are stored only in the operating-system keyring, with no plaintext fallback. Linux requires an available, unlocked Secret Service provider. Manual Connect and one daily `HH:MM` schedule are supported while the app or tray process remains running; a sleep-missed run is caught up once, but closed-app runs are not. Tray-unavailable desktops fall back to the taskbar/window. VPN messages are probabilistic diagnostics, not proof.

Language choices are System default, Simplified Chinese, and English. macOS and automatic startup are not supported. Never place GitHub passwords, network credentials, or tokens in commands or tracked files.
