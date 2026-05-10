# X投稿自動化のランタイム成果物を repo 外に保存する補助モジュール。
# 画像プロンプト、下書きメタデータ、処理済みブックマーク state を一元管理する。
# 保存先は ~/.config/x-post-writer/x-post-writer/ 配下。

from __future__ import annotations

import json
import os
import re
from pathlib import Path


APP_NAME = "x-post-writer"
FEATURE_DIR = "x-post-writer"
BOOKMARK_STATE_FILENAME = "processed_bookmarks_state.json"
CHARACTER_SETTINGS_FILENAME = "character-settings.json"


def get_config_root() -> Path:
    base = Path(
        os.environ.get(
            "XDG_CONFIG_HOME",
            str(Path.home() / ".config"),
        )
    )
    return base / APP_NAME / FEATURE_DIR


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_runtime_root() -> Path:
    return ensure_dir(get_config_root())


def get_logs_dir() -> Path:
    return ensure_dir(get_runtime_root() / "logs")


def get_image_prompts_dir() -> Path:
    return ensure_dir(get_runtime_root() / "image-prompts")


def get_draft_metadata_dir() -> Path:
    return ensure_dir(get_runtime_root() / "draft-metadata")


def get_bookmark_state_path() -> Path:
    return get_runtime_root() / BOOKMARK_STATE_FILENAME


def get_character_settings_path() -> Path:
    return get_runtime_root() / CHARACTER_SETTINGS_FILENAME


def load_character_settings_payload() -> dict:
    path = get_character_settings_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_character_settings_payload(payload: dict) -> Path:
    path = get_character_settings_path()
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_character_setting(account_key: str) -> dict:
    payload = load_character_settings_payload()
    settings = payload.get("accounts")
    if not isinstance(settings, dict):
        return {}
    setting = settings.get(str(account_key))
    return setting if isinstance(setting, dict) else {}


def save_character_setting(account_key: str, setting: dict) -> Path:
    payload = load_character_settings_payload()
    settings = payload.get("accounts")
    if not isinstance(settings, dict):
        settings = {}
    settings[str(account_key)] = setting
    payload["accounts"] = settings
    return save_character_settings_payload(payload)


def load_bookmark_state_payload() -> dict:
    path = get_bookmark_state_path()
    if not path.exists():
        return {}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_bookmark_state_payload(payload: dict) -> Path:
    path = get_bookmark_state_path()
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def slugify(text: str, fallback: str = "draft") -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", text.strip())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-_")
    return normalized or fallback


def write_image_prompt_file(
    *,
    draft_id: str,
    position: int,
    copy_text: str,
    prompt_text: str,
    source_tweet_id: str,
) -> Path:
    filename = f"{draft_id}-p{position}-{slugify(source_tweet_id, 'source')}.md"
    path = get_image_prompts_dir() / filename
    body = (
        f"# Image Prompt for Draft {draft_id} / Part {position}\n\n"
        f"- source_tweet_id: {source_tweet_id}\n"
        f"- copy: {copy_text}\n\n"
        "## Prompt\n\n"
        f"{prompt_text.strip()}\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def get_draft_metadata_path(draft_id: str) -> Path:
    return get_draft_metadata_dir() / f"{draft_id}.json"


def save_draft_metadata(draft_id: str, payload: dict) -> Path:
    path = get_draft_metadata_path(draft_id)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def load_draft_metadata(draft_id: str) -> dict | None:
    path = get_draft_metadata_path(draft_id)
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_processed_bookmarks() -> set[str]:
    payload = load_bookmark_state_payload()
    items = payload.get("processed_tweet_ids", [])
    if not isinstance(items, list):
        return set()
    return {str(item) for item in items if str(item).strip()}


def save_processed_bookmarks(tweet_ids: set[str]) -> Path:
    payload = load_bookmark_state_payload()
    payload["processed_tweet_ids"] = sorted(tweet_ids)
    return save_bookmark_state_payload(payload)


def save_bookmark_status(
    *,
    tweet_id: str,
    status: str,
    draft_id: str | None = None,
    metadata_path: str | None = None,
    image_prompt_path: str | None = None,
) -> Path:
    payload = load_bookmark_state_payload()
    statuses = payload.get("bookmark_status")
    if not isinstance(statuses, dict):
        statuses = {}

    entry = statuses.get(str(tweet_id))
    if not isinstance(entry, dict):
        entry = {}
    entry["status"] = status
    if draft_id:
        entry["draft_id"] = draft_id
    if metadata_path:
        entry["metadata_path"] = metadata_path
    if image_prompt_path:
        entry["image_prompt_path"] = image_prompt_path

    statuses[str(tweet_id)] = entry
    payload["bookmark_status"] = statuses
    return save_bookmark_state_payload(payload)
