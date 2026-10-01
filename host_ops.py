"""Small, dependency-light helpers for verified host-side SSH operations."""

import os
import subprocess
from pathlib import Path


def ssh_host_key_options(known_hosts: Path, alias: str, gateway: str, environment: dict[str, str] | None = None) -> list[str]:
    """Require an operator-registered host key before host-side actions."""
    resolved_alias = alias or gateway
    if not known_hosts.is_file():
        raise RuntimeError(
            "未找到宿主机 SSH 指纹。请先从可信终端连接服务器并将指纹写入 "
            f"{known_hosts}；如面板通过 Docker 网关连接，请设置 PANEL_SSH_HOST_ALIAS。"
        )
    try:
        probe = subprocess.run(
            ["ssh-keygen", "-F", resolved_alias, "-f", str(known_hosts)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("无法检查宿主机 SSH 指纹，请确认 ssh-keygen 可用") from exc
    if probe.returncode != 0 or not probe.stdout.strip():
        raise RuntimeError(f"宿主机 SSH 指纹尚未登记（主机别名：{resolved_alias}）。请先核对并写入 {known_hosts}。")
    return [
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"HostKeyAlias={resolved_alias}",
        "-o", f"UserKnownHostsFile={known_hosts}",
    ]


def host_ssh_command(
    deployment_key: Path,
    deploy_user: str,
    gateway: str,
    known_hosts: Path,
    host_alias: str,
    environment: dict[str, str] | None = None,
) -> list[str]:
    """Build the single SSH command shape used by host-side operations."""
    return [
        "ssh", "-i", str(deployment_key),
        "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        *ssh_host_key_options(known_hosts, host_alias, gateway, environment=environment),
        "-o", "LogLevel=ERROR", f"{deploy_user}@{gateway}", "python3", "-",
    ]


def nss_environment(user_home: Path) -> dict[str, str]:
    """Provide an NSS entry when a rootless container has only a numeric UID."""
    environment = os.environ.copy()
    uid, gid = os.getuid(), os.getgid()
    try:
        has_user = any(
            line.split(":", 3)[2] == str(uid)
            for line in Path("/etc/passwd").read_text(encoding="utf-8", errors="replace").splitlines()
            if line.count(":") >= 2
        )
    except OSError:
        return environment
    if has_user:
        return environment
    nss_wrapper = next(Path("/usr/lib").rglob("libnss_wrapper.so"), None)
    if not nss_wrapper:
        return environment
    runtime_dir = Path(f"/tmp/codex-panel-nss-{uid}")
    runtime_dir.mkdir(mode=0o700, exist_ok=True)
    passwd_file, group_file = runtime_dir / "passwd", runtime_dir / "group"
    passwd_file.write_text(
        Path("/etc/passwd").read_text(encoding="utf-8", errors="replace")
        + f"codex-panel:x:{uid}:{gid}:Codex Panel:{user_home}:/usr/sbin/nologin\n",
        encoding="utf-8",
    )
    group_file.write_text(
        Path("/etc/group").read_text(encoding="utf-8", errors="replace")
        + f"codex-panel:x:{gid}:\n",
        encoding="utf-8",
    )
    os.chmod(passwd_file, 0o600)
    os.chmod(group_file, 0o600)
    preload = environment.get("LD_PRELOAD", "")
    environment["LD_PRELOAD"] = f"{nss_wrapper}:{preload}" if preload else str(nss_wrapper)
    environment["NSS_WRAPPER_PASSWD"] = str(passwd_file)
    environment["NSS_WRAPPER_GROUP"] = str(group_file)
    return environment
