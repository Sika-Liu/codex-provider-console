"""Provider-switch orchestration independent of FastAPI route handlers."""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from fastapi import HTTPException

from application.config_service import (
    canonicalize_base_config,
    normalize_features_sections,
    remove_goals_feature,
    set_goals_feature,
    split_toml_document,
    toml_quote,
)
from application.provider_activation import ProviderActivationValidationError, build_activation_plan
from provider_domain import backfill_profile_model, is_profile_usable, normalize_profile, storage_profile


ProgressReporter = Callable[[int, str], None]


def activation_error_message(detail: object) -> str:
    """Turn provider activation failures into the existing user-facing message."""
    if isinstance(detail, dict):
        message = str(detail.get("message") or detail.get("detail") or "供应商切换失败")
        check = detail.get("check")
        diagnostic = check.get("diagnostic") if isinstance(check, dict) else None
        checks = diagnostic.get("checks") if isinstance(diagnostic, dict) else None
        if isinstance(checks, list):
            failed = [
                f"{item.get('name', '检查')}：{item.get('detail', '失败')}"
                for item in checks
                if isinstance(item, dict) and item.get("status") == "fail"
            ]
            if failed:
                return f"{message}\n" + "\n".join(failed)
        return message
    return str(detail or "供应商切换失败")


@dataclass(frozen=True)
class ProviderSwitchDependencies:
    panel_settings: Callable[[], Mapping[str, Any]]
    read_profiles: Callable[[], dict[str, dict]]
    active_provider: Callable[[], str | None]
    active_default_model: Callable[[], str | None]
    diagnose_profile: Callable[[dict], dict]
    backup_state: Callable[..., tuple[str, Path]]
    config_text: Callable[[], str]
    run_host_app_server_control: Callable[[str], object]
    migrate_session_provider: Callable[..., dict]
    read_auth_overrides: Callable[[], dict]
    write_private: Callable[[Path, str], None]
    restore_session_state: Callable[[Path], None]
    audit: Callable[..., None]
    prune_provider_switch_backups: Callable[[], list[str]]
    app_server_lock: Any
    common_config_path: Path
    config_path: Path
    auth_path: Path
    profile_path: Path
    settings_path: Path
    session_provider_id: str
    host_relay_base_url: str


