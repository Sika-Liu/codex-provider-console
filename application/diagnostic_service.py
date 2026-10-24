"""Provider diagnostic orchestration independent from HTTP routes."""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable


@dataclass
class DiagnosticService:
    normalize_profile: Callable[[dict], dict]
    validate_auth_snapshot: Callable[[str], None]
    fetch_upstream_models: Callable[[str, str], dict]
    test_model_request: Callable[..., dict]

    def diagnose(
        self,
        profile: dict,
        progress: Callable[[int, str], None] | None = None,
        on_check: Callable[[dict[str, str]], None] | None = None,
    ) -> dict:
        profile = self.normalize_profile(profile)
        checks: list[dict[str, str]] = []

        def report(percent: int, stage: str) -> None:
            if progress:
                progress(percent, stage)

        def add(name: str, status: str, detail: str, diagnostic_detail: str | None = None, action: str | None = None) -> None:
            item = {"name": name, "status": status, "detail": detail}
            if diagnostic_detail:
                item["diagnostic_detail"] = diagnostic_detail
            if action:
                item["action"] = action
            checks.append(item)
            if on_check:
                on_check(dict(item))

        mode = profile["mode"]
        model = str(profile.get("model", "")).strip()
        report(12, "正在检查配置完整性")
        if mode == "official":
            snapshot = profile.get("auth_contents", "")
            try:
                self.validate_auth_snapshot(snapshot)
                add("官方认证", "pass", "官方登录快照格式有效")
            except Exception as exc:
                add("官方认证", "fail", str(getattr(exc, "detail", exc)))
        else:
            base_url = str(profile.get("base_url", "")).strip()
            api_key = str(profile.get("bearer_token") or "").strip()
            protocol = profile.get("wire_api")
            add("Base URL", "pass" if re.match(r"^https?://.+", base_url) else "fail", base_url if re.match(r"^https?://.+", base_url) else "请输入有效的 http(s) Base URL")
            no_auth = bool(profile.get("no_auth", False))
            add("API Key", "pass" if api_key or no_auth else "fail", "无需认证" if no_auth else "已配置" if api_key else "未配置")
            add("上游协议", "pass" if protocol in {"responses", "chat"} else "fail", "Responses API" if protocol == "responses" else "Chat Completions" if protocol == "chat" else "协议无效")
            if base_url and (api_key or no_auth):
                try:
                    report(35, "正在获取上游模型列表")
                    upstream = self.fetch_upstream_models(base_url, api_key)
                    names = upstream["models"]
                    add("模型目录", "pass" if names else "fail", f"{upstream['endpoint']} 返回 {len(names)} 个模型" if names else "上游没有返回可用模型")
                    if model:
                        add("配置模型可见性", "pass" if model in names else "warning", "模型在上游目录中可见" if model in names else "配置模型未出现在上游模型目录；仍可能可用")
                    if names:
                        test_model = model or names[0]
                        report(65, f"正在请求 {test_model}")
                        real_request = self.test_model_request(profile, test_model, timeout_seconds=20, max_attempts=1)
                        retry_note = f'（第 {real_request.get("attempts", 1)} 次请求成功）' if real_request["ok"] and real_request.get("attempts", 1) > 1 else ""
                        if real_request["ok"]:
                            endpoint = real_request.get("endpoint", "请求")
                            status = real_request.get("status", "")
                            detail = f"{endpoint} 返回 HTTP {status}{retry_note}"
                            diagnostic_detail = detail
                        else:
                            detail = f'测试「{test_model}」失败：{real_request.get("endpoint", "请求")} {real_request.get("detail", "请求失败")}'
                            diagnostic_detail = detail
                        # A successful model catalog proves that the endpoint and
                        # credential are usable. Some reasoning models need longer
                        # than a small diagnostic request to produce their first
                        # response, so a failed live probe is a warning rather than
                        # a reason to reject a supplier switch (Codex++ behavior).
                        add("真实请求", "pass" if real_request["ok"] else "warning", detail, diagnostic_detail)
                    else:
                        add("真实请求", "warning", "上游未返回可测试模型，该步骤未执行")
                except Exception as exc:
                    add("上游模型目录", "fail", str(getattr(exc, "detail", exc)))
            else:
                add("模型目录", "warning", "配置不完整，该步骤未执行")
                add("真实请求", "warning", "配置不完整，该步骤未执行")
        failed = [item for item in checks if item["status"] == "fail"]
        warnings = [item for item in checks if item["status"] == "warning"]
        real_request = next((item for item in checks if item["name"] == "真实请求"), None)
        passed = not failed and not warnings and bool(real_request and real_request["status"] == "pass")
        summary = "供应商基础诊断通过。" if passed else f"发现 {len(failed)} 项失败，Codex 可能无法使用该供应商。" if failed else f"基础连接可用，但有 {len(warnings)} 项需要确认。"
        report(100, "供应商诊断完成")
        return {"ok": not failed, "passed": passed, "checks": checks, "summary": summary}

