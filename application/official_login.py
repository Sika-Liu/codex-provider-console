"""Codex device-code login session without HTTP-route dependencies."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

class DeviceLoginSession:
    """Owns one local Codex app-server device-code login without exposing tokens."""

    def __init__(self, provider_id: str, *, codex_home: Path, timeout_seconds: int = 10):
        self.provider_id = provider_id
        self.codex_home = codex_home
        self.timeout_seconds = timeout_seconds
        self.process: subprocess.Popen[str] | None = None
        self.lock = threading.Lock()
        self.next_id = 1
        self.waiters: dict[int, tuple[threading.Event, dict]] = {}
        self.login_id = ""
        self.verification_url = ""
        self.user_code = ""
        self.status = "starting"
        self.detail = "正在启动官方登录服务…"
        self.plan_type = ""
        self.captured = False

    def _reader(self) -> None:
        assert self.process and self.process.stdout
        for raw in self.process.stdout:
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            response_id = message.get("id")
            if isinstance(response_id, int):
                with self.lock:
                    waiter = self.waiters.get(response_id)
                    if waiter:
                        waiter[1]["message"] = message
                        waiter[0].set()
                continue
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if message.get("method") == "account/login/completed" and params.get("loginId") == self.login_id:
                with self.lock:
                    if params.get("success"):
                        self.status = "completed"
                        self.detail = "登录已完成，正在保存认证状态。"
                    else:
                        self.status = "failed"
                        self.detail = str(params.get("error") or "官方登录未完成")[:300]
            elif message.get("method") == "account/updated":
                with self.lock:
                    self.plan_type = str(params.get("planType") or "")

    def _send(self, payload: dict) -> None:
        if not self.process or not self.process.stdin:
            raise RuntimeError("Codex login service is unavailable")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict) -> dict:
        with self.lock:
            request_id = self.next_id
            self.next_id += 1
            event = threading.Event()
            holder: dict = {}
            self.waiters[request_id] = (event, holder)
            self._send({"method": method, "id": request_id, "params": params})
        if not event.wait(self.timeout_seconds):
            with self.lock:
                self.waiters.pop(request_id, None)
            raise RuntimeError("Codex login service timed out")
        message = holder.get("message", {})
        if message.get("error"):
            raise RuntimeError(str(message["error"].get("message") or "Codex login service rejected the request"))
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Codex login service returned an invalid response")
        return result

    def start(self) -> dict:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.codex_home)
        try:
            self.process = subprocess.Popen(
                ["codex", "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                env=env,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Codex CLI is unavailable in the control panel container. Update the deployment and try again.") from exc
        threading.Thread(target=self._reader, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "codex_provider_console", "title": "Codex Provider Console", "version": "1.0"}})
        self._send({"method": "initialized", "params": {}})
        result = self.request("account/login/start", {"type": "chatgptDeviceCode"})
        self.login_id = str(result.get("loginId") or "")
        self.verification_url = str(result.get("verificationUrl") or "")
        self.user_code = str(result.get("userCode") or "")
        if not self.login_id or not self.verification_url or not self.user_code:
            raise RuntimeError("Codex did not return a device login code")
        self.status = "pending"
        self.detail = "请在浏览器完成官方登录。"
        return self.public_status()

    def cancel(self) -> None:
        if self.status == "pending" and self.login_id:
            try:
                self.request("account/login/cancel", {"loginId": self.login_id})
            except RuntimeError:
                pass
        self.status = "cancelled"
        self.detail = "登录已取消。"
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def public_status(self) -> dict:
        if self.process and self.process.poll() is not None and self.status in {"starting", "pending"}:
            self.status = "failed"
            self.detail = "Codex 登录服务已退出。"
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "detail": self.detail,
            "verification_url": self.verification_url if self.status == "pending" else "",
            "user_code": self.user_code if self.status == "pending" else "",
            "plan_type": self.plan_type,
            "captured": self.captured,
        }


