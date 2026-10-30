"""Reverse-proxy validation and host script construction."""
from __future__ import annotations

import base64
import json
import os
import re
import socket
from pathlib import Path

from fastapi import HTTPException

REVERSE_PROXY_UPSTREAM = "codex-provider-console:8787"

def host_panel_project_path() -> str:
    home = os.environ.get("HOST_USER_HOME_PATH", "").strip()
    if home.startswith("/"):
        return str(Path(home) / "codex-provider-console")
    raise RuntimeError("未找到宿主机控制台目录，无法管理反向代理")

def validate_reverse_proxy_input(domain: str, certificate_pem: str, private_key_pem: str) -> tuple[str, str, str, str]:
    domain, upstream = domain.strip().lower(), REVERSE_PROXY_UPSTREAM
    certificate_pem, private_key_pem = certificate_pem.strip() + "\n", private_key_pem.strip() + "\n"
    if not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain):
        raise HTTPException(422, "请输入有效的域名，例如 console.example.com")
    if "-----BEGIN CERTIFICATE-----" not in certificate_pem or "-----END CERTIFICATE-----" not in certificate_pem:
        raise HTTPException(422, "证书必须是 PEM 格式，并包含 BEGIN/END CERTIFICATE")
    if "-----BEGIN" not in private_key_pem or "PRIVATE KEY-----" not in private_key_pem:
        raise HTTPException(422, "私钥必须是 PEM 格式，并包含 BEGIN/END PRIVATE KEY")
    if len(certificate_pem) > 200_000 or len(private_key_pem) > 200_000:
        raise HTTPException(422, "证书或私钥过大")
    return domain, upstream, certificate_pem, private_key_pem


def check_reverse_proxy_upstream(upstream: str) -> None:
    host, separator, port = upstream.rpartition(":")
    if not separator:
        host, port = upstream, "80"
    try:
        with socket.create_connection((host, int(port)), timeout=5):
            pass
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"无法连接上游服务 {upstream}：{exc}") from exc


def reverse_proxy_certificate_preflight_script(domain: str, certificate_pem: str) -> str:
    payload = json.dumps({"domain": domain, "certificate": base64.b64encode(certificate_pem.encode()).decode()})
    return f'''import base64, json, subprocess, tempfile
from pathlib import Path
data = json.loads({payload!r})
with tempfile.TemporaryDirectory(prefix="proxy-cert-check-") as directory:
    certificate = Path(directory) / "certificate.pem"
    certificate.write_bytes(base64.b64decode(data["certificate"]))
    hostname = subprocess.run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkhost", data["domain"]], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if hostname.returncode != 0 or "does match certificate" not in hostname.stdout.lower():
        raise SystemExit("证书不包含输入的域名：" + hostname.stdout.strip()[-300:])
    expiration = subprocess.run(["openssl", "x509", "-in", str(certificate), "-noout", "-checkend", "0"], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if expiration.returncode != 0:
        raise SystemExit("证书已过期：" + expiration.stdout.strip()[-300:])
print("certificate_valid")
'''


