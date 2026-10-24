"""Verified SSH gateway for host-side Codex operations."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class HostGateway:
    deployment_key: Path
    deploy_user: str
    user_home: Path
    host_user_home: str
    host_codex_home: str
    command: Callable[[str], list[str]]
    environment: Callable[[], dict[str, str]]

    def docker_gateway(self) -> str:
        try:
            for line in Path('/proc/net/route').read_text(encoding='ascii').splitlines()[1:]:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == '00000000':
                    encoded = fields[2]
                    return '.'.join(str(int(encoded[index:index + 2], 16)) for index in range(6, -1, -2))
        except OSError:
            pass
        raise RuntimeError('无法确定 Docker 宿主机地址')

    def codex_home(self) -> str:
        configured = self.host_codex_home.strip()
        if configured.startswith('/'):
            return configured
        home = self.host_user_home.strip()
        if home.startswith('/'):
            return f"{home.rstrip('/')}/.codex"
        raise RuntimeError('未配置宿主机 Codex 数据目录，无法同步删除云端会话')

    def run(self, script: str, operation: str, timeout: int = 45) -> str:
        if not self.deployment_key.is_file():
            raise RuntimeError('缺少面板部署密钥；请先在健康检查中部署密钥')
        if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', self.deploy_user):
            raise RuntimeError(f'部署用户名无效，无法{operation}')
        try:
            result = subprocess.run(self.command(self.docker_gateway()), capture_output=True, text=True, input=script, timeout=timeout, check=False, env=self.environment())
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f'宿主机{operation}超时') from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or '宿主机未提供失败原因').strip()
            raise RuntimeError(f'宿主机{operation}失败：{detail[-600:]}')
        return (result.stdout or result.stderr).strip()