class ProviderSwitchService:
    """Apply one provider atomically while preserving rollback behavior."""

    def __init__(self, dependencies: ProviderSwitchDependencies) -> None:
        self._deps = dependencies

    def run(
        self,
        provider_id: str,
        *,
        verify: bool = True,
        model_override: str | None = None,
        progress: ProgressReporter | None = None,
    ) -> dict:
        deps = self._deps

        def report(percent: int, stage: str) -> None:
            if progress:
                progress(percent, stage)

        report(8, "正在验证供应商配置")
        if not deps.panel_settings()["provider_switching_enabled"]:
            raise HTTPException(409, "Provider switching is disabled in the control panel")
        profiles = deps.read_profiles()
        profile = profiles.get(provider_id)
        if not profile:
            raise HTTPException(404, "Provider profile not found")
        profile = normalize_profile(profile)
        mode = profile["mode"]
        usable, unusable_reason = is_profile_usable(profile)
        if not usable:
            raise HTTPException(422, unusable_reason)
        report(18, "正在诊断上游连通性")
        diagnostic = deps.diagnose_profile(profile) if verify else {"ok": True, "checks": [], "summary": "诊断已跳过"}
        check = {"ok": diagnostic["ok"], "detail": diagnostic["summary"], "diagnostic": diagnostic}
        if not check["ok"]:
            detail = activation_error_message({"message": "供应商连通性测试失败；配置未改动。", "check": check})
            deps.audit("provider_switch_rejected", provider_id=provider_id, detail=detail)
            raise HTTPException(422, {"message": "供应商连通性测试失败；配置未改动。", "check": check})
        diagnostic_warnings = [
            f"{item['name']}：{item['detail']}"
            for item in diagnostic["checks"]
            if item["status"] == "warning"
        ]
        report(38, "诊断完成，正在保存当前状态")
        previous_provider_id = deps.active_provider()
        previous_model = deps.active_default_model()
        backfilled_provider_id = None
        if previous_provider_id and previous_provider_id != provider_id and previous_provider_id in profiles:
            backfilled_profile, changed = backfill_profile_model(profiles[previous_provider_id], previous_model)
            if changed:
                profiles[previous_provider_id] = backfilled_profile
                backfilled_provider_id = previous_provider_id

        report(48, "正在创建配置备份")
        backup_id, backup_dir = deps.backup_state(include_sessions=True, reason="provider_switch")
        saved_common = deps.common_config_path.read_text(encoding="utf-8") if deps.common_config_path.exists() else deps.config_text()
        base = remove_goals_feature(canonicalize_base_config(saved_common))
        provider_config = profile.get("config_contents", "").strip()
        if re.search(r"^\s*model_provider\s*=", provider_config, re.M):
            provider_config = ""
        if provider_config:
            masked_token = 'experimental_bearer_token = "***"'
            if profile.get("bearer_token"):
                provider_config = provider_config.replace(masked_token, f'experimental_bearer_token = {toml_quote(profile["bearer_token"])}')
            _, provider_config = split_toml_document(canonicalize_base_config(provider_config))
        if profile.get("goals_configured", False):
            provider_config = set_goals_feature(provider_config, bool(profile.get("goals_enabled", False)))
        base_root, base_sections = split_toml_document(base)
        merged_sections = normalize_features_sections("\n\n".join(part for part in (base_sections, provider_config) if part))
        try:
            activation_plan = build_activation_plan(
                profile,
                base_root=base_root,
                merged_sections=merged_sections,
                session_provider_id=deps.session_provider_id,
                host_relay_base_url=deps.host_relay_base_url,
                model_override=model_override,
            )
        except ProviderActivationValidationError as exc:
            raise HTTPException(422, str(exc)) from exc
        selected_model = activation_plan.selected_model
        app_server_stopped = False
        migration = {"threads": 0, "rollout_files": 0, "rollout_records": 0}
        try:
            report(62, "正在停止 Codex App Server")
            with deps.app_server_lock:
                deps.run_host_app_server_control("stop")
                app_server_stopped = True
                report(74, "正在写入供应商配置")
                if backfilled_provider_id:
                    deps.write_private(deps.profile_path, json.dumps({key: storage_profile(value) for key, value in profiles.items()}, ensure_ascii=False, indent=2) + "\n")
                deps.write_private(deps.config_path, activation_plan.config_text)
                deps.write_private(deps.auth_path, activation_plan.auth_text)
                report(82, "正在更新已有会话的供应商身份")
                migration = deps.migrate_session_provider(deps.session_provider_id, apply=True, backup_dir=backup_dir)
                settings = deps.read_auth_overrides()
                settings["active_provider_id"] = provider_id
                deps.write_private(deps.settings_path, json.dumps(settings, ensure_ascii=False, indent=2) + "\n")
                report(88, "正在启动 Codex App Server")
                deps.run_host_app_server_control("start")
        except (OSError, RuntimeError, sqlite3.Error, tomllib.TOMLDecodeError) as exc:
            for name, target in (("config.toml", deps.config_path), ("auth.json", deps.auth_path), ("control-panel-profiles.json", deps.profile_path), ("control-panel-settings.json", deps.settings_path)):
                backup_file = backup_dir / name
                if backup_file.exists():
                    shutil.copy2(backup_file, target)
                    os.chmod(target, 0o600)
            try:
                deps.restore_session_state(backup_dir)
            except OSError:
                pass
            if app_server_stopped:
                try:
                    with deps.app_server_lock:
                        deps.run_host_app_server_control("start")
                except RuntimeError:
                    pass
            deps.audit("provider_switch_failed", provider_id=provider_id, detail=str(exc))
            raise HTTPException(502, "供应商配置未能完成应用；已恢复旧配置并尝试重新启动 Codex App Server。") from exc
        report(96, "正在确认新配置")
        pruned_backup_ids = deps.prune_provider_switch_backups()
        runtime_detail = "Codex App Server 已重启，新的连接会读取当前供应商配置。"
        if diagnostic_warnings:
            runtime_detail += " 诊断警告：" + "；".join(diagnostic_warnings)
        runtime = {"restarted": True, "detail": runtime_detail}
        deps.audit(
            "provider_switched",
            provider_id=provider_id,
            backup_id=backup_id,
            model=selected_model or None,
            previous_provider_id=previous_provider_id,
            backfilled_provider_id=backfilled_provider_id,
            migration=migration,
            diagnostic_warnings=diagnostic_warnings,
            app_server_restarted=True,
            pruned_backup_ids=",".join(pruned_backup_ids) or None,
        )
        return {
            "active_provider": provider_id,
            "backup_id": backup_id,
            "check": check,
            "model": selected_model or None,
            "backfilled_provider_id": backfilled_provider_id,
            "mode": mode,
            "migration": migration,
            "diagnostic_warnings": diagnostic_warnings,
            "pruned_backup_ids": pruned_backup_ids,
            "runtime": runtime,
        }
