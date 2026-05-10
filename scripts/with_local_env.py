# repo直下の .env を読み込み、指定コマンドをその環境で実行する。
# launchd から起動したジョブでも、X API や Neon の秘密情報を同じ正本から参照できるようにする。
# 旧settings.local.jsonの env は移行期間だけfallbackとして読む。
# 使い方: python with_local_env.py -- <command> [args...]

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = REPO_ROOT / ".env"
CLAUDE_SETTINGS_FILE = REPO_ROOT / ".claude" / "settings.local.json"


def ensure_cli_path(env: dict[str, str]) -> None:
    home = Path.home()
    pyenv_root = Path(env.get("PYENV_ROOT") or home / ".pyenv")
    additions = [
        home / ".local" / "bin",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        pyenv_root / "shims",
        pyenv_root / "bin",
    ]
    existing = env.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    parts = [str(path) for path in additions] + existing.split(":")
    deduped = list(dict.fromkeys(part for part in parts if part))
    env["PATH"] = ":".join(deduped)


def load_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.exists():
        return {}

    loaded: dict[str, str] = {}
    pattern = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(raw_line)
        if not match:
            continue
        key, raw_value = match.groups()
        raw_value = raw_value.strip()
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
            value = parts[0] if parts else ""
        except ValueError:
            value = raw_value.strip("\"'")
        loaded[key] = value
    return loaded


def load_settings_env() -> dict[str, str]:
    if not CLAUDE_SETTINGS_FILE.exists():
        return {}

    config = json.loads(CLAUDE_SETTINGS_FILE.read_text(encoding="utf-8"))
    env = config.get("env")
    if not isinstance(env, dict):
        return {}

    loaded: dict[str, str] = {}
    for key, value in env.items():
        if isinstance(key, str):
            loaded[key] = value if isinstance(value, str) else str(value)
    return loaded


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        print("❌ 実行コマンドが必要です", file=sys.stderr)
        return 1

    env = os.environ.copy()
    for key, value in load_settings_env().items():
        env.setdefault(key, value)
    env.update(load_dotenv())
    env.setdefault("X_POST_WRITER_ROOT", str(REPO_ROOT))
    ensure_cli_path(env)

    completed = subprocess.run(args, cwd=str(REPO_ROOT), env=env)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
