"""Bilingual application messages without UI or storage dependencies."""

from __future__ import annotations

import locale
import string
from types import MappingProxyType
from typing import Mapping


class I18nError(Exception):
    """Raised when language selection or message formatting is invalid."""


_CATALOGS: dict[str, dict[str, str]] = {
    "zh": {
        "app.title": "网络连接器",
        "tooltip.settings": "设置",
        "action.connect": "连接", "action.retry": "重试", "action.save": "保存",
        "action.cancel": "取消", "action.show_password": "显示密码",
        "action.hide_password": "隐藏密码", "action.exit": "退出",
        "status.waiting": "等待连接", "status.checking": "正在检查网络",
        "status.connecting": "正在连接", "status.connected": "已连接",
        "status.already_online": "已在线", "status.offline": "离线",
        "status.failed": "连接失败", "status.last_check.never": "从未检查",
        "status.last_check": "上次检查：{time}",
        "schedule.title": "定时连接", "schedule.tray_hint": "定时连接将在后台运行",
        "schedule.every_day": "每天", "schedule.next_run": "下次运行：{time}",
        "schedule.disabled": "定时连接已关闭", "schedule.invalid_time": "请输入有效时间",
        "window.close_to_tray": "关闭窗口后程序将最小化到托盘",
        "window.tray_unavailable": "系统托盘不可用，程序将继续运行并最小化到任务栏",
        "settings.title": "设置", "settings.username": "用户名", "settings.password": "密码",
        "settings.language": "语言", "settings.language.system": "跟随系统",
        "settings.language.zh": "简体中文", "settings.language.en": "English",
        "settings.credential_hint": "凭据将安全保存在本机",
        "tray.show": "显示窗口", "tray.connect": "连接网络", "tray.exit": "退出",
        "error.missing_credentials": "请先填写用户名和密码",
        "error.credential_store": "无法访问凭据存储",
        "error.portal_unreachable": "无法连接工作门户",
        "error.portal_rejected": "工作门户拒绝了连接请求",
        "error.verification_failed": "连接验证失败",
        "error.vpn_detected": "检测到 VPN 或隧道接口（{interfaces}），可能阻碍工作门户连接。请断开后重试。",
        "error.internal": "发生内部错误", "error.settings_recovered": "设置已恢复为默认值",
        "error.settings_save": "无法保存设置", "error.settings_rollback": "设置未保存，且无法恢复原凭据",
        "error.busy": "连接操作正在进行",
        "dialog.error": "错误", "dialog.info": "提示", "dialog.credentials_required": "需要凭据",
    },
    "en": {
        "app.title": "Network Connector",
        "tooltip.settings": "Settings",
        "action.connect": "Connect", "action.retry": "Retry", "action.save": "Save",
        "action.cancel": "Cancel", "action.show_password": "Show password",
        "action.hide_password": "Hide password", "action.exit": "Exit",
        "status.waiting": "Waiting to connect", "status.checking": "Checking network",
        "status.connecting": "Connecting", "status.connected": "Connected",
        "status.already_online": "Already online", "status.offline": "Offline",
        "status.failed": "Connection failed", "status.last_check.never": "Never checked",
        "status.last_check": "Last checked: {time}",
        "schedule.title": "Scheduled connection", "schedule.tray_hint": "Scheduled connection runs in the background",
        "schedule.every_day": "Every day", "schedule.next_run": "Next run: {time}",
        "schedule.disabled": "Scheduled connection is disabled", "schedule.invalid_time": "Enter a valid time",
        "window.close_to_tray": "Closing the window will minimize the app to the tray",
        "window.tray_unavailable": "System tray is unavailable; the app will remain running and be minimized to the taskbar",
        "settings.title": "Settings", "settings.username": "Username", "settings.password": "Password",
        "settings.language": "Language", "settings.language.system": "System default",
        "settings.language.zh": "Simplified Chinese", "settings.language.en": "English",
        "settings.credential_hint": "Credentials are stored securely on this device",
        "tray.show": "Show window", "tray.connect": "Connect network", "tray.exit": "Exit",
        "error.missing_credentials": "Enter a username and password first",
        "error.credential_store": "Unable to access credential storage",
        "error.portal_unreachable": "Unable to reach the work portal",
        "error.portal_rejected": "The work portal rejected the connection request",
        "error.verification_failed": "Connection verification failed",
        "error.vpn_detected": "Detected VPN or tunnel interfaces ({interfaces}) may block the work portal. Disconnect them and retry.",
        "error.internal": "An internal error occurred", "error.settings_recovered": "Settings were restored to defaults",
        "error.settings_save": "Unable to save settings",
        "error.settings_rollback": "Settings were not saved and previous credentials could not be restored",
        "error.busy": "A connection operation is already in progress",
        "dialog.error": "Error", "dialog.info": "Information", "dialog.credentials_required": "Credentials required",
    },
}

CATALOGS: Mapping[str, Mapping[str, str]] = MappingProxyType(
    {language: MappingProxyType(messages) for language, messages in _CATALOGS.items()}
)
del _CATALOGS

_UNSET = object()
_FORMATTER = string.Formatter()


def _validate_effective_language(language: object) -> str:
    if type(language) is not str or language not in ("zh", "en"):
        raise I18nError("Invalid language configuration.")
    return language


def resolve_language(mode: str, system_locale: str | None | object = _UNSET) -> str:
    """Resolve a persisted language mode to one of the available catalogs."""
    if type(mode) is not str or mode not in ("system", "zh", "en"):
        raise I18nError("Invalid language configuration.")
    if mode != "system":
        return mode

    locale_name = system_locale
    if locale_name is _UNSET:
        try:
            locale_name = locale.getlocale()[0]
        except Exception:
            return "en"
    if type(locale_name) is not str:
        return "en"
    return "zh" if locale_name.lower().replace("-", "_").split("_", 1)[0] == "zh" else "en"


def _field_names(template: str) -> set[str]:
    names: set[str] = set()
    for _, field_name, _, _ in _FORMATTER.parse(template):
        if field_name is not None:
            names.add(field_name.split(".", 1)[0].split("[", 1)[0])
    return names


class Translator:
    """Translate stable catalog keys for one effective language."""

    def __init__(self, language: str) -> None:
        self._language = _validate_effective_language(language)

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, language: str) -> None:
        self._language = _validate_effective_language(language)

    def text(self, key: str, **kwargs: object) -> str:
        if type(key) is not str:
            return "[missing]"
        template = CATALOGS[self._language].get(key)
        if template is None:
            return f"[{key}]"
        formatting_failed = set(kwargs) != _field_names(template)
        if not formatting_failed:
            try:
                formatted = template.format(**kwargs)
            except Exception:
                formatting_failed = True

        if formatting_failed:
            kwargs.clear()
            kwargs = {}
            raise I18nError("Invalid message formatting.")
        return formatted
