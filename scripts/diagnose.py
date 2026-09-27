"""Read-only deployment diagnostics for a personal console installation."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def check(name: str, ok: bool, detail: str) -> dict[str, object]:
    return {"name": name, "ok": ok, "detail": detail}


def main() -> int:
    results: list[dict[str, object]] = []
    version_path = ROOT / "VERSION"
    version = version_path.read_text(encoding="utf-8").strip() if version_path.is_file() else ""
    results.append(check("version", bool(version), version or "VERSION is missing"))
    results.append(check("environment", (ROOT / ".env").is_file(), "configured .env" if (ROOT / ".env").is_file() else "missing .env; copy .env.example first"))
    compose = shutil.which("docker")
    if compose:
        result = subprocess.run([compose, "compose", "config", "--quiet"], cwd=ROOT, capture_output=True, text=True, check=False)
        results.append(check("compose", result.returncode == 0, "docker compose config is valid" if result.returncode == 0 else result.stderr.strip()[-500:]))
    else:
        results.append(check("compose", False, "docker is not installed"))
    results.append(check("data directory", bool(os.environ.get("CODEX_HOME_HOST", "")), "CODEX_HOME_HOST is set" if os.environ.get("CODEX_HOME_HOST") else "CODEX_HOME_HOST is not set; compose default is used"))
    required_ok = all(item["ok"] for item in (results[0], results[2]))
    print(json.dumps({"version": version, "ok": required_ok, "checks": results}, ensure_ascii=False, indent=2))
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