def reverse_proxy_apply_script(domain: str, upstream: str, certificate_pem: str, private_key_pem: str, *, validate_only: bool = False) -> str:
    payload = json.dumps({
        "project": host_panel_project_path(), "domain": domain, "upstream": upstream,
        "certificate": base64.b64encode(certificate_pem.encode()).decode(),
        "private_key": base64.b64encode(private_key_pem.encode()).decode(),
        "validate_only": validate_only,
    })
    return f'''import base64, json, os, shutil, subprocess, sys, uuid
from pathlib import Path

data = json.loads({payload!r})
project = Path(data["project"])
compose = project / "compose.yml"
if not compose.is_file():
    raise SystemExit("控制台部署目录或 compose.yml 不存在")
listeners = subprocess.run(["ss", "-ltn"], text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
system_nginx = shutil.which("nginx") and ("LISTEN" in listeners) and (":80 " in listeners or ":443 " in listeners)
if system_nginx:
    domain = data["domain"]
    staging = project / ".reverse-proxy-host-staging"
    if data["validate_only"]:
        print(json.dumps({{"validated": True, "backend": "system_nginx", "upstream": "127.0.0.1:8787"}}))
        sys.exit(0)
    stage = project / (".external-proxy-stage-" + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        (stage / "certificate.pem").write_bytes(base64.b64decode(data["certificate"]))
        (stage / "private-key.pem").write_bytes(base64.b64decode(data["private_key"]))
        config = ("server {{\\n"
            "    listen 80;\\n"
            "    server_name " + domain + ";\\n"
            "    return 301 https://$host$request_uri;\\n"
            "}}\\n\\n"
            "server {{\\n"
            "    listen 443 ssl;\\n"
            "    server_name " + domain + ";\\n"
            "    ssl_certificate /etc/nginx/codex-provider-console-certs/certificate.pem;\\n"
            "    ssl_certificate_key /etc/nginx/codex-provider-console-certs/private-key.pem;\\n"
            "    ssl_protocols TLSv1.2 TLSv1.3;\\n"
            "    # Managed by codex-provider-console.\\n"
            "    location / {{\\n"
            "        proxy_pass http://127.0.0.1:8787;\\n"
            "        proxy_http_version 1.1;\\n"
            "        proxy_set_header Host $host;\\n"
            "        proxy_set_header X-Real-IP $remote_addr;\\n"
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\\n"
            "        proxy_set_header X-Forwarded-Proto $scheme;\\n"
            "        proxy_read_timeout 300s;\\n"
            "    }}\\n"
            "}}\\n")
        (stage / "default.conf").write_text(config, encoding="utf-8")
        if staging.exists():
            shutil.rmtree(staging)
        stage.rename(staging)
        deployed = subprocess.run(["sudo", "-n", "/usr/local/sbin/codex-provider-console-nginx-deploy"], cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if deployed.returncode != 0:
            raise SystemExit(deployed.stdout[-1200:])
        print(json.dumps({{"running": True, "domain": domain, "backend": "system_nginx", "upstream": "127.0.0.1:8787"}}))
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    sys.exit(0)
root = project / "reverse-proxy"
stage = project / (".reverse-proxy-stage-" + uuid.uuid4().hex)
previous = project / ".reverse-proxy-previous"
try:
    nginx_dir, cert_dir = stage / "nginx", stage / "certs"
    nginx_dir.mkdir(parents=True, mode=0o700)
    cert_dir.mkdir(mode=0o700)
    certificate = base64.b64decode(data["certificate"])
    private_key = base64.b64decode(data["private_key"])
    (cert_dir / "certificate.pem").write_bytes(certificate)
    (cert_dir / "private-key.pem").write_bytes(private_key)
    os.chmod(cert_dir / "certificate.pem", 0o600)
    os.chmod(cert_dir / "private-key.pem", 0o600)
    config = """server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {domain};
    ssl_certificate /etc/nginx/certs/certificate.pem;
    ssl_certificate_key /etc/nginx/certs/private-key.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    # Resolve the Compose service name at request time.  This also lets the
    # isolated `nginx -t` validation container check syntax without joining
    # the application's Docker network.
    resolver 127.0.0.11 ipv6=off valid=30s;
    set $proxy_upstream http://{upstream};
    location / {{
        proxy_pass $proxy_upstream;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }}
}}
"""
    (nginx_dir / "default.conf").write_text(config, encoding="utf-8")
    os.chmod(nginx_dir / "default.conf", 0o600)
    validation = subprocess.run([
        "docker", "run", "--rm", "-v", f"{{nginx_dir}}:/etc/nginx/conf.d:ro",
        "-v", f"{{cert_dir}}:/etc/nginx/certs:ro", "nginx:1.27-alpine", "nginx", "-t",
    ], text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if validation.returncode != 0:
        raise SystemExit("Nginx 配置或证书校验失败：" + validation.stdout[-1200:])
    if data["validate_only"]:
        print(json.dumps({{"validated": True}}))
        sys.exit(0)
    if previous.exists():
        shutil.rmtree(previous)
    if root.exists():
        root.rename(previous)
    stage.rename(root)
    deployed = subprocess.run([
        "docker", "compose", "--profile", "reverse-proxy", "up", "-d", "--force-recreate", "reverse-proxy",
    ], cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if deployed.returncode != 0:
        if root.exists():
            shutil.rmtree(root)
        if previous.exists():
            previous.rename(root)
            subprocess.run(["docker", "compose", "--profile", "reverse-proxy", "up", "-d", "--force-recreate", "reverse-proxy"], cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        raise SystemExit(deployed.stdout[-1200:])
    if previous.exists():
        shutil.rmtree(previous)
    print(json.dumps({{"running": True, "domain": data["domain"]}}))
finally:
    if stage.exists():
        shutil.rmtree(stage)
'''


