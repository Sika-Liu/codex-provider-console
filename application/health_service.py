"""Health-check orchestration independent of FastAPI route handlers."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


CheckCallback = Callable[[dict[str, str]], None]


@dataclass(frozen=True)
class HealthDependencies:
    codex_home: Path
    config_path: Path
    auth_path: Path
    deploy_user: str
    auth_enabled: bool
    executable_user_cli_path: Callable[[], Path | None]
    fetch_latest_cli_version: Callable[[], str]
    cli_version_number: Callable[[str], str]
    deployment_key_status: Callable[[], tuple[bool, str]]
    inspect_host_app_server: Callable[[], tuple[str, str]]
    inspect_session_storage: Callable[[], tuple[str, str]]
    config_text: Callable[[], str]
    auth_configured: Callable[[], bool]
    read_profiles: Callable[[], dict[str, dict]]
    active_provider: Callable[[], str | None]
    normalize_profile: Callable[[Mapping[str, Any]], dict]
    diagnose_profile: Callable[..., dict]
    audit: Callable[..., None]


class HealthService:
    """Run the existing health checks without owning HTTP or global state."""

    def __init__(self, dependencies: HealthDependencies) -> None:
        self._deps = dependencies

    def run(self, on_check: CheckCallback | None = None) -> dict:
        deps = self._deps
        checks: list[dict[str, str]] = []

        def add(name: str, status: str, detail: str, diagnostic_detail: str | None = None, action: str | None = None) -> None:
            item = {"name": name, "status": status, "detail": detail}
            if diagnostic_detail:
                item["diagnostic_detail"] = diagnostic_detail
            if action:
                item["action"] = action
            checks.append(item)
            if on_check:
                on_check(dict(item))

        add("Codex 数据目录", "pass" if deps.codex_home.exists() else "fail", str(deps.codex_home) if deps.codex_home.exists() else f"目录不存在：{deps.codex_home}")
        cli_path = deps.executable_user_cli_path()
        cli_version = ""
        if cli_path:
            try:
                cli_version = subprocess.run([str(cli_path), "--version"], capture_output=True, text=True, timeout=8, check=False).stdout.strip().splitlines()[0]
            except (OSError, subprocess.SubprocessError, IndexError):
                cli_version = ""
        cli_installed = bool(cli_version)
        cli_detail = f"{cli_version}；已验证部署用户 {deps.deploy_user} 的实际 CLI。" if cli_installed else f"用户 {deps.deploy_user} 未检测到 Codex CLI；可在此页直接安装。"
        add("Codex CLI", "pass" if cli_installed else "fail", cli_detail)
        if cli_installed:
            try:
                latest_cli_version = deps.fetch_latest_cli_version()
                current_cli_version = deps.cli_version_number(cli_version)
                current_key = tuple(int(part) for part in current_cli_version.split("-", 1)[0].split(".")) if current_cli_version else ()
                latest_key = tuple(int(part) for part in latest_cli_version.split("-", 1)[0].split("."))
                if not current_key:
                    add("Codex CLI 版本", "warning", f"当前版本无法识别；官方最新版本为 {latest_cli_version}")
                elif current_key < latest_key:
                    add("Codex CLI 版本", "warning", f"当前 {current_cli_version}，最新 {latest_cli_version}；可以更新", action="update-codex")
                else:
                    add("Codex CLI 版本", "pass", f"当前 {current_cli_version}，已是最新版本")
            except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, urllib.error.HTTPError) as exc:
                add("Codex CLI 版本", "warning", f"暂时无法获取官方最新版本：{exc}")
        else:
            add("Codex CLI 版本", "warning", "尚未安装 Codex CLI")

        deployment_key_ready, deployment_key_detail = deps.deployment_key_status()
        add("Codex Desktop 部署密钥", "pass" if deployment_key_ready else "warning", deployment_key_detail)
        app_server_status, app_server_detail = deps.inspect_host_app_server()
        add("Codex App Server", app_server_status, app_server_detail)
        session_status, session_detail = deps.inspect_session_storage()
        add("会话文件与索引", session_status, session_detail)
        writable = deps.codex_home.exists() and os.access(deps.codex_home, os.W_OK)
        add("目录写入权限", "pass" if writable else "fail", "可写" if writable else "控制台无法写入 Codex 数据目录")

        config = deps.config_text()
        if deps.config_path.exists():
            add("config.toml", "pass" if config.strip() else "warning", "已找到配置文件" if config.strip() else "配置文件为空")
            try:
                tomllib.loads(config)
                add("config.toml 语法", "pass", "TOML 格式有效")
            except tomllib.TOMLDecodeError as exc:
                add("config.toml 语法", "fail", f"TOML 格式无效：{exc}；可在此页修复重复的 [features] 配置段")
            provider_match = re.search(r"^\s*model_provider\s*=\s*['\"]([^'\"]+)['\"]", config, re.M)
            model_match = re.search(r"^\s*model\s*=\s*['\"]([^'\"]+)['\"]", config, re.M)
            if provider_match:
                detail = f"供应商：{provider_match.group(1)}"
                detail += f"，默认模型：{model_match.group(1)}" if model_match else "；未设置默认模型（可在 Codex 中自行选择）"
                add("Codex 关键配置", "pass", detail)
            else:
                add("Codex 关键配置", "warning", "缺少 model_provider；保存并激活供应商后会自动补齐")
        else:
            add("config.toml", "warning", "尚未生成；保存并激活供应商后会创建")

        if deps.auth_path.exists():
            try:
                parsed_auth = json.loads(deps.auth_path.read_text(encoding="utf-8"))
                add("auth.json", "pass" if isinstance(parsed_auth, dict) else "fail", "JSON 格式有效" if isinstance(parsed_auth, dict) else "必须是 JSON 对象")
            except (OSError, json.JSONDecodeError):
                add("auth.json", "fail", "文件不可读或不是有效 JSON")
        else:
            add("auth.json", "warning", "未找到；官方登录和纯 API 供应商都会使用该文件保存认证，保存并激活供应商后会自动创建")

        if not deps.auth_configured() and deps.auth_enabled:
            add("控制台认证", "fail", "认证已启用但管理员账号、密码或会话密钥未配置")
        else:
            add("控制台认证", "pass", "认证配置完整" if deps.auth_enabled else "认证已关闭")

        profiles = deps.read_profiles()
        active_id = deps.active_provider()
        active = profiles.get(active_id) if active_id else None
        if not active:
            add("当前供应商", "warning", "尚未激活供应商；请先保存并激活一个供应商")
        else:
            add("当前供应商", "pass", f"{active.get('name', active_id)} ({active_id})")

            def add_supplier_check(item: dict[str, str]) -> None:
                name = "模型目录" if item["name"] == "上游模型目录" else item["name"]
                status = "fail" if name == "真实请求" and item["status"] == "warning" and item["detail"].startswith("测试「") else item["status"]
                add(f"供应商 · {name}", status, item["detail"], item.get("diagnostic_detail"))

            deps.diagnose_profile(active, on_check=add_supplier_check)

        try:
            usage = shutil.disk_usage(deps.codex_home)
            free_gb = usage.free / (1024 ** 3)
            add("磁盘空间", "pass" if free_gb >= 1 else "warning", f"剩余 {free_gb:.1f} GB")
        except OSError as exc:
            add("磁盘空间", "warning", f"无法读取磁盘空间：{exc}")

        failed = sum(item["status"] == "fail" for item in checks)
        warnings = sum(item["status"] == "warning" for item in checks)
        deps.audit("health_checked", failed=failed, warnings=warnings)
        return {"ok": failed == 0, "passed": failed == 0 and warnings == 0, "checks": checks, "summary": "环境满足使用条件。" if failed == 0 and warnings == 0 else f"发现 {failed} 项失败、{warnings} 项提醒。"}

    def plan(self) -> list[dict[str, str]]:
        names = [
            "Codex 数据目录", "Codex CLI", "Codex CLI 版本", "Codex Desktop 部署密钥", "Codex App Server",
            "会话文件与索引", "目录写入权限", "config.toml", "config.toml 语法", "Codex 关键配置",
            "auth.json", "控制台认证", "当前供应商",
        ]
        profiles = self._deps.read_profiles()
        active_id = self._deps.active_provider()
        active = profiles.get(active_id) if active_id else None
        if active:
            profile = self._deps.normalize_profile(active)
            if profile["mode"] == "official":
                names.append("供应商 · 官方认证")
            else:
                names.extend(["供应商 · Base URL", "供应商 · API Key", "供应商 · 上游协议", "供应商 · 模型目录"])
                if str(profile.get("model", "")).strip():
                    names.append("供应商 · 配置模型可见性")
                names.append("供应商 · 真实请求")
        names.append("磁盘空间")
        return [{"name": name, "status": "pending", "detail": "等待检查"} for name in names]