def reverse_proxy_status_script(domain: str = "") -> str:
    project = json.dumps(host_panel_project_path())
    target = json.dumps(domain)
    return f'''import json, ssl, subprocess, urllib.error, urllib.request
from pathlib import Path
project = Path({project})
target = {target}
root = project / "reverse-proxy"
result = subprocess.run(["docker", "compose", "--profile", "reverse-proxy", "ps", "--status", "running", "--services"], cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
docker_configured = (root / "nginx" / "default.conf").is_file()
docker_running = "reverse-proxy" in result.stdout.splitlines()
system_configured = False
system_config_path = ""
for directory in (Path("/etc/nginx/conf.d"), Path("/etc/nginx/sites-enabled"), Path("/etc/nginx/sites-available")):
    try:
        for path in directory.glob("*.conf"):
            if not path.is_file():
                continue
            contents = path.read_text(encoding="utf-8", errors="ignore")
            managed = "# Managed by codex-provider-console." in contents
            if target:
                match = managed and "server_name " + target + ";" in contents
            else:
                match = managed and "server_name " in contents
            if match:
                system_configured = True
                system_config_path = str(path)
                if not target:
                    import re
                    found = re.search(r"server_name\\s+([a-z0-9.-]+)\\s*;", contents, re.IGNORECASE)
                    if found:
                        target = found.group(1).lower()
                break
    except OSError:
        pass
configured = docker_configured or system_configured
running = docker_running or system_configured
backend = "system_nginx" if system_configured else ("docker" if docker_configured else "")
upstream = "127.0.0.1:8787" if backend == "system_nginx" else ("codex-provider-console:8787" if backend == "docker" else "")
https_verified = False
https_error = ""
if configured and running and target:
    try:
        request = urllib.request.Request("https://" + target + "/", headers={{"User-Agent": "Codex-Provider-Console/health"}})
        with urllib.request.urlopen(request, timeout=8, context=ssl.create_default_context()) as response:
            https_verified = 200 <= response.status < 500
            if not https_verified:
                https_error = "HTTPS 返回 HTTP " + str(response.status)
    except urllib.error.HTTPError as exc:
        https_verified = exc.code < 500
        if not https_verified:
            https_error = "HTTPS 返回 HTTP " + str(exc.code)
    except Exception as exc:
        https_error = str(exc).strip()[:240] or exc.__class__.__name__
elif not configured:
    https_error = "尚未生成 Nginx 配置"
elif not running:
    https_error = "反向代理容器未运行"
print(json.dumps({{"configured": configured, "running": running, "backend": backend, "domain": target, "upstream": upstream, "https_verified": https_verified, "https_error": https_error}}))
'''


def reverse_proxy_secure_mode_script() -> str:
    project = json.dumps(host_panel_project_path())
    return f"""
import json, os, re, shutil, subprocess, uuid
from pathlib import Path
project = Path({project})
env_path = project / ".env"
if not env_path.is_file():
    raise SystemExit("云端项目缺少 .env，无法启用安全模式")
original = env_path.read_text(encoding="utf-8")
backup = project / (".env.before-secure-" + uuid.uuid4().hex)
tmp = project / (".env.secure-" + uuid.uuid4().hex)
shutil.copy2(env_path, backup)
def set_env(text, key, value):
    lines = text.splitlines()
    pattern = re.compile(r"^" + chr(92) + "s*" + re.escape(key) + chr(92) + "s*=")
    replaced = False
    output = []
    for line in lines:
        if pattern.match(line):
            output.append(key + "=" + value)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(key + "=" + value)
    return "\\n".join(output) + "\\n"
tmp.write_text(set_env(set_env(original, "PANEL_BIND", "127.0.0.1"), "PANEL_COOKIE_SECURE", "true"), encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, env_path)
settings_path = Path(project).parent / ".codex" / "control-panel-settings.json"
if settings_path.is_file():
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {{}}
    settings["panel_cookie_secure"] = True
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    os.chmod(settings_path, 0o600)
subprocess.Popen(
    ["docker", "compose", "up", "-d", "--force-recreate", "codex-provider-console"],
    cwd=project,
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
backup.unlink(missing_ok=True)
print(json.dumps({{"secure_mode": True, "panel_bind": "127.0.0.1", "cookie_secure": True}}))
"""


def reverse_proxy_disable_secure_mode_script() -> str:
    project = json.dumps(host_panel_project_path())
    return f"""
import json, os, re, shutil, subprocess, uuid
from pathlib import Path
project = Path({project})
env_path = project / ".env"
if not env_path.is_file():
    raise SystemExit("云端项目缺少 .env，无法关闭安全模式")
original = env_path.read_text(encoding="utf-8")
backup = project / (".env.before-disable-secure-" + uuid.uuid4().hex)
tmp = project / (".env.disable-secure-" + uuid.uuid4().hex)
shutil.copy2(env_path, backup)
def set_env(text, key, value):
    lines = text.splitlines()
    pattern = re.compile(r"^" + chr(92) + "s*" + re.escape(key) + chr(92) + "s*=")
    replaced = False
    output = []
    for line in lines:
        if pattern.match(line):
            output.append(key + "=" + value)
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(key + "=" + value)
    return "\\n".join(output) + "\\n"
tmp.write_text(set_env(set_env(original, "PANEL_BIND", "0.0.0.0"), "PANEL_COOKIE_SECURE", "false"), encoding="utf-8")
os.chmod(tmp, 0o600)
os.replace(tmp, env_path)
settings_path = Path(project).parent / ".codex" / "control-panel-settings.json"
if settings_path.is_file():
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        settings = {{}}
    settings["panel_cookie_secure"] = False
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
    os.chmod(settings_path, 0o600)
subprocess.Popen(
    ["docker", "compose", "up", "-d", "--force-recreate", "codex-provider-console"],
    cwd=project, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
)
backup.unlink(missing_ok=True)
print(json.dumps({{"secure_mode": False, "panel_bind": "0.0.0.0", "cookie_secure": False}}))
"""


def reverse_proxy_delete_script(domain: str = "") -> str:
    project = json.dumps(host_panel_project_path())
    target = json.dumps(domain.strip().lower())
    return f'''import shutil, subprocess
from pathlib import Path
project = Path({project})
target = {target}
system_config = Path("/etc/nginx/conf.d/codex-provider-console.conf")
system_managed = False
for candidate in (system_config, *Path("/etc/nginx/conf.d").glob("*.conf"), *Path("/etc/nginx/sites-enabled").glob("*.conf")):
    try:
        contents = candidate.read_text(encoding="utf-8", errors="ignore")
        if "# Managed by codex-provider-console." in contents and "server_name " + target + ";" in contents:
            system_config = candidate
            system_managed = True
            break
    except OSError:
        pass
if target and system_managed:
    staging = project / ".reverse-proxy-host-staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(mode=0o700)
    (staging / "delete").write_text(target, encoding="utf-8")
    removed = subprocess.run(["sudo", "-n", "/usr/local/sbin/codex-provider-console-nginx-deploy"], cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if removed.returncode != 0:
        raise SystemExit(removed.stdout[-1200:])
    print("deleted system nginx proxy")
    raise SystemExit(0)
root = project / "reverse-proxy"
result = subprocess.run(["docker", "compose", "--profile", "reverse-proxy", "rm", "-sf", "reverse-proxy"], cwd=project, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
if result.returncode != 0:
    raise SystemExit(result.stdout[-1200:])
if root.exists():
    if root.is_symlink():
        raise SystemExit("拒绝删除意外的符号链接")
    shutil.rmtree(root)
print("deleted")
'''
