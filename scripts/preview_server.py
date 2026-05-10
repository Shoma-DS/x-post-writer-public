# Neon DBから下書きデータを取得してブラウザプレビューに渡すローカルAPIサーバー。
# Discord通知・localtunnelによる公開URLも管理する。
# 起動: bash start_preview.sh（推奨）または python preview_server.py

import json
import base64
import hashlib
import hmac
import html
import mimetypes
import os
import queue
import re
import secrets
import shlex
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote as urlquote, urlencode, unquote, urljoin
from http.cookies import SimpleCookie
import psycopg2
from psycopg2.extras import RealDictCursor
from runtime_store import (
    get_runtime_root,
    load_character_setting,
    load_draft_metadata,
    save_character_setting,
    save_draft_metadata,
    slugify,
)
from draft_manager import update_draft_image

PORT = 8765
PREVIEW_DIR   = Path(__file__).parent / "preview"
BOOKMARKS_FILE = Path(__file__).parent / "bookmarks_latest.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = REPO_ROOT / ".env"
ACCOUNTS_CONFIG_PATH = Path(__file__).parent / "accounts_config.json"
ACCOUNT_INFO_DIR = Path(__file__).parent.parent / "accounts"
LOGO_PRESETS_PATH = Path(__file__).parent / "logo_presets.json"
PLAYWRIGHT_AUTO_POST_SCRIPT = Path(__file__).parent / "x_auto_post_playwright.mjs"
ENV_SETTINGS_ALLOWED_EXACT = {"DISCORD_WEBHOOK_X_DRAFT", "NEON_DATABASE_URL"}
ENV_SETTINGS_ALLOWED_PREFIXES = ("X_", "NEON_")
AUTH_SESSION_COOKIE = "x_draft_session"
AUTH_STATE_COOKIE = "x_draft_oauth_state"
GOOGLE_OAUTH_SCOPES = "openid email profile"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def esc_html(value: str) -> str:
    return html.escape(str(value or ""), quote=True)


def parse_env_file(path: Path = ENV_PATH) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
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
        values[key] = value
    return values


def load_repo_env() -> dict[str, str]:
    values = parse_env_file()
    for key, value in values.items():
        os.environ.setdefault(key, value)
    return values


load_repo_env()

PUBLIC_URL = os.environ.get("LT_PUBLIC_URL", f"http://localhost:{PORT}")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_X_DRAFT", "")
LOCAL_IMAGE_ROOTS = [
    Path(os.environ.get("X_POST_WRITER_ROOT", Path.cwd())) / "personal",
    Path.home() / ".codex" / "generated_images",
]
OBSIDIAN_VAULT_ENV_KEYS = ("X_POST_OBSIDIAN_VAULT", "OBSIDIAN_VAULT_PATH")

# oauth2_setup.py からのコールバックを一時保存する（プロセス内共有）
_oauth2_pending: dict = {}
_image_generation_jobs: dict[str, dict] = {}
_image_generation_lock = threading.Lock()
_image_generation_processes: dict[str, subprocess.Popen] = {}
_image_rewrite_jobs: dict[str, dict] = {}
_image_rewrite_lock = threading.Lock()
_image_rewrite_processes: dict[str, subprocess.Popen] = {}
_auto_post_jobs: dict[str, dict] = {}


def image_generation_output_dir() -> Path:
    return Path(os.environ.get("X_POST_WRITER_ROOT", REPO_ROOT)) / "outputs" / "x-post-images"


def current_dev_mode_label() -> str:
    try:
        mode = (REPO_ROOT / ".dev-mode").read_text(encoding="utf-8").strip()
    except OSError:
        mode = ""
    if mode == "team":
        return "チーム開発モード"
    if mode == "personal":
        return "個人開発モード"
    return mode or "未設定"


def image_generation_search_roots() -> list[Path]:
    return [
        Path.home() / ".codex" / "generated_images",
        image_generation_output_dir(),
    ]


def logo_cache_dir() -> Path:
    return image_generation_output_dir() / "logos"


def reference_image_upload_dir() -> Path:
    return image_generation_output_dir() / "references"
_auto_post_lock = threading.Lock()
IMAGEGEN_SKILL_PATH = Path.home() / ".codex" / "skills" / ".system" / "imagegen" / "SKILL.md"
IMAGE_GENERATION_STALL_SECONDS = 75
IMAGE_GENERATION_MAX_SECONDS = 300
DEFAULT_CHARACTER_TRAITS = (
    "半目、眠そうな表情、舌を少し出す、頬杖、黒い短髪、黒いスーツ、"
    "白シャツ、赤ネクタイ、気だるい表情"
)
DEFAULT_CHARACTER_NEGATIVE = "明るい丸目の少年、パーカー、元気な笑顔、指差しポーズ"


def get_db_conn():
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        print("❌ NEON_DATABASE_URL が設定されていません", file=sys.stderr)
        sys.exit(1)
    return psycopg2.connect(url)


def auth_enabled() -> bool:
    return bool(os.environ.get("GOOGLE_OAUTH_CLIENT_ID") and os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET"))


def session_secret() -> str:
    return os.environ.get("SESSION_SECRET") or os.environ.get("APP_ENCRYPTION_KEY") or "x-draft-local-dev-session"


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def sign_session_payload(payload: dict) -> str:
    body = b64url(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(session_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{b64url(sig)}"


def verify_session_token(token: str) -> dict | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = b64url(hmac.new(session_secret().encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return None
    if int(payload.get("exp") or 0) < int(time.time()):
        return None
    return payload if isinstance(payload, dict) else None


def parse_cookies(header: str) -> dict[str, str]:
    cookie = SimpleCookie()
    cookie.load(header or "")
    return {key: morsel.value for key, morsel in cookie.items()}


def ensure_multi_user_schema() -> None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS app_users (
                    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    google_sub TEXT UNIQUE,
                    email TEXT,
                    display_name TEXT,
                    picture_url TEXT,
                    role TEXT NOT NULL DEFAULT 'user',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    user_id UUID NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL DEFAULT '',
                    is_secret BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, key)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS global_logo_presets (
                    logo_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    name TEXT NOT NULL UNIQUE,
                    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
                    image_url TEXT NOT NULL DEFAULT '',
                    image_path TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    usage_note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_logo_presets (
                    logo_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    user_id UUID NOT NULL REFERENCES app_users(user_id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
                    image_url TEXT NOT NULL DEFAULT '',
                    image_path TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    usage_note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE (user_id, name)
                )
            """)
            for table in ("accounts", "drafts"):
                cur.execute(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = %s AND column_name = 'owner_user_id'
                    """,
                    (table,),
                )
                if not cur.fetchone():
                    cur.execute(f"ALTER TABLE {table} ADD COLUMN owner_user_id UUID NULL REFERENCES app_users(user_id) ON DELETE SET NULL")
            conn.commit()
    finally:
        conn.close()


def upsert_app_user(profile: dict) -> dict:
    google_sub = str(profile.get("sub") or "").strip()
    if not google_sub:
        raise ValueError("Google profile に sub がありません")
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO app_users (google_sub, email, display_name, picture_url)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (google_sub) DO UPDATE
                   SET email = EXCLUDED.email,
                       display_name = EXCLUDED.display_name,
                       picture_url = EXCLUDED.picture_url,
                       updated_at = NOW()
                RETURNING user_id, google_sub, email, display_name, picture_url, role
            """, (
                google_sub,
                profile.get("email") or "",
                profile.get("name") or profile.get("email") or "",
                profile.get("picture") or "",
            ))
            user = dict(cur.fetchone())
            conn.commit()
            return {**user, "user_id": str(user["user_id"])}
    finally:
        conn.close()


def get_app_user(user_id: str) -> dict | None:
    if not user_id:
        return None
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, google_sub, email, display_name, picture_url, role
                FROM app_users
                WHERE user_id = %s
            """, (user_id,))
            row = cur.fetchone()
            return {**dict(row), "user_id": str(row["user_id"])} if row else None
    finally:
        conn.close()


def get_user_setting(user_id: str | None, key: str) -> str | None:
    if not user_id:
        return None
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT value FROM user_settings WHERE user_id = %s AND key = %s", (user_id, key))
            row = cur.fetchone()
            return row["value"] if row else None
    finally:
        conn.close()


def save_user_settings(user_id: str, updates: dict[str, str]) -> dict:
    if not user_id:
        raise ValueError("ログインユーザーが必要です")
    invalid_keys = sorted(
        str(key).strip()
        for key in updates
        if not is_editable_env_key(str(key).strip())
    )
    if invalid_keys:
        raise ValueError("保存できない設定キーです: " + ", ".join(invalid_keys))

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            for key, value in updates.items():
                clean_key = str(key).strip()
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", clean_key):
                    continue
                cur.execute("""
                    INSERT INTO user_settings (user_id, key, value, is_secret)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (user_id, key) DO UPDATE
                       SET value = EXCLUDED.value,
                           is_secret = EXCLUDED.is_secret,
                           updated_at = NOW()
                """, (user_id, clean_key, str(value), is_sensitive_env_key(clean_key)))
            conn.commit()
    finally:
        conn.close()
    return {"updated_keys": sorted(str(k).strip() for k in updates)}


def fetch_drafts(account: str = "", *, limit: int = 20, offset: int = 0, query: str = "", image_filter: str = ""):
    limit = max(1, min(int(limit or 20), 100))
    offset = max(0, int(offset or 0))
    image_filter = (image_filter or "").strip().lower()
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where_parts = []
            params: list[str] = []
            if account:
                where_parts.append("lower(a.x_username) = lower(%s)")
                params.append(account)
            if query:
                like = f"%{query.lower()}%"
                where_parts.append("""
                    (
                        lower(COALESCE(a.display_name, '')) LIKE %s
                        OR lower(COALESCE(a.x_username, '')) LIKE %s
                        OR lower(COALESCE(d.memo, '')) LIKE %s
                        OR EXISTS (
                            SELECT 1 FROM draft_parts sp
                            WHERE sp.draft_id = d.draft_id
                              AND lower(COALESCE(sp.content, '')) LIKE %s
                        )
                    )
                """)
                params.extend([like, like, like, like])
            if image_filter == "yes":
                where_parts.append("""
                    EXISTS (
                        SELECT 1
                        FROM draft_parts ip
                        WHERE ip.draft_id = d.draft_id
                          AND COALESCE(ip.image_url, '') <> ''
                    )
                """)
            elif image_filter == "no":
                where_parts.append("""
                    NOT EXISTS (
                        SELECT 1
                        FROM draft_parts ip
                        WHERE ip.draft_id = d.draft_id
                          AND COALESCE(ip.image_url, '') <> ''
                    )
                """)
            where = f"WHERE {' AND '.join(where_parts)}" if where_parts else ""

            cur.execute(f"""
                SELECT COUNT(*) AS total
                FROM drafts d
                JOIN accounts a ON d.account_id = a.account_id
                {where}
            """, tuple(params))
            total = int(cur.fetchone()["total"] or 0)

            cur.execute(f"""
                SELECT d.draft_id, a.x_username, a.display_name,
                       a.profile_image_url,
                       d.status, d.memo, d.created_at, d.published_at,
                       (
                           SELECT COUNT(*)
                           FROM draft_parts p
                           WHERE p.draft_id = d.draft_id
                       ) AS part_count,
                       (
                           SELECT p.content
                           FROM draft_parts p
                           WHERE p.draft_id = d.draft_id
                           ORDER BY p.position
                           LIMIT 1
                       ) AS preview_content,
                       EXISTS (
                           SELECT 1
                           FROM draft_parts p
                           WHERE p.draft_id = d.draft_id
                             AND COALESCE(p.image_url, '') <> ''
                       ) AS has_image
                FROM drafts d
                JOIN accounts a ON d.account_id = a.account_id
                {where}
                ORDER BY d.created_at DESC
                LIMIT %s OFFSET %s
            """, tuple([*params, limit, offset]))
            drafts = cur.fetchall()
            obsidian_saved_index = get_obsidian_saved_draft_index()
            result = []
            for draft in drafts:
                draft_id = str(draft["draft_id"])
                result.append({
                    "draft_id": draft_id,
                    "x_username": draft["x_username"],
                    "display_name": draft["display_name"] or draft["x_username"],
                    "profile_image_url": draft["profile_image_url"] or "",
                    "memo": draft["memo"] or "",
                    "status": draft["status"] or "draft",
                    "has_image": bool(draft["has_image"]),
                    "part_count": int(draft["part_count"] or 0),
                    "preview_content": draft["preview_content"] or "",
                    "created_at": draft["created_at"].strftime("%Y-%m-%d %H:%M"),
                    "published_at": draft["published_at"].strftime("%Y-%m-%d %H:%M") if draft["published_at"] else None,
                    "obsidian_save": obsidian_saved_index.get(draft_id, {"saved": False}),
                })
            return {
                "items": result,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + len(result) < total,
            }
    finally:
        conn.close()


def load_accounts_config() -> list[dict]:
    if not ACCOUNTS_CONFIG_PATH.exists():
        return []
    try:
        payload = json.loads(ACCOUNTS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    accounts = payload.get("accounts") if isinstance(payload, dict) else []
    return accounts if isinstance(accounts, list) else []


def fetch_accounts() -> list[dict]:
    presets: dict[str, dict] = {}
    for item in load_accounts_config():
        if not isinstance(item, dict):
            continue
        username = str(item.get("x_username") or "").strip()
        account_id = str(item.get("id") or username or "").strip()
        if not account_id and not username:
            continue
        key = username.lower() or account_id.lower()
        presets[key] = {
            "id": account_id,
            "x_username": username,
            "display_name": item.get("display_name") or username or account_id,
            "profile_image_url": item.get("profile_image_url") or "",
            "token_env": item.get("token_env") or "",
            "token_secret_env": item.get("token_secret_env") or "",
            "draft_count": 0,
        }

    try:
        conn = get_db_conn()
    except SystemExit:
        return list(presets.values())
    except Exception:
        return list(presets.values())

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT a.account_id, a.x_username, a.display_name, a.profile_image_url,
                       COUNT(d.draft_id) AS draft_count
                FROM accounts a
                LEFT JOIN drafts d ON d.account_id = a.account_id
                GROUP BY a.account_id, a.x_username, a.display_name, a.profile_image_url
                ORDER BY a.x_username
            """)
            for row in cur.fetchall():
                username = row["x_username"] or ""
                key = username.lower() or str(row["account_id"]).lower()
                preset = presets.get(key, {})
                preset.update({
                    "id": preset.get("id") or str(row["account_id"]),
                    "x_username": username,
                    "display_name": row["display_name"] or username,
                    "profile_image_url": row["profile_image_url"] or preset.get("profile_image_url") or "",
                    "draft_count": int(row["draft_count"] or 0),
                    "token_env": preset.get("token_env") or "",
                    "token_secret_env": preset.get("token_secret_env") or "",
                })
                presets[key] = preset
    finally:
        conn.close()

    return sorted(presets.values(), key=lambda item: (item.get("x_username") or item.get("id") or "").lower())


def _load_original_tweet(draft_id: str, seen: set[str] | None = None) -> dict | None:
    """draft-metadata と bookmarks_latest.json から元ツイート（スレッド含む）を返す。"""
    seen = seen or set()
    draft_id = str(draft_id)
    if draft_id in seen:
        return None
    seen.add(draft_id)

    metadata = load_draft_metadata(draft_id)
    if not metadata:
        return None
    tweet_id = str(metadata.get("bookmark_tweet_id") or "")
    author   = metadata.get("author_username") or ""
    if not tweet_id:
        return None

    if tweet_id.startswith("draft-"):
        source_draft_id = tweet_id.removeprefix("draft-")
        source_original = _load_original_tweet(source_draft_id, seen)
        if source_original:
            source_original["rewritten_from_draft_id"] = source_draft_id
            return source_original

    if BOOKMARKS_FILE.exists():
        try:
            data      = json.loads(BOOKMARKS_FILE.read_text(encoding="utf-8"))
            bookmarks = data.get("bookmarks", [])
            for bm in bookmarks:
                if str(bm.get("tweet_id")) == tweet_id:
                    username     = bm.get("author_username") or author
                    raw_parts    = bm.get("thread_parts") or []
                    # 各パーツに author_username を付与してフロントでURL生成できるようにする
                    thread_parts = [
                        {**p, "author_username": username,
                         "tweet_url": f"https://x.com/{username}/status/{p['tweet_id']}"}
                        for p in raw_parts
                    ]
                    text_raw = bm.get("text") or ""
                    raw_urls = re.findall(r'https?://\S+', text_raw)
                    urls = [re.sub(r'[.,);]+$', '', u) for u in raw_urls]
                    return {
                        "tweet_id":        tweet_id,
                        "tweet_url":       f"https://x.com/{username}/status/{tweet_id}",
                        "text":            text_raw,
                        "author_username": username,
                        "author_name":     bm.get("author_name") or "",
                        "thread_parts":    thread_parts,
                        "media":           bm.get("media") or [],
                        "urls":            urls,
                    }
        except Exception:
            pass
    # bookmarks_latest にないときは tweet_id と author だけ返す
    tweet_url = f"https://x.com/{author}/status/{tweet_id}" if author else ""
    return {
        "tweet_id":        tweet_id,
        "tweet_url":       tweet_url,
        "text":            None,
        "author_username": author,
        "author_name":     "",
        "thread_parts":    [],
    }


def fetch_draft(draft_id):
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT d.draft_id, a.x_username, a.display_name,
                       a.profile_image_url,
                       d.status, d.memo, d.created_at, d.published_at
                FROM drafts d
                JOIN accounts a ON d.account_id = a.account_id
                WHERE d.draft_id = %s
            """, (draft_id,))
            draft = cur.fetchone()
            if not draft:
                return None
            cur.execute("""
                SELECT part_id, position, content, image_url
                FROM draft_parts WHERE draft_id = %s ORDER BY position
            """, (draft_id,))
            parts = cur.fetchall()
            metadata      = load_draft_metadata(str(draft["draft_id"])) or {}
            image_prompts = metadata.get("image_prompts") or []
            account_key = draft["x_username"] or str(draft["draft_id"])
            character_setting = merged_character_setting(account_key, draft["profile_image_url"] or "")
            has_image     = any((p["image_url"] or "").strip() for p in parts)
            return {
                "draft_id": str(draft["draft_id"]),
                "x_username": draft["x_username"],
                "display_name": draft["display_name"] or draft["x_username"],
                "profile_image_url": draft["profile_image_url"] or "",
                "memo": draft["memo"] or "",
                "status": draft["status"] or "draft",
                "has_image": has_image,
                "created_at": draft["created_at"].strftime("%Y-%m-%d %H:%M"),
                "published_at": draft["published_at"].strftime("%Y-%m-%d %H:%M") if draft["published_at"] else None,
                "parts": [
                    {
                        "part_id": str(p["part_id"]),
                        "position": p["position"],
                        "content": p["content"],
                        "image_url": p["image_url"],
                    }
                    for p in parts
                ],
                "image_prompts": image_prompts,
                "character_reference": metadata.get("character_reference") or {},
                "character_setting": character_setting,
                "original_tweet": _load_original_tweet(str(draft["draft_id"])),
                "obsidian_save": get_obsidian_save_state(str(draft["draft_id"])),
            }
    finally:
        conn.close()


def delete_draft(draft_id):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM draft_parts WHERE draft_id = %s", (draft_id,))
            cur.execute("DELETE FROM drafts WHERE draft_id = %s", (draft_id,))
            conn.commit()
            return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def add_draft_part(draft_id, content):
    conn = get_db_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT COALESCE(MAX(position), 0) AS max_pos FROM draft_parts WHERE draft_id = %s",
                (draft_id,),
            )
            next_pos = cur.fetchone()["max_pos"] + 1
            cur.execute(
                "INSERT INTO draft_parts (draft_id, position, content) VALUES (%s, %s, %s)",
                (draft_id, next_pos, content),
            )
            conn.commit()
            return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_part_content(part_id, content):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE draft_parts SET content = %s WHERE part_id = %s",
                (content, part_id),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_part_contents(part_updates: list[dict]) -> bool:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            for item in part_updates:
                cur.execute(
                    "UPDATE draft_parts SET content = %s WHERE part_id = %s",
                    (item.get("content") or "", str(item.get("part_id") or "")),
                )
                if cur.rowcount != 1:
                    conn.rollback()
                    return False
            conn.commit()
            return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_draft_image(draft_id, position, image_url):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE draft_parts SET image_url = %s WHERE draft_id = %s AND position = %s",
                (image_url, draft_id, position),
            )
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def update_draft_status(draft_id, status):
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if status == "published":
                cur.execute(
                    "UPDATE drafts SET status = %s, published_at = NOW() WHERE draft_id = %s",
                    (status, draft_id),
                )
            else:
                cur.execute(
                    "UPDATE drafts SET status = %s, published_at = NULL WHERE draft_id = %s",
                    (status, draft_id),
                )
            conn.commit()
            return cur.rowcount > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _git_account_slug() -> str:
    try:
        proc = subprocess.run(
            ["git", "config", "user.name"],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            timeout=3,
            check=False,
        )
        raw = proc.stdout.strip()
    except Exception:
        raw = ""
    normalized = re.sub(r"[^0-9A-Za-z]+", "", raw.lower())
    if normalized:
        return normalized

    personal_root = REPO_ROOT / "personal"
    if personal_root.exists():
        accounts = [p.name for p in personal_root.iterdir() if p.is_dir()]
        if len(accounts) == 1:
            return accounts[0]
    return "deguchishouma"


def get_obsidian_vault_path() -> Path:
    invalid_env_paths: list[Path] = []
    for key in OBSIDIAN_VAULT_ENV_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            candidate = Path(value).expanduser()
            if candidate.exists() and (candidate / ".obsidian").exists():
                return candidate
            invalid_env_paths.append(candidate)

    personal_root = REPO_ROOT / "personal"
    candidates: list[Path] = [
        personal_root / _git_account_slug() / "obsidian" / "claude-obsidian",
        personal_root / "deguchishouma" / "obsidian" / "claude-obsidian",
    ]
    if personal_root.exists():
        candidates.extend(
            account / "obsidian" / "claude-obsidian"
            for account in sorted(personal_root.iterdir())
            if account.is_dir()
        )

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists() and (candidate / ".obsidian").exists():
            return candidate

    return invalid_env_paths[0] if invalid_env_paths else candidates[0]


def get_obsidian_registered_vault_paths() -> list[Path]:
    config_path = Path.home() / "Library" / "Application Support" / "obsidian" / "obsidian.json"
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    paths: list[Path] = []
    seen: set[str] = set()
    for entry in (data.get("vaults") or {}).values():
        raw_path = entry.get("path") if isinstance(entry, dict) else ""
        if not raw_path:
            continue
        candidate = Path(raw_path).expanduser()
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved)
        if key in seen or not candidate.exists():
            continue
        seen.add(key)
        paths.append(candidate)
    return paths


def build_obsidian_open_url(note_path: Path, preferred_vault: Path) -> tuple[str, Path, str]:
    note_resolved = note_path.resolve()
    registered_matches: list[tuple[int, Path, Path]] = []
    for vault_path in get_obsidian_registered_vault_paths():
        try:
            vault_resolved = vault_path.resolve()
            relative = note_resolved.relative_to(vault_resolved)
        except Exception:
            continue
        registered_matches.append((len(vault_resolved.parts), vault_path, relative))

    if registered_matches:
        _, open_vault, relative = sorted(registered_matches, key=lambda item: item[0], reverse=True)[0]
        obsidian_url = "obsidian://open?" + urlencode(
            {"vault": open_vault.name, "file": relative.as_posix()}
        )
        return obsidian_url, open_vault, relative.as_posix()

    try:
        fallback_relative = note_path.relative_to(preferred_vault).as_posix()
    except ValueError:
        fallback_relative = note_path.name
    obsidian_url = "obsidian://open?" + urlencode({"path": str(note_path)})
    return obsidian_url, preferred_vault, fallback_relative


def find_obsidian_note_path_by_draft_id(draft_id: str, vault: Path | None = None) -> Path | None:
    try:
        target_vault = vault or get_obsidian_vault_path()
    except Exception:
        return None
    note_dir = target_vault / "wiki" / "sources" / "x-posts"
    if not note_dir.exists():
        return None

    escaped_id = re.escape(str(draft_id))
    patterns = [
        re.compile(rf'(?m)^draft_id:\s*"{escaped_id}"\s*$'),
        re.compile(rf"(?m)^draft_id:\s*'{escaped_id}'\s*$"),
        re.compile(rf"(?m)^draft_id:\s*{escaped_id}\s*$"),
    ]
    try:
        note_paths = sorted(note_dir.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        note_paths = list(note_dir.glob("*.md"))
    for note_path in note_paths:
        try:
            text = note_path.read_text(encoding="utf-8")
        except Exception:
            continue
        if any(pattern.search(text) for pattern in patterns):
            return note_path
    return None


def _obsidian_note_save_state(note_path: Path, vault: Path) -> dict:
    relative = note_path.relative_to(vault)
    obsidian_url, open_vault, open_relative = build_obsidian_open_url(note_path, vault)
    return {
        "saved": True,
        "path": str(note_path),
        "vault": str(vault),
        "open_vault": str(open_vault),
        "relative_path": relative.as_posix(),
        "open_relative_path": open_relative,
        "obsidian_url": obsidian_url,
    }


def get_obsidian_save_state(draft_id: str) -> dict:
    try:
        vault = get_obsidian_vault_path()
        note_path = find_obsidian_note_path_by_draft_id(draft_id, vault=vault)
        if not note_path:
            return {"saved": False}
        return _obsidian_note_save_state(note_path, vault)
    except Exception as exc:
        return {"saved": False, "error": str(exc)}


def get_obsidian_saved_draft_index() -> dict[str, dict]:
    try:
        vault = get_obsidian_vault_path()
    except Exception:
        return {}
    note_dir = vault / "wiki" / "sources" / "x-posts"
    if not note_dir.exists():
        return {}

    draft_id_pattern = re.compile(r'(?m)^draft_id:\s*["\']?([^"\'\n]+)["\']?\s*$')
    saved: dict[str, dict] = {}
    try:
        note_paths = sorted(note_dir.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    except Exception:
        note_paths = list(note_dir.glob("*.md"))
    for note_path in note_paths:
        try:
            text = note_path.read_text(encoding="utf-8")
        except Exception:
            continue
        match = draft_id_pattern.search(text)
        if not match:
            continue
        draft_id = match.group(1).strip()
        if draft_id and draft_id not in saved:
            try:
                saved[draft_id] = _obsidian_note_save_state(note_path, vault)
            except Exception:
                continue
    return saved


def _yaml_quote(value: str) -> str:
    return '"' + str(value or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _yaml_key_list(key: str, values: list[str]) -> str:
    clean_values = [str(value).strip() for value in values if str(value).strip()]
    if not clean_values:
        return f"{key}: []"
    lines = [f"{key}:"]
    lines.extend(f"  - {_yaml_quote(value)}" for value in clean_values)
    return "\n".join(lines)


def _markdown_list(items: list[str]) -> str:
    clean_items = [str(item).strip() for item in items if str(item).strip()]
    if not clean_items:
        return "- なし"
    return "\n".join(f"- {item}" for item in clean_items)


KNOWLEDGE_CONCEPT_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AIエージェント", ("AIエージェント", "AIエージェント")),
    ("AIリライト", ("AIリライト", "リライト")),
    ("画像生成AI", ("画像生成AI", "画像生成")),
    ("自動投稿", ("自動投稿", "投稿自動化")),
    ("引用投稿", ("引用投稿", "引用リツイート")),
    ("ナレッジグラフ", ("ナレッジグラフ", "knowledge graph")),
    ("開発ツール", ("開発ツール", "コーディングツール")),
    ("買い切りツール", ("買い切り", "買い切りツール")),
    ("サブスクリプション", ("サブスク", "月額課金", "subscription")),
    ("コード理解", ("コードの意味", "コードを読む", "構造を読む", "コード理解")),
    ("ドキュメント", ("README", "ドキュメント", "Code Wiki")),
    ("フォルダ構造", ("フォルダ構造", "フォルダ名")),
    ("Obsidian", ("Obsidian", "obsidian")),
    ("GitHub", ("GitHub", "github")),
    ("ChatGPT", ("ChatGPT", "chatgpt")),
    ("Claude", ("Claude", "claude")),
    ("Codex", ("Codex", "codex")),
    ("OpenClaw", ("OpenClaw", "openclaw")),
    ("Playwright", ("Playwright", "playwright")),
    ("Google Drive", ("Google Drive", "GoogleDrive", "グーグルドライブ")),
    ("1Password", ("1Password", "1password")),
    ("Keychain", ("Keychain", "キーチェーン")),
    ("MCP", ("MCP", "mcp")),
    ("Unity", ("Unity", "unity")),
    ("Blender", ("Blender", "blender")),
    ("BOOTH", ("BOOTH", "booth.pm")),
    ("Synaptic Code", ("Synaptic Code",)),
)

KNOWLEDGE_ENGLISH_STOPWORDS = {
    "Post",
    "Drafts",
    "Reply",
    "Everyone",
    "URL",
    "HTTP",
    "HTTPS",
    "GUI",
    "CTA",
}


def _wikilink(target: str) -> str:
    safe_target = str(target or "").strip().replace("|", "｜")
    return f"[[{safe_target}]]"


def _dedupe_keep_order(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _draft_knowledge_text(draft: dict) -> str:
    values: list[str] = []
    values.append(str(draft.get("memo") or ""))
    for part in draft.get("parts") or []:
        values.append(str(part.get("content") or ""))
        values.append(str(part.get("image_url") or ""))
    original = draft.get("original_tweet") or {}
    values.append(str(original.get("text") or ""))
    values.append(str(original.get("tweet_url") or ""))
    for url in original.get("urls") or []:
        values.append(str(url or ""))
    for part in original.get("thread_parts") or []:
        values.append(str(part.get("text") or ""))
        values.append(str(part.get("tweet_url") or ""))
    for prompt in draft.get("image_prompts") or []:
        values.append(str(prompt.get("copy") or ""))
        values.append(str(prompt.get("prompt") or ""))
    return "\n".join(values)


def _extract_knowledge_graph_nodes(draft: dict) -> dict[str, list[str]]:
    corpus = _draft_knowledge_text(draft)
    original = draft.get("original_tweet") or {}
    account_usernames = {
        str(draft.get("x_username") or "").lower(),
        str(original.get("author_username") or "").lower(),
    }
    account_usernames.discard("")
    concepts: list[str] = []
    for concept, needles in KNOWLEDGE_CONCEPT_PATTERNS:
        if any(needle.lower() in corpus.lower() for needle in needles):
            concepts.append(concept)

    for match in re.finditer(r"\b[A-Za-z][A-Za-z0-9+#.-]*(?:\s+[A-Z][A-Za-z0-9+#.-]*){0,2}\b", corpus):
        term = match.group(0).strip(" .,:;!?()[]{}")
        if len(term) < 3 or term in KNOWLEDGE_ENGLISH_STOPWORDS:
            continue
        if term.lower() in {"https", "http", "com", "www"}:
            continue
        if term.lower() in account_usernames:
            continue
        if any(ch.isupper() for ch in term) or " " in term or any(ch.isdigit() for ch in term):
            concepts.append(term)

    hashtags = [
        f"ハッシュタグ/{match.group(1)}"
        for match in re.finditer(r"#([0-9A-Za-z_ぁ-んァ-ヶ一-龠ー]+)", corpus)
    ]

    domains: list[str] = []
    for raw_url in re.findall(r"https?://[^\s)>\"]+", corpus):
        try:
            hostname = urlparse(raw_url).netloc.lower()
        except Exception:
            hostname = ""
        if hostname:
            domains.append(f"Webドメイン/{hostname.removeprefix('www.')}")

    account_nodes = []
    if draft.get("x_username"):
        account_nodes.append(f"X/@{draft['x_username']}")
    if original.get("author_username"):
        account_nodes.append(f"X/@{original['author_username']}")

    base_nodes = ["X投稿", "投稿テンプレート"]
    concepts = _dedupe_keep_order(concepts)
    concepts = [
        concept
        for concept in concepts
        if not any(
            concept != other and concept.lower() in other.lower()
            for other in concepts
        )
    ][:16]
    domains = _dedupe_keep_order(domains)[:8]
    hashtags = _dedupe_keep_order(hashtags)[:8]
    accounts = _dedupe_keep_order(account_nodes)
    node_targets = _dedupe_keep_order(base_nodes + accounts + concepts + domains + hashtags)
    return {
        "concepts": concepts,
        "domains": domains,
        "hashtags": hashtags,
        "accounts": accounts,
        "node_targets": node_targets,
        "node_links": [_wikilink(target) for target in node_targets],
    }


def _draft_markdown_parts(parts: list[dict]) -> str:
    lines: list[str] = []
    for part in parts:
        position = part.get("position") or len(lines) + 1
        content = str(part.get("content") or "").strip()
        image_url = str(part.get("image_url") or "").strip()
        lines.append(f"### Part {position}\n\n{content or '(空)'}")
        if image_url:
            lines.append(f"\n画像: {image_url}")
    return "\n\n".join(lines) if lines else "(下書き本文なし)"


def _original_markdown(original: dict | None) -> str:
    if not original:
        return "元投稿情報なし"
    lines = [
        f"- author: @{original.get('author_username') or ''}",
        f"- url: {original.get('tweet_url') or ''}",
    ]
    if original.get("rewritten_from_draft_id"):
        lines.append(f"- rewritten_from_draft_id: {original['rewritten_from_draft_id']}")
    thread_parts = original.get("thread_parts") or []
    if thread_parts:
        lines.append("\n### 元投稿スレッド")
        for part in thread_parts:
            text = str(part.get("text") or "").strip()
            url = str(part.get("tweet_url") or "").strip()
            lines.append(f"\n#### {url}\n\n{text or '(本文なし)'}")
    else:
        lines.append("\n### 元投稿本文\n")
        lines.append(str(original.get("text") or "").strip() or "(本文なし)")
    urls = original.get("urls") or []
    if urls:
        lines.append("\n### 外部リンク\n")
        lines.append(_markdown_list(urls))
    return "\n".join(lines)


def save_draft_to_obsidian(draft_id: str) -> dict:
    draft = fetch_draft(draft_id)
    if not draft:
        raise ValueError("下書きが見つかりません")

    vault = get_obsidian_vault_path()
    if not vault.exists():
        raise FileNotFoundError(f"Obsidian vault が見つかりません: {vault}")

    created_date = str(draft.get("created_at") or datetime.now().strftime("%Y-%m-%d")).split(" ")[0]
    first_text = draft.get("parts", [{}])[0].get("content", "")
    title_seed = draft.get("memo") or first_text[:36] or draft_id
    filename = f"{created_date}-x-{slugify(title_seed, str(draft_id))[:64]}.md"
    note_dir = vault / "wiki" / "sources" / "x-posts"
    note_dir.mkdir(parents=True, exist_ok=True)
    note_path = find_obsidian_note_path_by_draft_id(str(draft_id), vault=vault) or note_dir / filename

    original = draft.get("original_tweet") or {}
    source_url = original.get("tweet_url") or ""
    draft_url = f"{PUBLIC_URL}?draft={draft_id}"
    account_link = f"[[X/@{draft.get('x_username') or 'unknown'}]]"
    source_account = original.get("author_username")
    source_link = f"[[X/@{source_account}]]" if source_account else ""
    image_prompts = draft.get("image_prompts") or []
    knowledge_graph = _extract_knowledge_graph_nodes(draft)
    frontmatter_graph = "\n".join(
        [
            _yaml_key_list("related_nodes", knowledge_graph["node_links"]),
            _yaml_key_list("concepts", knowledge_graph["concepts"]),
            _yaml_key_list("domains", knowledge_graph["domains"]),
        ]
    )
    graph_links_line = " ".join(knowledge_graph["node_links"][:18])
    graph_node_list = _markdown_list(knowledge_graph["node_links"])

    body = f"""---
type: x-post-knowledge
source: x-draft-preview
draft_id: {_yaml_quote(str(draft_id))}
x_username: {_yaml_quote(draft.get("x_username") or "")}
status: {_yaml_quote(draft.get("status") or "")}
created_at: {_yaml_quote(draft.get("created_at") or "")}
source_url: {_yaml_quote(source_url)}
{frontmatter_graph}
tags:
  - x-post
  - knowledge-seed
  - x-draft
---

# X投稿ナレッジ: {draft.get("memo") or first_text[:48] or draft_id}

関連: {graph_links_line or f"[[X投稿]] [[投稿テンプレート]] {account_link}{(' ' + source_link) if source_link else ''}"}

## 関連ノード

{graph_node_list}

## なぜ保存したか

- GUIから保存したナレッジ候補。後で「刺さった理由」「使える型」「自分の言葉への変換」を追記する。

## 下書き

- account: @{draft.get("x_username") or ""}
- preview: {draft_url}
- status: {draft.get("status") or ""}

{_draft_markdown_parts(draft.get("parts") or [])}

## 元投稿

{_original_markdown(original)}

## 抽出したい型

- フック:
- 展開:
- 感情:
- CTA:
- 自分の投稿へ転用する時の注意:
"""

    if image_prompts:
        body += "\n## 画像プロンプト\n\n"
        for idx, prompt in enumerate(image_prompts, 1):
            body += f"### Prompt {idx}\n\n"
            if prompt.get("copy"):
                body += f"- copy: {prompt.get('copy')}\n\n"
            body += f"```text\n{prompt.get('prompt') or ''}\n```\n\n"

    if note_path.exists():
        existing = note_path.read_text(encoding="utf-8")
        if f"draft_id: {_yaml_quote(str(draft_id))}" in existing:
            note_path.write_text(body, encoding="utf-8")
        else:
            timestamp = datetime.now().strftime("%H%M%S")
            note_path = note_dir / f"{note_path.stem}-{timestamp}.md"
            note_path.write_text(body, encoding="utf-8")
    else:
        note_path.write_text(body, encoding="utf-8")

    relative = note_path.relative_to(vault)
    obsidian_url, open_vault, open_relative = build_obsidian_open_url(note_path, vault)
    return {
        "path": str(note_path),
        "vault": str(vault),
        "open_vault": str(open_vault),
        "relative_path": relative.as_posix(),
        "open_relative_path": open_relative,
        "obsidian_url": obsidian_url,
    }


def arrange_posting_windows(target_url: str, preview_url: str = "") -> dict:
    clean_target = (target_url or "").strip() or "https://x.com/compose/post"
    parsed = urlparse(clean_target)
    allowed_hosts = {"x.com", "twitter.com", "www.x.com", "www.twitter.com"}
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in allowed_hosts:
        raise ValueError("X のURLだけ開けます")
    if sys.platform != "darwin":
        raise RuntimeError("ウィンドウ自動配置はmacOSのみ対応です")

    preview_needles = [
        (preview_url or "").strip(),
        f"http://localhost:{PORT}",
        f"http://127.0.0.1:{PORT}",
        PUBLIC_URL,
    ]
    preview_needles = [value for value in preview_needles if value]

    script = r'''
on run argv
  set targetUrl to item 1 of argv
  set previewNeedlesRaw to item 2 of argv
  set AppleScript's text item delimiters to "|||"
  set previewNeedles to text items of previewNeedlesRaw
  set AppleScript's text item delimiters to ""

  tell application "Finder"
    set desktopBounds to bounds of window of desktop
  end tell
  set screenLeft to item 1 of desktopBounds
  set screenTop to item 2 of desktopBounds
  set screenRight to item 3 of desktopBounds
  set screenBottom to item 4 of desktopBounds
  set midX to screenLeft + ((screenRight - screenLeft) div 2)

  tell application "Google Chrome"
    activate
    set previewWindow to missing value
    repeat with w in windows
      repeat with t in tabs of w
        set tabUrl to URL of t as text
        repeat with needle in previewNeedles
          set needleText to needle as text
          if needleText is not "" and tabUrl starts with needleText then
            set previewWindow to w
            exit repeat
          end if
        end repeat
        if previewWindow is not missing value then exit repeat
      end repeat
      if previewWindow is not missing value then exit repeat
    end repeat

    set xWindow to make new window
    set URL of active tab of xWindow to targetUrl
    delay 0.8

    if previewWindow is not missing value then
      set bounds of previewWindow to {screenLeft, screenTop, midX, screenBottom}
    end if
    set bounds of xWindow to {midX, screenTop, screenRight, screenBottom}
    return "ok"
  end tell
end run
'''

    subprocess.run(
        ["osascript", "-e", script, clean_target, "|||".join(preview_needles)],
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return {
        "target_url": clean_target,
        "preview_needles": preview_needles,
    }


def update_image_prompt_metadata(draft_id: str, prompt: str, copy_text: str = "") -> dict:
    metadata = load_draft_metadata(str(draft_id)) or {}
    prompts = metadata.get("image_prompts")
    if not isinstance(prompts, list) or not prompts:
        prompts = [{"position": 1, "copy": "", "prompt": "", "file_path": ""}]
    character_reference = metadata.get("character_reference") or {}

    first = prompts[0] if isinstance(prompts[0], dict) else {}
    first["position"] = first.get("position") or 1
    first["copy"] = copy_text
    first["prompt"] = prompt
    if character_reference.get("url") and not first.get("character_reference_url"):
        first["character_reference_url"] = character_reference["url"]

    file_path = first.get("file_path")
    if file_path:
        path = Path(file_path)
        try:
            body = (
                f"# Image Prompt for Draft {draft_id} / Part {first['position']}\n\n"
                f"- copy: {copy_text}\n\n"
                "## Prompt\n\n"
                f"{prompt.strip()}\n"
            )
            path.write_text(body, encoding="utf-8")
        except OSError:
            pass

    prompts[0] = first
    metadata["image_prompts"] = prompts
    save_draft_metadata(str(draft_id), metadata)
    return first


def merged_character_setting(account_key: str, fallback_url: str = "") -> dict:
    setting = load_character_setting(account_key)
    reference_url = (setting.get("reference_url") or fallback_url or "").strip()
    traits = (setting.get("traits") or DEFAULT_CHARACTER_TRAITS).strip()
    negative = (setting.get("negative") or DEFAULT_CHARACTER_NEGATIVE).strip()
    placement = (setting.get("placement") or "右下20〜25%。図解が主役、キャラクターは補助。").strip()
    return {
        "account_key": account_key,
        "reference_url": reference_url,
        "traits": traits,
        "negative": negative,
        "placement": placement,
        "updated_at": setting.get("updated_at") or "",
    }


def update_character_setting(account_key: str, payload: dict) -> dict:
    setting = {
        "account_key": account_key,
        "reference_url": (payload.get("reference_url") or "").strip(),
        "traits": (payload.get("traits") or DEFAULT_CHARACTER_TRAITS).strip(),
        "negative": (payload.get("negative") or DEFAULT_CHARACTER_NEGATIVE).strip(),
        "placement": (payload.get("placement") or "右下20〜25%。図解が主役、キャラクターは補助。").strip(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    save_character_setting(account_key, setting)
    return setting


def is_sensitive_env_key(key: str) -> bool:
    return any(word in key.upper() for word in ("TOKEN", "SECRET", "KEY", "PASSWORD", "COOKIE", "DATABASE_URL"))


def is_editable_env_key(key: str) -> bool:
    return key in ENV_SETTINGS_ALLOWED_EXACT or key.startswith(ENV_SETTINGS_ALLOWED_PREFIXES)


def mask_env_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "********"
    return f"{value[:3]}********{value[-4:]}"


def dotenv_quote(value: str) -> str:
    if value == "":
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def list_env_settings() -> list[dict]:
    values = parse_env_file()
    keys = sorted(key for key in values.keys() if is_editable_env_key(key))
    return [
        {
            "key": key,
            "value": values.get(key, ""),
            "masked": mask_env_value(values.get(key, "")),
            "is_set": bool(values.get(key, "")),
            "sensitive": is_sensitive_env_key(key),
            "source": str(ENV_PATH),
        }
        for key in keys
    ]


def list_settings_for_user(user_id: str | None = None) -> list[dict]:
    env_values = parse_env_file()
    keys = {key for key in env_values.keys() if is_editable_env_key(key)}
    user_values: dict[str, str] = {}
    if user_id:
        conn = get_db_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT key, value FROM user_settings WHERE user_id = %s ORDER BY key", (user_id,))
                for row in cur.fetchall():
                    key = row["key"]
                    if is_editable_env_key(key):
                        user_values[key] = row["value"]
                        keys.add(key)
        finally:
            conn.close()

    return [
        {
            "key": key,
            "value": user_values.get(key, env_values.get(key, "")),
            "masked": mask_env_value(user_values.get(key, env_values.get(key, ""))),
            "is_set": bool(user_values.get(key, env_values.get(key, ""))),
            "sensitive": is_sensitive_env_key(key),
            "source": "user_settings" if key in user_values else str(ENV_PATH),
        }
        for key in sorted(keys)
    ]


def save_env_settings(updates: dict[str, str]) -> dict:
    invalid_keys = sorted(
        str(key).strip()
        for key in updates
        if not is_editable_env_key(str(key).strip())
    )
    if invalid_keys:
        raise ValueError(
            "この画面から編集できるのは X / Neon / DISCORD_WEBHOOK_X_DRAFT の環境変数だけです: "
            + ", ".join(invalid_keys)
        )

    cleaned = {
        str(key).strip(): str(value)
        for key, value in updates.items()
        if (
            re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key).strip())
            and is_editable_env_key(str(key).strip())
        )
    }
    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pattern = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*).*$")
    seen: set[str] = set()
    next_lines: list[str] = []

    for line in lines:
        match = pattern.match(line)
        if not match:
            next_lines.append(line)
            continue
        prefix, key, sep = match.groups()
        if key not in cleaned:
            next_lines.append(line)
            continue
        next_lines.append(f"{prefix}{key}{sep}{dotenv_quote(cleaned[key])}")
        seen.add(key)

    for key in sorted(cleaned.keys() - seen):
        next_lines.append(f"{key}={dotenv_quote(cleaned[key])}")

    ENV_PATH.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
    for key, value in cleaned.items():
        os.environ[key] = value
    return {"path": str(ENV_PATH), "updated_keys": sorted(cleaned.keys())}


def image_prompt_draft_text(draft: dict | None) -> str:
    if not draft:
        return ""
    lines: list[str] = []
    for part in draft.get("parts") or []:
        content = str(part.get("content") or "").strip()
        if not content:
            continue
        position = part.get("position") or len(lines) + 1
        lines.append(f"[Part {position}]\n{content}")
    return "\n\n".join(lines)


def image_prompt_logo_reference_block(logo_references: list[dict]) -> str:
    if not logo_references:
        return "(なし)"
    lines = [
        "以下のロゴは参照画像として登録済みです。画像プロンプト内で該当ロゴを使う場合は、記憶や推測ではなく参照画像の形・比率・色を優先する指示を入れてください。"
    ]
    for logo in logo_references:
        aliases = ", ".join(logo.get("matched_aliases") or [])
        image_url = logo.get("image_url") or ""
        image_path = logo.get("image_path") or ""
        source_url = logo.get("source_url") or ""
        lines.append(
            f"- {logo.get('name')}: aliases={aliases or '(なし)'}, image_url={image_url or '(なし)'}, image_path={image_path or '(なし)'}, source={source_url or '(なし)'}"
        )
    return "\n".join(lines)


def rewrite_image_prompt_with_codex(
    *,
    draft_id: str,
    current_prompt: str,
    instruction: str,
    copy_text: str,
    character_reference_url: str = "",
    mode: str = "rewrite",
    user_id: str | None = None,
    selected_logo_names: list[str] | None = None,
    status_callback=None,
    should_stop=None,
    process_callback=None,
) -> str:
    if mode != "regenerate":
        raise RuntimeError("画像プロンプトのAIリライトは無効です。修正依頼または画像プロンプト再生成を使ってください。")
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError("codex コマンドが見つかりません")

    mode = "regenerate"
    draft = fetch_draft(str(draft_id)) or {}
    draft_text = image_prompt_draft_text(draft)
    logo_references = detect_logo_references(
        draft_text,
        copy_text,
        current_prompt,
        user_id=user_id,
        selected_names=selected_logo_names,
    )
    task = (
        "投稿本文・画像コピー・参照キャラクター・登録済みロゴをもとに、画像プロンプトをゼロから再生成してください。"
    )
    default_instruction = (
        "既存案に引っ張られず、投稿本文の要点がスマホで読みやすい1枚の縦型図解になるよう再構成する"
    )

    prompt = f"""
あなたはX投稿用の縦型インフォグラフィック画像プロンプト編集者です。
APIや画像生成APIの話は一切しないでください。
{task}
出力は最終的な画像プロンプト本文のみ。説明、Markdown、コードブロックは禁止。

draft_id: {draft_id}
アカウント: {draft.get("display_name") or ""} @{draft.get("x_username") or ""}
画像コピー: {copy_text}
キャラクター参照画像URL: {character_reference_url or "なし"}

必須:
- 3:4縦型のX向け図解として、スマホで読める余白・文字量・階層にする。
- 画像内テキストは日本語で、見出しと3〜5個の短い要点に絞る。
- キャラクター参照画像URLがある場合は、画像プロンプト内に「このプロフィール画像をキャラクター参照として使う」と明記する。
- 参照キャラクターの髪型・表情・服装・雰囲気を保つ。
- キャラクターは図解の右下などに20〜25%以内で入れ、主役は図解にする。
- 画像生成APIやAPIキーの説明は絶対に入れない。

[投稿本文]
{draft_text or "(なし)"}

[現在の画像プロンプト]
{current_prompt or "(なし)"}

[登録済みロゴ参照]
{image_prompt_logo_reference_block(logo_references)}

[ユーザーの追加指示]
{instruction or default_instruction}
""".strip()

    repo_root = Path(__file__).resolve().parents[1]
    return run_codex_app_server_turn(
        codex_path,
        repo_root,
        prompt,
        status_callback=status_callback,
        should_stop=should_stop,
        process_callback=process_callback,
    )


def _normalize_x_handle(value: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", (value or "").lower().lstrip("@"))


def find_account_info_file(x_username: str) -> Path | None:
    wanted = _normalize_x_handle(x_username)
    if not wanted or not ACCOUNT_INFO_DIR.exists():
        return None
    md_files = sorted(ACCOUNT_INFO_DIR.rglob("*.md"))
    for path in md_files:
        if _normalize_x_handle(path.stem) == wanted:
            return path
    handle_pattern = re.compile(rf"@{re.escape(wanted)}\b", re.IGNORECASE)
    for path in md_files:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if handle_pattern.search(text):
            return path
    return None


def append_rewrite_preference_to_account(draft_id: str, instruction: str) -> Path:
    draft = fetch_draft(draft_id)
    if not draft:
        raise RuntimeError("下書きが見つかりません")
    account_path = find_account_info_file(draft.get("x_username") or "")
    if not account_path:
        raise RuntimeError(f"@{draft.get('x_username') or 'unknown'} のアカウント情報ファイルが見つかりません")

    cleaned_instruction = re.sub(r"\s+", " ", (instruction or "").strip())
    if not cleaned_instruction:
        cleaned_instruction = "画像プロンプトは、スマホ表示で読みやすく、文字量を抑え、結論と視認性を優先する。"
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    section = "## 画像プロンプト再生成時の継続注意"
    entry = f"- {now} / draft_id: {draft_id} / 今後も画像プロンプト再生成では「{cleaned_instruction}」を優先する。"

    text = account_path.read_text(encoding="utf-8") if account_path.exists() else ""
    if section not in text:
        text = text.rstrip() + f"\n\n{section}\n\n{entry}\n"
    else:
        text = text.rstrip() + f"\n{entry}\n"
    account_path.write_text(text, encoding="utf-8")
    return account_path


def append_text_rewrite_rule_to_account(draft_id: str, instruction: str, scope: str = "part") -> Path:
    draft = fetch_draft(draft_id)
    if not draft:
        raise RuntimeError("下書きが見つかりません")
    account_path = find_account_info_file(draft.get("x_username") or "")
    if not account_path:
        raise RuntimeError(f"@{draft.get('x_username') or 'unknown'} のアカウント情報ファイルが見つかりません")

    cleaned_instruction = re.sub(r"\s+", " ", (instruction or "").strip())
    if not cleaned_instruction:
        cleaned_instruction = "投稿本文は、読みやすさと自然な口調を優先して整える。"
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    section = "## 投稿本文AI修正時の継続ルール"
    scope_label = "下書き全文" if scope == "draft" else "個別投稿"
    entry = (
        f"- {now} / draft_id: {draft_id} / 今後の投稿本文AI修正（{scope_label}）では"
        f"「{cleaned_instruction}」を優先する。画像プロンプトは変更しない。"
    )

    text = account_path.read_text(encoding="utf-8") if account_path.exists() else ""
    if section not in text:
        text = text.rstrip() + f"\n\n{section}\n\n{entry}\n"
    else:
        text = text.rstrip() + f"\n{entry}\n"
    account_path.write_text(text, encoding="utf-8")
    return account_path


def account_rules_for_draft(draft: dict) -> str:
    account_path = find_account_info_file(draft.get("x_username") or "")
    if not account_path or not account_path.exists():
        return ""
    try:
        return account_path.read_text(encoding="utf-8")[:16000]
    except OSError:
        return ""


def _clean_text_rewrite_output(value: str) -> str:
    text = (value or "").strip()
    text = re.sub(r"^修正後本文\s*[:：]\s*", "", text)
    text = re.sub(r"^本文\s*[:：]\s*", "", text)
    text = text.strip().strip("\"'")
    return text.strip()


def _parse_text_rewrite_json(value: str) -> dict:
    text = (value or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError("全文AI修正の結果をJSONとして読めませんでした")
        try:
            parsed = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("全文AI修正の結果をJSONとして読めませんでした") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("全文AI修正の結果形式が不正です")
    return parsed


def rewrite_draft_text_with_codex(draft_id: str, part_id: str, instruction: str) -> str:
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError("codex コマンドが見つかりません")

    draft = fetch_draft(str(draft_id))
    if not draft:
        raise RuntimeError("下書きが見つかりません")

    target_part = None
    for part in draft.get("parts") or []:
        if str(part.get("part_id")) == str(part_id):
            target_part = part
            break
    if not target_part:
        raise RuntimeError("対象の本文パーツが見つかりません")

    current_text = str(target_part.get("content") or "")
    if not current_text.strip():
        raise RuntimeError("対象の本文が空です")

    account_rules = account_rules_for_draft(draft)

    prompt = f"""
あなたはX投稿本文の編集者です。
対象パートの本文だけを、ユーザーの修正指示とアカウントルールに沿って改善してください。
画像プロンプト、画像生成指示、画像URL、メタデータ、他パート本文は絶対に変更しません。
出力は修正後の対象パート本文のみ。説明、見出し、Markdown、コードブロックは禁止。

アカウント: {draft.get("display_name") or ""} @{draft.get("x_username") or ""}
draft_id: {draft_id}
part_id: {part_id}
position: {target_part.get("position") or ""}

[アカウントルール]
{account_rules or "(アカウント情報なし)"}

[下書き全体]
{_draft_markdown_parts(draft.get("parts") or [])}

[修正対象の現在本文]
{current_text}

[ユーザーの修正指示]
{instruction.strip()}

必須条件:
- 修正対象の本文だけを返す。
- 投稿の意味、事実、固有名詞、URL、メンション、ハッシュタグは必要なく変更しない。
- 絵文字や過度な装飾は、元の本文にない限り追加しない。
- 画像プロンプトや画像の説明は出力しない。
""".strip()

    repo_root = Path(__file__).resolve().parents[1]
    rewritten = run_codex_app_server_turn(
        codex_path,
        repo_root,
        prompt,
        timeout=240,
        developer_instructions=(
            "あなたはX投稿本文の編集だけを行う。ファイル編集、外部送信、"
            "コマンド実行、画像プロンプト変更、画像生成APIの提案は禁止。"
        ),
        sandbox_policy={"type": "readOnly"},
    )
    cleaned = _clean_text_rewrite_output(rewritten)
    if not cleaned:
        raise RuntimeError("本文AI修正の結果が空です")
    return cleaned


def rewrite_draft_all_text_with_codex(draft_id: str, instruction: str) -> list[dict]:
    codex_path = shutil.which("codex")
    if not codex_path:
        raise RuntimeError("codex コマンドが見つかりません")

    draft = fetch_draft(str(draft_id))
    if not draft:
        raise RuntimeError("下書きが見つかりません")

    parts = draft.get("parts") or []
    if not parts:
        raise RuntimeError("修正できる本文がありません")

    account_rules = account_rules_for_draft(draft)
    parts_payload = json.dumps(
        [
            {
                "part_id": str(part.get("part_id") or ""),
                "position": part.get("position") or index + 1,
                "content": str(part.get("content") or ""),
            }
            for index, part in enumerate(parts)
        ],
        ensure_ascii=False,
        indent=2,
    )

    prompt = f"""
あなたはX投稿本文の編集者です。
下書き全体の流れを見ながら、全投稿パートの本文だけをユーザーの修正指示とアカウントルールに沿って改善してください。
画像プロンプト、画像生成指示、画像URL、メタデータは絶対に変更しません。
出力はJSONのみ。説明、Markdown、コードブロックは禁止。

JSON形式:
{{"parts":[{{"part_id":"元のpart_id","position":元のposition,"content":"修正後本文"}}]}}

アカウント: {draft.get("display_name") or ""} @{draft.get("x_username") or ""}
draft_id: {draft_id}

[アカウントルール]
{account_rules or "(アカウント情報なし)"}

[修正対象パーツ]
{parts_payload}

[ユーザーの修正指示]
{instruction.strip()}

必須条件:
- parts の件数、part_id、position、順番は元と同じにする。
- content だけを修正する。
- 全体の論理の流れ、重複、口調、読みやすさを見て整える。
- 事実、固有名詞、URL、メンション、ハッシュタグは必要なく変更しない。
- 絵文字や過度な装飾は、元の本文にない限り追加しない。
- 画像プロンプトや画像の説明は出力しない。
""".strip()

    repo_root = Path(__file__).resolve().parents[1]
    result_text = run_codex_app_server_turn(
        codex_path,
        repo_root,
        prompt,
        timeout=300,
        developer_instructions=(
            "あなたはX投稿本文の編集だけを行い、必ず指定JSONだけを返す。"
            "ファイル編集、外部送信、コマンド実行、画像プロンプト変更、画像生成APIの提案は禁止。"
        ),
        sandbox_policy={"type": "readOnly"},
    )
    payload = _parse_text_rewrite_json(result_text)
    rewritten_parts = payload.get("parts")
    if not isinstance(rewritten_parts, list):
        raise RuntimeError("全文AI修正の結果に parts がありません")

    original_ids = [str(part.get("part_id") or "") for part in parts]
    rewritten_by_id: dict[str, dict] = {}
    for item in rewritten_parts:
        if not isinstance(item, dict):
            raise RuntimeError("全文AI修正の parts 形式が不正です")
        part_id = str(item.get("part_id") or "")
        content = item.get("content")
        if part_id not in original_ids or content is None:
            raise RuntimeError("全文AI修正の part_id または content が不正です")
        rewritten_by_id[part_id] = {
            "part_id": part_id,
            "position": item.get("position"),
            "content": str(content).strip(),
        }

    if set(rewritten_by_id.keys()) != set(original_ids):
        raise RuntimeError("全文AI修正の結果パーツ数が元の下書きと一致しません")

    ordered_parts: list[dict] = []
    for part in parts:
        part_id = str(part.get("part_id") or "")
        rewritten = rewritten_by_id[part_id]
        ordered_parts.append({
            "part_id": part_id,
            "position": part.get("position") or rewritten.get("position"),
            "content": rewritten["content"],
        })
    return ordered_parts


def run_codex_app_server_turn(
    codex_path: str,
    cwd: Path,
    prompt: str,
    timeout: int = 300,
    *,
    developer_instructions: str | None = None,
    sandbox_policy: dict | None = None,
    input_items: list[dict] | None = None,
    status_callback=None,
    should_stop=None,
    process_callback=None,
    allow_empty_result: bool = False,
    wait_for_turn_completed: bool = False,
) -> str:
    """Codex App Server の JSONL プロトコルで1ターン実行し、最終テキストを返す。"""
    proc = subprocess.Popen(
        [
            codex_path,
            "app-server",
            "--listen",
            "stdio://",
        ],
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if process_callback:
        process_callback(proc)
    assert proc.stdin is not None
    assert proc.stdout is not None

    stderr_lines: queue.Queue[str] = queue.Queue()

    def read_stderr():
        if proc.stderr is None:
            return
        for line in proc.stderr:
            stderr_lines.put(line.rstrip())

    threading.Thread(target=read_stderr, daemon=True).start()

    next_id = 0

    def send(method: str, params: dict | None = None, *, request: bool = True) -> int | None:
        nonlocal next_id
        message = {"method": method, "params": params or {}}
        request_id = None
        if request:
            request_id = next_id
            next_id += 1
            message["id"] = request_id
        proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        return request_id

    def raise_if_error(msg: dict):
        if "error" in msg:
            error = msg.get("error") or {}
            raise RuntimeError(str(error.get("message") or error)[:1000])

    try:
        initialize_id = send(
            "initialize",
            {
                "clientInfo": {
                    "name": "team_info_x_post_preview",
                    "title": "X Post Writer Preview",
                    "version": "0.1.0",
                }
            },
        )
        send("initialized", request=False)
        sandbox_type = (sandbox_policy or {}).get("type") if isinstance(sandbox_policy, dict) else None
        thread_sandbox = {
            "readOnly": "read-only",
            "workspaceWrite": "workspace-write",
            "dangerFullAccess": "danger-full-access",
        }.get(sandbox_type or "readOnly", "read-only")
        thread_id_request = send(
            "thread/start",
            {
                "cwd": str(cwd),
                "sandbox": thread_sandbox,
                "approvalPolicy": "never",
                "developerInstructions": (
                    developer_instructions
                    or "あなたは短いテキスト変換だけを行う。ファイル編集、外部送信、"
                    "コマンド実行、画像生成APIの提案は禁止。"
                ),
            },
        )

        deadline = time.monotonic() + timeout
        thread_id = None
        turn_started = False
        chunks: list[str] = []
        completed_texts: list[str] = []
        completed_text_at: float | None = None

        while time.monotonic() < deadline:
            if should_stop and should_stop():
                return ""
            ready, _, _ = select.select([proc.stdout], [], [], 0.2)
            if not ready:
                if (
                    not wait_for_turn_completed
                    and completed_texts
                    and completed_text_at
                    and time.monotonic() - completed_text_at >= 1.5
                ):
                    break
                if proc.poll() is not None:
                    break
                continue
            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    break
                time.sleep(0.05)
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            raise_if_error(msg)

            if msg.get("id") == initialize_id:
                continue

            if msg.get("id") == thread_id_request:
                thread = (msg.get("result") or {}).get("thread") or {}
                thread_id = thread.get("id")
                if not thread_id:
                    raise RuntimeError("Codex App Server から thread_id が返りませんでした")
                send(
                    "turn/start",
                    {
                        "threadId": thread_id,
                        "input": input_items or [{"type": "text", "text": prompt}],
                        "cwd": str(cwd),
                        "sandboxPolicy": sandbox_policy or {"type": "readOnly"},
                    },
                )
                turn_started = True
                continue

            method = msg.get("method")
            params = msg.get("params") or {}
            if method == "item/agentMessage/delta":
                chunks.append(params.get("delta") or "")
                if status_callback:
                    status_callback(method, params)
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    completed_texts.append(item["text"])
                    completed_text_at = time.monotonic()
                if status_callback:
                    status_callback(method, params)
            elif method == "turn/completed" and turn_started:
                if status_callback:
                    status_callback(method, params)
                break
            elif method == "thread/status/changed" and completed_texts and not wait_for_turn_completed:
                if status_callback:
                    status_callback(method, params)
                break
            elif status_callback and method:
                status_callback(method, params)

        else:
            raise RuntimeError("Codex App Server の応答がタイムアウトしました")

        if proc.poll() is not None and not chunks and not completed_texts:
            errors = "\n".join(list(stderr_lines.queue)[-10:])
            raise RuntimeError((errors or "Codex App Server が終了しました")[:1000])

    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if process_callback:
            process_callback(None)

    rewritten = ("".join(chunks).strip() or "\n".join(completed_texts).strip())
    if rewritten.startswith("```"):
        rewritten = re.sub(r"^```(?:text)?\s*", "", rewritten)
        rewritten = re.sub(r"\s*```$", "", rewritten)
    if not rewritten:
        if allow_empty_result:
            return ""
        errors = "\n".join(list(stderr_lines.queue)[-10:])
        raise RuntimeError((errors or "Codex App Server からリライト結果が返りませんでした")[:1000])
    return rewritten


def attach_generated_image(draft_id: str, image_path_text: str) -> str:
    image_path = resolve_local_image(image_path_text)
    if not image_path:
        raise ValueError("画像が見つからないか、許可されていないパスです")
    image_url = f"/local-image?path={str(image_path)}"
    if not update_draft_image(draft_id, 1, image_url):
        raise ValueError("画像を添付する投稿パーツが見つかりません")
    return image_url


def build_image_generation_request(
    copy_text: str,
    prompt: str,
    character_reference_url: str = "",
    character_traits: str = "",
    character_negative: str = "",
    character_placement: str = "",
    feedback: str = "",
    current_image_url: str = "",
    reference_image_url: str = "",
    reference_image_path: str = "",
    logo_references: list[dict] | None = None,
) -> str:
    traits = (character_traits or DEFAULT_CHARACTER_TRAITS).strip()
    negative = (character_negative or DEFAULT_CHARACTER_NEGATIVE).strip()
    placement = (character_placement or "右下20〜25%。図解が主役、キャラクターは補助。").strip()
    lines = [
        "Codex/ChatGPTサブスク内の画像生成で、次のX投稿用図解を1枚生成してください。",
        "APIは使わないでください。",
        "添付されたプロフィール画像をキャラクター参照として使い、同じキャラクターの外見を図解内に入れてください。",
        f"参照キャラクターの重要特徴: {traits}",
        f"禁止する見た目: {negative}",
        f"キャラクター配置: {placement}",
        "",
        f"[CHARACTER REFERENCE]\n{character_reference_url or '(なし)'}",
        "",
        f"[COPY]\n{copy_text or '(なし)'}",
        "",
        f"[IMAGE PROMPT]\n{prompt}",
    ]
    if logo_references:
        lines.extend([
            "",
            "[LOGO REFERENCE IMAGES]",
            "以下のロゴは参照画像として添付済みです。ロゴを描く必要がある場合は、記憶や推測ではなく添付された参照画像の形・比率・色を優先してください。",
        ])
        for logo in logo_references:
            aliases = ", ".join(logo.get("matched_aliases") or [])
            lines.append(f"- {logo.get('name')}: {aliases}")
    if feedback or current_image_url or reference_image_url or reference_image_path:
        lines.extend([
            "",
            "[REGENERATION CONTEXT]",
            f"現在の生成画像: {current_image_url or '(なし)'}",
            f"参照画像URL: {reference_image_url or '(なし)'}",
            f"参照画像ローカルパス: {reference_image_path or '(なし)'}",
            "",
            f"[USER FEEDBACK]\n{feedback or '(なし)'}",
            "",
            "現在の生成画像の良い部分は保ちつつ、ユーザーのフィードバックと参照画像を優先して再生成してください。",
            "ロゴやマークなど参照画像がある要素は、形状・比率・色・余白を参照画像に寄せてください。",
        ])
    return "\n".join(lines)


def seed_global_logo_presets() -> None:
    if not LOGO_PRESETS_PATH.exists():
        return
    try:
        payload = json.loads(LOGO_PRESETS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    logos = payload.get("logos") if isinstance(payload, dict) else []
    if not isinstance(logos, list):
        return
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            for logo in logos:
                if not isinstance(logo, dict) or not logo.get("name"):
                    continue
                cur.execute("""
                    INSERT INTO global_logo_presets (name, aliases, image_url, image_path, source_url, usage_note)
                    VALUES (%s, %s::jsonb, %s, %s, %s, %s)
                    ON CONFLICT (name) DO UPDATE
                       SET aliases = EXCLUDED.aliases,
                           image_url = EXCLUDED.image_url,
                           image_path = EXCLUDED.image_path,
                           source_url = EXCLUDED.source_url,
                           usage_note = EXCLUDED.usage_note,
                           updated_at = NOW()
                """, (
                    logo.get("name") or "",
                    json.dumps(logo.get("aliases") or [], ensure_ascii=False),
                    logo.get("image_url") or "",
                    logo.get("image_path") or "",
                    logo.get("source_url") or "",
                    logo.get("usage_note") or "",
                ))
            conn.commit()
    finally:
        conn.close()


def load_logo_presets(user_id: str | None = None) -> list[dict]:
    presets: list[dict] = []
    try:
        conn = get_db_conn()
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT name, aliases, image_url, image_path, source_url, usage_note
                FROM global_logo_presets
                ORDER BY name
            """)
            presets.extend(dict(row) for row in cur.fetchall())
            if user_id:
                cur.execute("""
                    SELECT name, aliases, image_url, image_path, source_url, usage_note
                    FROM user_logo_presets
                    WHERE user_id = %s
                    ORDER BY name
                """, (user_id,))
                presets.extend(dict(row) for row in cur.fetchall())
        conn.close()
    except Exception:
        presets = []
    for path in [LOGO_PRESETS_PATH, get_runtime_root() / "logo_presets.json"]:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        items = payload.get("logos") if isinstance(payload, dict) else payload
        if isinstance(items, list):
            presets.extend(item for item in items if isinstance(item, dict))
    return presets


def _normalize_logo_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").lower())


def _normalize_logo_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def detect_logo_references(*texts: str, user_id: str | None = None, selected_names: list[str] | None = None) -> list[dict]:
    haystack = _normalize_logo_text("\n".join(texts))
    detected: list[dict] = []
    seen: set[str] = set()
    selected_set = {_normalize_logo_name(name) for name in (selected_names or []) if str(name).strip()}
    for preset in load_logo_presets(user_id):
        name = str(preset.get("name") or "").strip()
        if selected_names is not None and _normalize_logo_name(name) not in selected_set:
            continue
        aliases = [name, *preset.get("aliases", [])]
        matched = [
            str(alias).strip()
            for alias in aliases
            if str(alias).strip() and _normalize_logo_text(str(alias)) in haystack
        ]
        if selected_names is not None and _normalize_logo_name(name) in selected_set and not matched:
            matched = [name]
        if not name or not matched:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        detected.append({**preset, "matched_aliases": matched})
    return detected[:6]


LOGO_CANDIDATE_STOPWORDS = {
    "ai", "api", "url", "x", "sns", "png", "jpg", "jpeg", "webp", "svg", "ui", "ux",
    "react", "javascript", "typescript", "html", "css", "json", "github", "openai",
    "chatgpt", "claude", "anthropic", "codex", "youtube", "google", "web", "ec",
}


def extract_logo_candidate_names(*texts: str, user_id: str | None = None) -> list[dict]:
    text = "\n".join(texts)
    presets = load_logo_presets(user_id)
    registered_by_key = {_normalize_logo_name(str(item.get("name") or "")): item for item in presets}
    candidates: dict[str, dict] = {}

    for logo in detect_logo_references(text, user_id=user_id):
        key = _normalize_logo_name(str(logo.get("name") or ""))
        if key:
            candidates[key] = {
                "name": logo.get("name") or "",
                "registered": True,
                "image_url": logo.get("image_url") or "",
                "image_path": logo.get("image_path") or "",
                "source_url": logo.get("source_url") or "",
                "matched_aliases": logo.get("matched_aliases") or [],
                "reason": "登録済み辞書に一致",
            }

    for url in re.findall(r"https?://[^\s)）\]】\"']+", text):
        parsed = urlparse(url.rstrip(".,、。"))
        host = parsed.netloc.lower().removeprefix("www.")
        if not host or any(bad in host for bad in ("x.com", "twitter.com", "youtube.com", "google.com")):
            continue
        label = host.split(".")[0]
        if not label or label in LOGO_CANDIDATE_STOPWORDS:
            continue
        name = next((part for part in re.split(r"[-_]", label) if len(part) >= 3), label)
        display_name = name[:1].upper() + name[1:]
        key = _normalize_logo_name(display_name)
        if key and key not in candidates:
            registered = registered_by_key.get(key)
            candidates[key] = {
                "name": registered.get("name") if registered else display_name,
                "registered": bool(registered),
                "image_url": (registered or {}).get("image_url") or "",
                "image_path": (registered or {}).get("image_path") or "",
                "source_url": f"{parsed.scheme}://{parsed.netloc}",
                "matched_aliases": [host],
                "reason": "本文・プロンプト内URLのドメイン",
            }

    patterns = [
        r"\b[A-Z][A-Za-z0-9]{2,}(?:[A-Z][A-Za-z0-9]+)+\b",
        r"\b[A-Z][A-Za-z0-9]{2,}(?:[.\- ][A-Z][A-Za-z0-9]{2,})*\b",
        r"\b[A-Z][a-z0-9]{2,}(?:[A-Z][a-z0-9]+)+\b",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            raw = match.group(0).strip(" .,:;()[]{}「」『』（）")
            if not raw or len(raw) > 40:
                continue
            key = _normalize_logo_name(raw)
            if len(key) < 3 or key in LOGO_CANDIDATE_STOPWORDS:
                continue
            if key in candidates:
                candidates[key].setdefault("matched_aliases", []).append(raw)
                continue
            registered = registered_by_key.get(key)
            candidates[key] = {
                "name": registered.get("name") if registered else raw,
                "registered": bool(registered),
                "image_url": (registered or {}).get("image_url") or "",
                "image_path": (registered or {}).get("image_path") or "",
                "source_url": (registered or {}).get("source_url") or "",
                "matched_aliases": [raw],
                "reason": "本文・画像プロンプト内のサービス名らしい英字表記",
            }
    return sorted(candidates.values(), key=lambda item: (not item.get("registered"), str(item.get("name") or "").lower()))[:12]


def guess_official_site(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9 ]+", " ", name).strip()
    if not clean:
        return ""
    query = urlencode({"q": f"{clean} official website"})
    search_url = f"https://duckduckgo.com/html/?{query}"
    req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        html_text = resp.read(800_000).decode("utf-8", errors="ignore")
    bad_hosts = ("duckduckgo.com", "twitter.com", "x.com", "facebook.com", "linkedin.com", "youtube.com", "wikipedia.org")
    name_key = _normalize_logo_name(clean)
    links: list[str] = []
    for raw in re.findall(r'href="([^"]+)"', html_text):
        url = html.unescape(raw)
        if "uddg=" in url:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            url = unquote(params.get("uddg", [""])[0] or url)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        host = parsed.netloc.lower().removeprefix("www.")
        if any(bad in host for bad in bad_hosts):
            continue
        links.append(url)
        if name_key and name_key in _normalize_logo_name(host):
            return f"{parsed.scheme}://{parsed.netloc}"
    if links:
        parsed = urlparse(links[0])
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def absolute_url(base_url: str, href: str) -> str:
    return urljoin(base_url, href)


def find_logo_image_url(source_url: str) -> str:
    parsed_source = urlparse(source_url)
    if parsed_source.scheme not in {"http", "https"} or not parsed_source.netloc:
        return ""
    homepage = f"{parsed_source.scheme}://{parsed_source.netloc}"
    req = urllib.request.Request(homepage, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=12) as resp:
        page = resp.read(900_000).decode("utf-8", errors="ignore")

    candidates: list[tuple[int, str]] = []
    for tag in re.findall(r"<link\b[^>]*>", page, flags=re.IGNORECASE):
        rel_match = re.search(r'\brel=["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        href_match = re.search(r'\bhref=["\']([^"\']+)["\']', tag, flags=re.IGNORECASE)
        if not rel_match or not href_match:
            continue
        rel = rel_match.group(1).lower()
        href = html.unescape(href_match.group(1))
        if "apple-touch-icon" in rel:
            candidates.append((100, absolute_url(homepage, href)))
        elif "mask-icon" in rel:
            candidates.append((80, absolute_url(homepage, href)))
        elif "icon" in rel:
            score = 70
            size_match = re.search(r'\bsizes=["\'](\d+)x(\d+)["\']', tag, flags=re.IGNORECASE)
            if size_match:
                score += min(40, int(size_match.group(1)) // 16)
            candidates.append((score, absolute_url(homepage, href)))

    for pattern, score in [
        (r'<meta\b[^>]*(?:property|name)=["\']og:image["\'][^>]*content=["\']([^"\']+)["\'][^>]*>', 60),
        (r'<meta\b[^>]*content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']og:image["\'][^>]*>', 60),
    ]:
        for match in re.finditer(pattern, page, flags=re.IGNORECASE):
            candidates.append((score, absolute_url(homepage, html.unescape(match.group(1)))))

    candidates.append((30, absolute_url(homepage, "/favicon.ico")))
    for _, url in sorted(candidates, key=lambda item: item[0], reverse=True):
        if urlparse(url).scheme in {"http", "https"}:
            return url
    return ""


def download_logo_image(name: str, image_url: str) -> Path:
    cache_dir = logo_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(image_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read(3_000_000)
        content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if not body:
        raise RuntimeError("ロゴ画像の取得結果が空でした")
    ext = mimetypes.guess_extension(content_type) or Path(urlparse(image_url).path).suffix or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    if ext.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg"}:
        ext = ".png"
    safe_name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "logo"
    dest = cache_dir / f"{safe_name}{ext.lower()}"
    dest.write_bytes(body)
    return dest


def normalize_local_image_path_text(path_text: str) -> Path:
    text = str(path_text or "").strip()
    parsed = urlparse(text)
    if text.startswith("file://"):
        text = unquote(parsed.path or "")
    elif parsed.path == "/local-image" or text.startswith("/local-image"):
        qs = parse_qs(parsed.query)
        local_path = qs.get("path", [""])[0]
        if local_path:
            text = unquote(local_path)
    return Path(text).expanduser()


def save_uploaded_reference_image(filename: str, content_type: str, body: bytes) -> dict:
    if not body:
        raise ValueError("画像ファイルが空です")
    if len(body) > 12 * 1024 * 1024:
        raise ValueError("参照画像は12MB以下にしてください")

    clean_name = Path(filename or "reference.png").name
    ext = Path(clean_name).suffix.lower()
    guessed_ext = mimetypes.guess_extension((content_type or "").split(";")[0].strip().lower()) or ""
    allowed_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}
    if ext not in allowed_exts:
        ext = guessed_ext if guessed_ext in allowed_exts else ".png"
    if content_type and not content_type.lower().startswith("image/"):
        raise ValueError("画像ファイルだけアップロードできます")

    dest_dir = reference_image_upload_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{timestamp}-{secrets.token_hex(6)}{ext}"
    dest.write_bytes(body)
    return {
        "local_path": str(dest),
        "url": f"/local-image?path={str(dest)}",
    }


def parse_reference_image_upload(content_type: str, raw_body: bytes) -> dict:
    match = re.search(r'boundary=(?:"([^"]+)"|([^;]+))', content_type or "")
    if not match:
        raise ValueError("multipart boundary が見つかりません")
    boundary = (match.group(1) or match.group(2) or "").encode("utf-8")
    marker = b"--" + boundary
    for part in raw_body.split(marker):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        if b"\r\n\r\n" not in part:
            continue
        header_blob, body = part.split(b"\r\n\r\n", 1)
        headers = header_blob.decode("latin1", errors="ignore")
        disposition = next((line for line in headers.split("\r\n") if line.lower().startswith("content-disposition:")), "")
        if 'name="image"' not in disposition:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        filename = filename_match.group(1) if filename_match else "reference.png"
        type_match = re.search(r"(?im)^content-type:\s*([^\r\n]+)", headers)
        part_content_type = type_match.group(1).strip() if type_match else ""
        return save_uploaded_reference_image(filename, part_content_type, body.rstrip(b"\r\n"))
    raise ValueError("アップロード画像が見つかりません")


def copy_logo_image_to_cache(name: str, source_path: Path) -> Path:
    cache_dir = logo_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    ext = source_path.suffix.lower() or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    if ext not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".svg"}:
        raise RuntimeError("対応していないロゴ画像形式です。png/jpg/webp/gif/ico/svg を指定してください")
    safe_name = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "logo"
    dest = cache_dir / f"{safe_name}{ext}"
    if source_path.resolve() != dest.resolve():
        shutil.copy2(source_path, dest)
    return dest


def upsert_user_logo_preset(
    user_id: str | None,
    name: str,
    *,
    aliases: list[str] | None = None,
    image_url: str = "",
    image_path: str = "",
    source_url: str = "",
    usage_note: str = "",
) -> dict:
    if not user_id:
        raise RuntimeError("ロゴ登録にはログインユーザーが必要です")
    clean_name = str(name or "").strip()
    if not clean_name:
        raise RuntimeError("ロゴ名が空です")
    alias_list = sorted({a.strip() for a in [clean_name, *(aliases or [])] if str(a).strip()})
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO user_logo_presets (user_id, name, aliases, image_url, image_path, source_url, usage_note)
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s)
                ON CONFLICT (user_id, name) DO UPDATE
                   SET aliases = EXCLUDED.aliases,
                       image_url = EXCLUDED.image_url,
                       image_path = EXCLUDED.image_path,
                       source_url = EXCLUDED.source_url,
                       usage_note = EXCLUDED.usage_note,
                       updated_at = NOW()
            """, (
                user_id,
                clean_name,
                json.dumps(alias_list, ensure_ascii=False),
                image_url,
                image_path,
                source_url,
                usage_note,
            ))
        conn.commit()
    finally:
        conn.close()
    return {
        "name": clean_name,
        "aliases": alias_list,
        "image_url": image_url,
        "image_path": image_path,
        "source_url": source_url,
        "usage_note": usage_note,
        "registered": True,
    }


def fetch_and_register_logo(name: str, user_id: str | None, source_url: str = "") -> dict:
    existing = next((item for item in load_logo_presets(user_id) if _normalize_logo_name(item.get("name") or "") == _normalize_logo_name(name)), None)
    if existing and (existing.get("image_path") or existing.get("image_url")):
        return {**existing, "registered": True, "status": "already_registered"}
    site_url = source_url or guess_official_site(name)
    if not site_url:
        raise RuntimeError(f"{name} の公式サイトを特定できませんでした")
    logo_url = find_logo_image_url(site_url)
    if not logo_url:
        raise RuntimeError(f"{name} のロゴ画像URLを特定できませんでした")
    logo_path = download_logo_image(name, logo_url)
    preset = upsert_user_logo_preset(
        user_id,
        name,
        aliases=[name],
        image_url=logo_url,
        image_path=str(logo_path),
        source_url=site_url,
        usage_note="ツール検出から自動取得。公式サイトのfavicon/apple-touch-icon/og:image候補を保存。",
    )
    return {**preset, "status": "registered"}


def fetch_logo_candidate(name: str, user_id: str | None, source_url: str = "") -> dict:
    existing = next((item for item in load_logo_presets(user_id) if _normalize_logo_name(item.get("name") or "") == _normalize_logo_name(name)), None)
    if existing and (existing.get("image_path") or existing.get("image_url")):
        return {**existing, "registered": True, "status": "already_registered"}
    site_url = source_url or guess_official_site(name)
    if not site_url:
        raise RuntimeError(f"{name} の公式サイトを特定できませんでした")
    logo_url = find_logo_image_url(site_url)
    if not logo_url:
        raise RuntimeError(f"{name} のロゴ画像URLを特定できませんでした")
    logo_path = download_logo_image(name, logo_url)
    return {
        "name": name,
        "aliases": [name],
        "image_url": logo_url,
        "image_path": str(logo_path),
        "source_url": site_url,
        "usage_note": "ツール検出から自動取得。OK後に辞書登録。",
        "registered": False,
        "status": "fetched",
    }


def register_logo_from_payload(user_id: str | None, payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name:
        raise RuntimeError("ロゴ名が空です")
    aliases = payload.get("aliases")
    if isinstance(aliases, str):
        aliases = [part.strip() for part in re.split(r"[,、\n]", aliases) if part.strip()]
    elif not isinstance(aliases, list):
        aliases = [name]
    image_url = str(payload.get("image_url") or "").strip()
    image_path = str(payload.get("image_path") or "").strip()
    source_url = str(payload.get("source_url") or "").strip()
    usage_note = str(payload.get("usage_note") or "ツール検出UIから登録").strip()

    resolved_path = None
    if image_path:
        resolved_path = resolve_any_local_image(image_path)
        if not resolved_path:
            raise RuntimeError("指定されたローカル画像パスが見つからないか、画像ではありません")
        image_path = str(copy_logo_image_to_cache(name, resolved_path))
    elif image_url:
        resolved_path = download_logo_image(name, image_url)
        image_path = str(resolved_path)
    else:
        raise RuntimeError("ロゴ画像URLまたはローカル画像パスが必要です")

    return upsert_user_logo_preset(
        user_id,
        name,
        aliases=aliases,
        image_url=image_url,
        image_path=image_path,
        source_url=source_url,
        usage_note=usage_note,
    )


def build_logo_input_item(logo: dict) -> dict | None:
    image_path = str(logo.get("image_path") or "").strip()
    if image_path:
        path = Path(image_path).expanduser()
        if not path.is_absolute():
            path = (LOGO_PRESETS_PATH.parent / path).resolve()
        if path.exists() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}:
            return {"type": "localImage", "path": str(path)}

    image_url = str(logo.get("image_url") or "").strip()
    if image_url:
        parsed = urlparse(image_url)
        if parsed.scheme in {"http", "https"}:
            return {"type": "image", "url": image_url}
    return None


def resolve_any_local_image(path_text: str) -> Path | None:
    path = normalize_local_image_path_text(path_text)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}:
        return None
    return resolved


def build_image_input_item(image_text: str, *, allow_any_local: bool = False) -> dict | None:
    image_text = (image_text or "").strip()
    if not image_text:
        return None
    local_path = resolve_draft_image_path(image_text) or resolve_local_image(image_text)
    if not local_path and allow_any_local:
        local_path = resolve_any_local_image(image_text)
    if local_path:
        return {"type": "localImage", "path": str(local_path)}
    parsed = urlparse(image_text)
    if parsed.scheme in {"http", "https"}:
        return {"type": "image", "url": image_text}
    return None


def append_image_generation_log(job: dict, message: str, *, level: str = "info", method: str = "") -> None:
    logs = job.setdefault("logs", [])
    job["last_event_at"] = time.time()
    logs.append({
        "at": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "method": method,
        "message": message,
    })
    del logs[:-60]


def append_image_rewrite_log(job: dict, message: str, *, level: str = "info", method: str = "") -> None:
    logs = job.setdefault("logs", [])
    job["last_event_at"] = time.time()
    logs.append({
        "at": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "method": method,
        "message": message,
    })
    del logs[:-60]


def start_image_rewrite_job(
    draft_id: str,
    current_prompt: str,
    instruction: str,
    copy_text: str = "",
    character_reference_url: str = "",
    remember_rewrite_preference: bool = False,
    mode: str = "rewrite",
    user_id: str | None = None,
    selected_logo_names: list[str] | None = None,
) -> dict:
    if mode != "regenerate":
        raise RuntimeError("画像プロンプトのAIリライトは無効です。修正依頼または画像プロンプト再生成を使ってください。")
    mode = "regenerate"
    action_label = "画像プロンプト再生成"
    remember_rewrite_preference = False
    with _image_rewrite_lock:
        running = [
            job
            for job in _image_rewrite_jobs.values()
            if job.get("status") not in {"completed", "failed", "cancelled"}
        ]
        if running:
            raise RuntimeError("別の画像プロンプト再生成がまだ実行中です。完了してから再実行してください。")

        job_id = f"{int(time.time() * 1000)}-{draft_id}"
        _image_rewrite_jobs[job_id] = {
            "job_id": job_id,
            "draft_id": draft_id,
            "started_at": time.time(),
            "status": "starting",
            "progress": 5,
            "message": f"{action_label}を準備しています",
            "mode": mode,
            "user_id": user_id or "",
            "original_prompt": current_prompt,
            "prompt": "",
            "changed": False,
            "account_memory_path": "",
            "account_memory_error": "",
            "cancel_requested": False,
            "selected_logo_names": selected_logo_names or [],
            "logs": [],
        }
        append_image_rewrite_log(_image_rewrite_jobs[job_id], "ジョブを作成しました")

    thread = threading.Thread(
        target=run_image_rewrite_job,
        args=(
            job_id,
            draft_id,
            current_prompt,
            instruction,
            copy_text,
            character_reference_url,
            remember_rewrite_preference,
            mode,
            user_id,
            selected_logo_names,
        ),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id, **_image_rewrite_jobs[job_id]}


def run_image_rewrite_job(
    job_id: str,
    draft_id: str,
    current_prompt: str,
    instruction: str,
    copy_text: str = "",
    character_reference_url: str = "",
    remember_rewrite_preference: bool = False,
    mode: str = "rewrite",
    user_id: str | None = None,
    selected_logo_names: list[str] | None = None,
) -> None:
    job = _image_rewrite_jobs[job_id]
    mode = "regenerate"
    action_label = "画像プロンプト再生成"

    def is_cancel_requested() -> bool:
        return bool(_image_rewrite_jobs.get(job_id, {}).get("cancel_requested"))

    def remember_process(proc) -> None:
        if proc is None:
            _image_rewrite_processes.pop(job_id, None)
        else:
            _image_rewrite_processes[job_id] = proc

    def on_status(method: str, params: dict) -> None:
        current = _image_rewrite_jobs.get(job_id)
        if not current or current.get("status") in {"completed", "failed", "cancelled"} or current.get("cancel_requested"):
            return
        if method == "turn/started":
            current.update({"status": "rewriting", "progress": 20, "message": "Codex App Server の turn を開始しました"})
            append_image_rewrite_log(current, "turn を開始しました", method=method)
        elif method == "item/started":
            item = params.get("item") or {}
            label = item.get("type") or "処理"
            current.update({"status": "rewriting", "progress": max(current.get("progress", 20), 35), "message": f"{label} を実行中"})
            append_image_rewrite_log(current, f"{label} を開始しました", method=method)
        elif method == "item/agentMessage/delta":
            current.update({"status": "rewriting", "progress": max(current.get("progress", 35), 55), "message": f"{action_label}結果を受信中"})
        elif method == "item/completed":
            item = params.get("item") or {}
            label = item.get("type") or "処理"
            current.update({"status": "rewriting", "progress": max(current.get("progress", 55), 78), "message": f"{action_label}結果を確認中"})
            append_image_rewrite_log(current, f"{label} が完了しました", method=method)
        elif method == "turn/completed":
            current.update({"status": "saving", "progress": 86, "message": f"{action_label}結果を保存しています"})
            append_image_rewrite_log(current, "turn が完了しました", method=method)
        elif method:
            append_image_rewrite_log(current, f"event: {method}", method=method)

    try:
        job.update({"status": "rewriting", "progress": 12, "message": f"Codex App Server に{action_label}を依頼しています"})
        append_image_rewrite_log(job, f"Codex App Server に{action_label}を依頼します")
        rewritten = rewrite_image_prompt_with_codex(
            draft_id=draft_id,
            current_prompt=current_prompt,
            instruction=instruction,
            copy_text=copy_text,
            character_reference_url=character_reference_url,
            mode=mode,
            user_id=user_id,
            selected_logo_names=selected_logo_names,
            status_callback=on_status,
            should_stop=is_cancel_requested,
            process_callback=remember_process,
        ).strip()
        if is_cancel_requested():
            job.update({"status": "cancelled", "progress": 100, "message": f"{action_label}を停止しました。指示を書き直せます。"})
            append_image_rewrite_log(job, "ユーザー操作で停止しました", level="warn")
            return
        if not rewritten:
            raise RuntimeError("リライト結果が空です")
        changed = rewritten.strip() != current_prompt.strip()
        job.update({"status": "saving", "progress": 90, "message": f"{action_label}結果を下書きへ保存しています"})
        append_image_rewrite_log(job, f"{action_label}結果を下書きへ保存します")
        image_prompt = update_image_prompt_metadata(draft_id, rewritten, copy_text)

        account_memory_path = ""
        account_memory_error = ""
        if remember_rewrite_preference:
            try:
                account_memory_path = str(append_rewrite_preference_to_account(str(draft_id), instruction))
                append_image_rewrite_log(job, "今後の注意をアカウント情報へ保存しました")
            except Exception as memory_error:
                account_memory_error = str(memory_error)
                append_image_rewrite_log(job, f"今後の注意保存に失敗: {account_memory_error}", level="warn")

        job.update({
            "status": "completed",
            "progress": 100,
            "message": f"{action_label}完了。画面へ反映しました" if changed else f"{action_label}完了。結果は元の内容と同じです",
            "prompt": rewritten,
            "image_prompt": image_prompt,
            "changed": changed,
            "account_memory_path": account_memory_path,
            "account_memory_error": account_memory_error,
        })
        append_image_rewrite_log(job, "完了しました")
    except Exception as exc:
        if is_cancel_requested():
            job.update({"status": "cancelled", "progress": 100, "message": f"{action_label}を停止しました。指示を書き直せます。"})
            append_image_rewrite_log(job, "ユーザー操作で停止しました", level="warn")
        else:
            job.update({"status": "failed", "progress": 100, "message": str(exc)[:1000]})
            append_image_rewrite_log(job, str(exc)[:1000], level="error")


def get_image_rewrite_job(job_id: str) -> dict:
    job = _image_rewrite_jobs.get(job_id)
    if not job:
        raise ValueError("画像プロンプト再生成ジョブが見つかりません")
    if job.get("status") in {"completed", "failed", "cancelled"}:
        return job
    elapsed = max(0.0, time.time() - float(job["started_at"]))
    if job.get("status") in {"starting", "rewriting"}:
        job["progress"] = min(88, max(int(job.get("progress", 12)), 12 + int(elapsed * 2.0)))
    return job


def cancel_image_rewrite_job(job_id: str) -> dict:
    with _image_rewrite_lock:
        job = _image_rewrite_jobs.get(job_id)
        if not job:
            raise ValueError("画像プロンプト再生成ジョブが見つかりません")
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return job
        action_label = "画像プロンプト再生成"
        job["cancel_requested"] = True
        job.update({
            "status": "cancelled",
            "progress": 100,
            "message": f"{action_label}を停止しました。指示を書き直せます。",
        })
        append_image_rewrite_log(job, "停止要求を受け付けました", level="warn")

    proc = _image_rewrite_processes.pop(job_id, None)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1)
            append_image_rewrite_log(job, "Codexプロセスを停止しました", level="warn")
        except Exception:
            try:
                proc.kill()
                append_image_rewrite_log(job, "Codexプロセスを強制終了しました", level="warn")
            except Exception as exc:
                append_image_rewrite_log(job, f"Codexプロセス停止に失敗: {exc}", level="error")
    return job


def list_generated_image_paths() -> set[str]:
    paths: set[str] = set()
    for root in image_generation_search_roots():
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            try:
                paths.add(str(path.resolve()))
            except OSError:
                continue
    return paths


def find_generated_image_after(started_at: float, known_paths: set[str] | None = None) -> Path | None:
    candidates: list[Path] = []
    known_paths = known_paths or set()
    for root in image_generation_search_roots():
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            try:
                resolved = str(path.resolve())
                if resolved in known_paths:
                    continue
                if path.stat().st_mtime >= started_at:
                    candidates.append(path)
            except OSError:
                continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def copy_generated_image_to_personal(draft_id: str, source_path: Path) -> Path:
    output_dir = image_generation_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / f"{draft_id}-p1{source_path.suffix.lower()}"
    if source_path.resolve() != dest.resolve():
        shutil.copy2(source_path, dest)
    return dest


def complete_image_generation_from_found(job_id: str, found: Path) -> bool:
    with _image_generation_lock:
        job = _image_generation_jobs.get(job_id)
        if not job or job.get("status") in {"completed", "failed", "cancelled"}:
            return False
        try:
            append_image_generation_log(job, f"生成画像を検出: {found}")
            personal_path = copy_generated_image_to_personal(job["draft_id"], found)
            append_image_generation_log(job, f"画像をコピー: {personal_path}")
            image_url = attach_generated_image(job["draft_id"], str(personal_path))
            append_image_generation_log(job, "1投稿目に画像を添付しました")
            job.update({
                "status": "completed",
                "progress": 100,
                "message": "画像生成完了。プレビューに反映しました",
                "image_path": str(personal_path),
                "image_url": image_url,
            })
            return True
        except Exception as exc:
            job.update({"status": "failed", "progress": 100, "message": str(exc)[:1000]})
            append_image_generation_log(job, str(exc)[:1000], level="error")
            return False


def find_image_path_in_text(text: str) -> Path | None:
    if not text:
        return None
    patterns = [
        r"file://(/[^)\]\s\"']+\.(?:png|jpe?g|webp))",
        r"(/Users/[^)\]\s\"']+\.(?:png|jpe?g|webp))",
        r"(/tmp/[^)\]\s\"']+\.(?:png|jpe?g|webp))",
        r"(/var/folders/[^)\]\s\"']+\.(?:png|jpe?g|webp))",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            raw = unquote(match.group(1))
            candidate = Path(raw)
            try:
                if candidate.exists() and candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None


def fail_image_generation_job(job_id: str, message: str, *, level: str = "error") -> dict | None:
    with _image_generation_lock:
        job = _image_generation_jobs.get(job_id)
        if not job or job.get("status") in {"completed", "failed", "cancelled"}:
            return job
        job.update({"status": "failed", "progress": 100, "message": message})
        append_image_generation_log(job, message, level=level)

    proc = _image_generation_processes.pop(job_id, None)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1)
            append_image_generation_log(job, "Codexプロセスを停止しました", level="warn")
        except Exception:
            try:
                proc.kill()
                append_image_generation_log(job, "Codexプロセスを強制終了しました", level="warn")
            except Exception as exc:
                append_image_generation_log(job, f"Codexプロセス停止に失敗: {exc}", level="error")
    return job


def watch_generated_image_job(job_id: str, timeout_seconds: float = 420.0) -> None:
    while True:
        job = _image_generation_jobs.get(job_id)
        if not job or job.get("status") in {"completed", "failed", "cancelled"}:
            return
        elapsed = time.time() - float(job["started_at"])
        if elapsed > timeout_seconds:
            fail_image_generation_job(job_id, "画像生成が長時間完了しなかったため停止しました。再実行してください。")
            return
        known_paths = set(job.get("known_image_paths") or [])
        found = find_generated_image_after(float(job["started_at"]), known_paths)
        if found and complete_image_generation_from_found(job_id, found):
            return
        time.sleep(2.0)


def start_image_generation_job(
    draft_id: str,
    copy_text: str,
    prompt: str,
    character_reference_url: str = "",
    character_traits: str = "",
    character_negative: str = "",
    character_placement: str = "",
    feedback: str = "",
    current_image_url: str = "",
    reference_image_url: str = "",
    reference_image_path: str = "",
    user_id: str | None = None,
    selected_logo_names: list[str] | None = None,
) -> dict:
    output_dir = image_generation_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    with _image_generation_lock:
        running = [
            job
            for job in _image_generation_jobs.values()
            if job.get("status") not in {"completed", "failed", "cancelled"}
        ]
        if running:
            raise RuntimeError("別の画像生成がまだ実行中です。完了してから次の画像を生成してください。")
        known_image_paths = sorted(list_generated_image_paths())

    job_id = f"{int(time.time() * 1000)}-{draft_id}"
    expected_output_path = output_dir / f"{job_id}-generated.png"
    logo_references = detect_logo_references(copy_text, prompt, user_id=user_id, selected_names=selected_logo_names)
    _image_generation_jobs[job_id] = {
        "draft_id": draft_id,
        "user_id": user_id or "",
        "started_at": time.time(),
        "last_event_at": time.time(),
        "known_image_paths": known_image_paths,
        "expected_output_path": str(expected_output_path),
        "status": "starting",
        "progress": 5,
        "request": build_image_generation_request(
            copy_text,
            prompt,
            character_reference_url,
            character_traits,
            character_negative,
            character_placement,
            feedback,
            current_image_url,
            reference_image_url,
            reference_image_path,
            logo_references,
        ),
        "logo_references": logo_references,
        "selected_logo_names": selected_logo_names,
        "image_url": "",
        "message": "Codex App Server を起動しています",
        "cancel_requested": False,
        "logs": [],
    }
    append_image_generation_log(_image_generation_jobs[job_id], "ジョブを作成しました")
    thread = threading.Thread(
        target=run_image_generation_job,
        args=(job_id, copy_text, prompt, character_reference_url, character_traits, character_negative, character_placement, feedback, current_image_url, reference_image_url, reference_image_path),
        daemon=True,
    )
    thread.start()
    watcher = threading.Thread(target=watch_generated_image_job, args=(job_id,), daemon=True)
    watcher.start()
    return {"job_id": job_id, **_image_generation_jobs[job_id]}


def run_image_generation_job(
    job_id: str,
    copy_text: str,
    prompt: str,
    character_reference_url: str = "",
    character_traits: str = "",
    character_negative: str = "",
    character_placement: str = "",
    feedback: str = "",
    current_image_url: str = "",
    reference_image_url: str = "",
    reference_image_path: str = "",
) -> None:
    job = _image_generation_jobs[job_id]

    def is_cancel_requested() -> bool:
        return bool(_image_generation_jobs.get(job_id, {}).get("cancel_requested"))

    def remember_process(proc) -> None:
        if proc is None:
            _image_generation_processes.pop(job_id, None)
        else:
            _image_generation_processes[job_id] = proc

    codex_path = shutil.which("codex")
    if not codex_path:
        job.update({"status": "failed", "progress": 100, "message": "codex コマンドが見つかりません"})
        append_image_generation_log(job, "codex コマンドが見つかりません", level="error")
        return

    started_at = float(job["started_at"])
    repo_root = Path(__file__).resolve().parents[1]
    expected_output_path = str(job.get("expected_output_path") or (image_generation_output_dir() / f"{job['draft_id']}-p1-generated.png"))
    image_request = build_image_generation_request(
        copy_text,
        prompt,
        character_reference_url,
        character_traits,
        character_negative,
        character_placement,
        feedback,
        current_image_url,
        reference_image_url,
        reference_image_path,
        job.get("logo_references") or [],
    )
    task_label = "修正フィードバック付きで再生成" if feedback or current_image_url or reference_image_url or reference_image_path else "生成"
    dev_mode_label = current_dev_mode_label()
    turn_prompt = f"""
imagegen スキルを使って、以下のX投稿用図解画像を1枚生成してください。
これはX下書きアプリの「画像生成」ボタン押下で開始されたバックグラウンドジョブです。
ユーザーはこの操作で、指定保存先へ画像ファイルを1つ作成することを承認済みです。
リポジトリの現在のモード: {dev_mode_label}
確認質問・承認待ち・「このまま続けてよいですか？」で終了しないでください。
この画面には質問を返せないため、実行できない場合は ERROR: から始まる短い理由だけを返してください。
ユーザーはAPI利用を明示していないため、画像生成API・APIキー・OPENAI_API_KEY・SDK・CLI fallbackは使わないでください。
必ず Codex/ChatGPT サブスク内の組み込み画像生成として実行してください。
生成後は最終画像を次のローカルパスへPNGで保存またはコピーしてください。
保存先: {expected_output_path}
この保存先ファイルが存在することを確認してから、保存したパスだけを短く返してください。
参照画像が添付されている場合は、ユーザーのフィードバックで指定された要素を参照画像に近づけてください。

{image_request}
""".strip()

    def on_status(method: str, params: dict) -> None:
        current = _image_generation_jobs.get(job_id)
        if not current or current.get("status") in {"completed", "failed", "cancelled"} or current.get("cancel_requested"):
            return
        if method == "turn/started":
            current.update({"status": "generating", "progress": 18, "message": "Codex App Server の turn を開始しました"})
            append_image_generation_log(current, "turn を開始しました", method=method)
        elif method == "item/started":
            item = params.get("item") or {}
            label = item.get("type") or "処理"
            current.update({"status": "generating", "progress": max(current.get("progress", 18), 32), "message": f"{label} を実行中"})
            append_image_generation_log(current, f"{label} を開始しました", method=method)
        elif method == "item/completed":
            item = params.get("item") or {}
            label = item.get("type") or "処理"
            current.update({"status": "generating", "progress": max(current.get("progress", 32), 70), "message": "画像生成結果を確認中"})
            append_image_generation_log(current, f"{label} が完了しました", method=method)
        elif method == "turn/completed":
            append_image_generation_log(current, "turn が完了しました。生成ファイルを確認します", method=method)
        elif method:
            append_image_generation_log(current, f"event: {method}", method=method)

    try:
        job.update({"status": "generating", "progress": 12, "message": "Codex App Server に画像生成を依頼しています"})
        append_image_generation_log(job, "Codex App Server に画像生成を依頼します")
        input_items = []
        if IMAGEGEN_SKILL_PATH.exists():
            input_items.append({"type": "skill", "name": "imagegen", "path": str(IMAGEGEN_SKILL_PATH)})
            append_image_generation_log(job, f"imagegen スキルを添付: {IMAGEGEN_SKILL_PATH}")
        else:
            append_image_generation_log(job, f"imagegen スキルが見つかりません: {IMAGEGEN_SKILL_PATH}", level="warn")
        if character_reference_url:
            character_item = build_image_input_item(character_reference_url)
            if character_item:
                input_items.append(character_item)
            append_image_generation_log(job, f"キャラクター参照画像を添付: {character_reference_url}")
        for logo in job.get("logo_references") or []:
            logo_item = build_logo_input_item(logo)
            if logo_item:
                input_items.append(logo_item)
                append_image_generation_log(job, f"ロゴ参照画像を添付: {logo.get('name')}")
            else:
                append_image_generation_log(job, f"ロゴ参照画像が未設定: {logo.get('name')}", level="warn")
        current_item = build_image_input_item(current_image_url)
        if current_item:
            input_items.append(current_item)
            append_image_generation_log(job, f"現在の生成画像を添付: {current_image_url}")
        reference_text = reference_image_path or reference_image_url
        reference_item = build_image_input_item(reference_text, allow_any_local=bool(reference_image_path))
        if reference_item:
            input_items.append(reference_item)
            append_image_generation_log(job, f"修正用参照画像を添付: {reference_text}")
        input_items.append({"type": "text", "text": turn_prompt})
        result_text = run_codex_app_server_turn(
            codex_path,
            repo_root,
            turn_prompt,
            timeout=420,
            developer_instructions=(
                f"あなたは画像生成担当です。今回のタスクは{task_label}です。ユーザーがAPI利用を明示していないため、"
                "画像生成API・APIキー・OPENAI_API_KEY・SDK・CLI fallbackは使わず、"
                "利用可能な組み込み画像生成/サブスク内画像生成を使ってください。"
                f"これはX下書きアプリの画像生成ボタンから起動されたバックグラウンドジョブで、"
                f"現在のリポジトリモードは「{dev_mode_label}」です。"
                "ユーザーは指定保存先へ画像を1ファイル作成することをこの操作で承認済みです。"
                "AGENTS.mdの新規ファイル作成確認を理由に質問で停止しないでください。"
                "ユーザーへの確認質問、進捗説明のみの返答、承認待ち文は禁止です。"
                "生成後は指定されたワークスペース内パスへ画像ファイルを保存してください。"
                "実行できない場合は ERROR: から始まる短い理由だけを返してください。"
            ),
            sandbox_policy={"type": "workspaceWrite", "networkAccess": False},
            input_items=input_items,
            status_callback=on_status,
            should_stop=lambda: bool(_image_generation_jobs.get(job_id, {}).get("cancel_requested"))
            or _image_generation_jobs.get(job_id, {}).get("status") == "completed",
            process_callback=remember_process,
            allow_empty_result=True,
            wait_for_turn_completed=True,
        )
        if result_text:
            append_image_generation_log(job, f"App Server応答: {result_text[:240]}")
            if "このまま続けてよいですか" in result_text or "現在のモード:" in result_text:
                raise RuntimeError(
                    "画像生成エージェントが確認待ちで終了しました。ページを再読み込みして、もう一度画像生成を実行してください。"
                )
            if result_text.strip().startswith("ERROR:"):
                raise RuntimeError(result_text.strip()[:1000])
        if is_cancel_requested():
            job.update({"status": "cancelled", "progress": 100, "message": "画像生成を停止しました。プロンプトを書き直して再実行できます。"})
            append_image_generation_log(job, "ユーザー操作で停止しました", level="warn")
            return
        if job.get("status") == "completed":
            return
        expected_path = Path(expected_output_path)
        known_paths = set(job.get("known_image_paths") or [])
        wait_deadline = time.monotonic() + 30
        wait_logged_at = 0.0
        while time.monotonic() < wait_deadline and not is_cancel_requested():
            if expected_path.exists():
                complete_image_generation_from_found(job_id, expected_path)
                return
            text_path = find_image_path_in_text(result_text or "")
            if text_path:
                complete_image_generation_from_found(job_id, text_path)
                return
            found = find_generated_image_after(started_at, known_paths)
            if found:
                complete_image_generation_from_found(job_id, found)
                return
            if time.monotonic() - wait_logged_at >= 8:
                wait_logged_at = time.monotonic()
                job.update({"status": "generating", "progress": max(int(job.get("progress", 70)), 88), "message": "生成画像ファイルの保存を確認中"})
                append_image_generation_log(job, "生成画像ファイルの保存を確認中")
            time.sleep(1)
        if is_cancel_requested():
            job.update({"status": "cancelled", "progress": 100, "message": "画像生成を停止しました。プロンプトを書き直して再実行できます。"})
            append_image_generation_log(job, "ユーザー操作で停止しました", level="warn")
            return
        if job.get("status") != "completed":
            message = "Codex App Server の turn は完了しましたが、指定保存先にも生成画像ファイルが見つかりませんでした"
            job.update({"status": "failed", "progress": 100, "message": message})
            append_image_generation_log(job, f"{message}: {expected_output_path}", level="error")
    except Exception as exc:
        if is_cancel_requested():
            job.update({"status": "cancelled", "progress": 100, "message": "画像生成を停止しました。プロンプトを書き直して再実行できます。"})
            append_image_generation_log(job, "ユーザー操作で停止しました", level="warn")
        else:
            job.update({"status": "failed", "progress": 100, "message": str(exc)[:1000]})
            append_image_generation_log(job, str(exc)[:1000], level="error")


def get_image_generation_job(job_id: str) -> dict:
    job = _image_generation_jobs.get(job_id)
    if not job:
        raise ValueError("画像生成ジョブが見つかりません")
    if job.get("status") in {"completed", "failed", "cancelled"}:
        return job

    elapsed = max(0.0, time.time() - float(job["started_at"]))
    found = find_generated_image_after(float(job["started_at"]), set(job.get("known_image_paths") or []))
    if found:
        complete_image_generation_from_found(job_id, found)
    else:
        idle = time.time() - float(job.get("last_event_at") or job["started_at"])
        progress = int(job.get("progress", 12))
        if elapsed >= IMAGE_GENERATION_MAX_SECONDS:
            fail_image_generation_job(job_id, "画像生成が長時間完了しなかったため停止しました。再実行してください。")
            return _image_generation_jobs[job_id]
        if progress <= 18 and idle >= IMAGE_GENERATION_STALL_SECONDS:
            fail_image_generation_job(job_id, "Codex App Server の画像生成応答が止まったため停止しました。再実行してください。")
            return _image_generation_jobs[job_id]
        if job.get("status") == "generating":
            job["progress"] = min(92, max(int(job.get("progress", 12)), 12 + int(elapsed * 1.2)))
    return job


def cancel_image_generation_job(job_id: str) -> dict:
    with _image_generation_lock:
        job = _image_generation_jobs.get(job_id)
        if not job:
            raise ValueError("画像生成ジョブが見つかりません")
        if job.get("status") in {"completed", "failed", "cancelled"}:
            return job
        job["cancel_requested"] = True
        job.update({
            "status": "cancelled",
            "progress": 100,
            "message": "画像生成を停止しました。プロンプトを書き直して再実行できます。",
        })
        append_image_generation_log(job, "停止要求を受け付けました", level="warn")

    proc = _image_generation_processes.pop(job_id, None)
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=1)
            append_image_generation_log(job, "Codexプロセスを停止しました", level="warn")
        except Exception:
            try:
                proc.kill()
                append_image_generation_log(job, "Codexプロセスを強制終了しました", level="warn")
            except Exception as exc:
                append_image_generation_log(job, f"Codexプロセス停止に失敗: {exc}", level="error")
    return job


def append_auto_post_log(job: dict, message: str, *, level: str = "info") -> None:
    logs = job.setdefault("logs", [])
    logs.append({
        "at": datetime.now().strftime("%H:%M:%S"),
        "level": level,
        "message": message,
    })
    del logs[:-30]


def get_openclaw_gateway_token() -> str:
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    if token:
        return token
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    if not config_path.exists():
        return ""
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(config.get("gateway", {}).get("auth", {}).get("token") or "").strip()


def run_openclaw_browser(args: list[str], *, timeout: int = 60) -> str:
    openclaw = shutil.which("openclaw")
    if not openclaw:
        raise RuntimeError("openclaw コマンドが見つかりません")
    command = [openclaw, "browser"]
    token = get_openclaw_gateway_token()
    if token:
        command.extend(["--token", token])
    command.extend(["--timeout", str(max(timeout, 1) * 1000)])
    command.extend(args)
    proc = subprocess.run(command, capture_output=True, text=True, timeout=timeout + 10)
    output = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
    if proc.returncode != 0:
        message = output or f"openclaw browser {' '.join(args)} failed"
        if is_openclaw_tab_missing_error(message):
            message = (
                f"{message}\n"
                "OpenClaw Browser Relay が操作対象タブに接続されていません。"
                "ChromeでXのタブを開き、OpenClaw Browser Relay 拡張アイコンをクリックして badge ON にしてから再実行してください。"
            )
        raise RuntimeError(message)
    return output


def is_openclaw_tab_missing_error(message: str) -> bool:
    text = str(message or "")
    return "HTTP 404" in text or "tab not found" in text or "no attached Chrome tabs" in text


def open_chrome_url(url: str) -> None:
    subprocess.run(["open", "-a", "Google Chrome", url], check=False, timeout=10)


def capture_openclaw_screenshot(job: dict, label: str) -> str:
    try:
        output = run_openclaw_browser(["screenshot"], timeout=60)
    except Exception as exc:
        append_auto_post_log(job, f"{label} のスクショ取得に失敗: {exc}", level="warn")
        return ""
    path_match = re.search(r"MEDIA:(\S+)", output)
    screenshot_path = path_match.group(1) if path_match else output.strip()
    append_auto_post_log(job, f"{label} のスクショ: {screenshot_path}")
    return screenshot_path


def copy_text_to_clipboard(text: str) -> bool:
    try:
        subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=5)
        return True
    except Exception:
        return False


def build_auto_post_failure_prompt(job: dict, error: Exception, screenshot_path: str) -> str:
    logs = job.get("logs") if isinstance(job.get("logs"), list) else []
    log_lines = "\n".join(
        f"- {item.get('at', '')} [{item.get('level', 'info')}] {item.get('message', '')}"
        for item in logs[-20:]
        if isinstance(item, dict)
    )
    return f"""X自動投稿フローの改善案を作成してください。

目的:
- OpenClaw browser で X の投稿モーダルを操作する自動投稿を安定化する。
- 画像付き投稿の場合は、メイン投稿文入力後に既存画像をクリップボードへコピーし、投稿欄をクリックしてからペーストする。
- その後、Postボタン横の+ボタンでツリーを1つずつ追加し、各パーツ欄をクリックして本文を入れる。
- 各ステップでスクショとDOM検証を挟み、失敗したら投稿せず停止する。

今回の失敗:
- draft_id: {job.get('draft_id', '')}
- job_id: {job.get('job_id', '')}
- as_quote: {job.get('as_quote', False)}
- error:
{str(error)[:3000]}

失敗時スクリーンショット:
{screenshot_path or '(スクショ取得失敗)'}

直近ログ:
{log_lines or '(ログなし)'}

確認してほしいこと:
1. Xの現在UIで、メイン投稿欄・画像添付プレビュー・Post横の+ボタン・追加されたパーツ欄・Postボタンを正しく特定できているか。
2. 誤検出しやすいセレクタや、DOM上でアイコン画像を添付画像と誤判定する箇所がないか。
3. 20秒前後のOpenClaw/Gateway timeoutを避けるため、操作をさらに短い単位へ分割すべき箇所。
4. スクショに基づき、次に修正すべき具体的な実装案。
5. 改善が完了し、原因確認に不要になったら、上記の失敗時スクリーンショットを削除すること。

出力形式:
- 原因仮説
- スクショから見るUI状態
- 修正案
- 具体的なコード変更方針
- 再テスト手順
- スクショ削除結果
"""


def handle_auto_post_failure(job: dict, error: Exception) -> None:
    screenshot_path = capture_openclaw_screenshot(job, "エラー発生時")
    prompt = build_auto_post_failure_prompt(job, error, screenshot_path)
    if copy_text_to_clipboard(prompt):
        message = "AIエージェントへの改善指示をクリップボードに保存しました。AIエージェントに指示をお願いします。"
        job["message"] = message
        job["agent_prompt_copied"] = True
        job["agent_prompt_notice"] = message
        job["agent_prompt_text"] = prompt
        append_auto_post_log(job, message)
    else:
        append_auto_post_log(job, "改善案作成用プロンプトのクリップボード保存に失敗しました", level="warn")


def resolve_draft_image_path(image_url: str) -> Path | None:
    if not image_url:
        return None
    parsed = urlparse(image_url)
    if parsed.path == "/local-image" or image_url.startswith("/local-image"):
        qs = parse_qs(parsed.query)
        return resolve_local_image(qs.get("path", [""])[0])
    return resolve_local_image(image_url)


def copy_image_to_clipboard(image_url: str) -> bool:
    source = resolve_draft_image_path(image_url)
    if not source:
        return False
    suffix = source.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        image_class = "JPEG picture"
    else:
        image_class = "«class PNGf»"
    script = f'set the clipboard to (read (POSIX file {json.dumps(str(source))}) as {image_class})'
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=15)
    return True


def prepare_openclaw_upload_image(draft_id: str, image_url: str) -> Path | None:
    source = resolve_draft_image_path(image_url)
    if not source:
        return None
    upload_dir = Path("/tmp/openclaw/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    suffix = source.suffix.lower() if source.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"} else ".png"
    target = upload_dir / f"x-draft-{draft_id}{suffix}"
    shutil.copy2(source, target)
    return target


def upload_image_to_x_file_input(draft_id: str, image_url: str) -> bool:
    upload_path = prepare_openclaw_upload_image(draft_id, image_url)
    if not upload_path:
        return False
    run_openclaw_browser([
        "upload",
        "--element",
        'input[data-testid="fileInput"], input[type="file"][accept*="image"], input[type="file"]',
        str(upload_path),
    ], timeout=60)
    return True


def paste_clipboard_to_chrome() -> None:
    script = """
tell application "Google Chrome" to activate
delay 0.2
tell application "System Events"
  keystroke "v" using command down
end tell
"""
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, text=True, timeout=10)


def x_compose_dom_helpers_js() -> str:
    return """
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  const isVisible = (el) => {
    if (!el) return false;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
  };
  const enabled = (el) => el && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  const isPostButtonText = (text) => /^(Post|Post all|ポスト|すべてポスト|投稿|すべて投稿)$/.test((text || '').trim());
  const composeRoot = () => {
    const modalDialogs = Array.from(document.querySelectorAll('div[aria-modal="true"][role="dialog"]')).filter(isVisible);
    const dialogs = modalDialogs.length
      ? modalDialogs
      : Array.from(document.querySelectorAll('[role="dialog"]')).filter(isVisible);
    const roots = dialogs.length ? dialogs : [document];
    const hasTextbox = (root) => root.querySelector('div[data-testid^="tweetTextarea_"][role="textbox"][contenteditable="true"]');
    const hasPostButton = (root) => Array.from(root.querySelectorAll('[data-testid="tweetButton"], button'))
      .some(el => isVisible(el) && isPostButtonText(el.innerText || el.textContent || ''));
    return roots.find(root => hasTextbox(root) && hasPostButton(root))
      || roots.find(hasTextbox)
      || roots[0]
      || document;
  };
  const textboxes = () => Array.from(composeRoot().querySelectorAll('div[data-testid^="tweetTextarea_"][role="textbox"][contenteditable="true"]'))
    .filter(isVisible)
    .sort((a, b) => {
      const aid = a.getAttribute('data-testid') || '';
      const bid = b.getAttribute('data-testid') || '';
      const ai = Number((aid.match(/tweetTextarea_(\\d+)/) || [])[1] || 999);
      const bi = Number((bid.match(/tweetTextarea_(\\d+)/) || [])[1] || 999);
      if (ai !== bi) return ai - bi;
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return ar.top - br.top || ar.left - br.left;
    });
  const textboxForPart = (partIndex) => {
    const exact = composeRoot().querySelector(`div[data-testid="tweetTextarea_${partIndex - 1}"][role="textbox"][contenteditable="true"]`);
    return isVisible(exact) ? exact : textboxes()[partIndex - 1];
  };
  const waitFor = async (fn, label, timeout = 12000) => {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const value = await fn();
      if (value) return value;
      await sleep(300);
    }
    throw new Error(label + ' が見つかりません');
  };
  const nearestScrollParent = (el) => {
    let node = el?.parentElement;
    while (node && node !== document.body) {
      const style = window.getComputedStyle(node);
      if (/(auto|scroll)/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 8) return node;
      node = node.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  };
  const scrollPartControlsIntoView = async (el) => {
    if (!el) return;
    el.scrollIntoView({ block: 'end', inline: 'nearest' });
    const scroller = nearestScrollParent(el);
    if (scroller) scroller.scrollTop += 180;
    await sleep(600);
  };
  const settleTextbox = async (el) => {
    if (!el) return;
    el.click();
    el.focus();
    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: '' }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: 'Process' }));
    await sleep(500);
    el.blur();
    await sleep(900);
  };
"""


def build_x_set_main_text_script(text: str) -> str:
    text_json = json.dumps(text, ensure_ascii=False)
    return f"""async () => {{
  const text = {text_json};
{x_compose_dom_helpers_js()}
  const setText = async (el, value) => {{
    el.scrollIntoView({{ block: 'center' }});
    el.click();
    el.focus();
    await sleep(100);
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand('delete', false, null);
    el.textContent = '';
    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'deleteContentBackward', data: null }}));
    await sleep(100);
    document.execCommand('insertText', false, value);
    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: value }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    await sleep(350);
  }};
  const firstBox = await waitFor(() => textboxForPart(1), '本文入力欄');
  await setText(firstBox, text || '');
  firstBox.focus();
  return {{ ok: true }};
}}"""


def build_x_add_tree_part_script(text: str, part_index: int) -> str:
    text_json = json.dumps(text, ensure_ascii=False)
    return f"""async () => {{
  const text = {text_json};
  const partIndex = {part_index};
{x_compose_dom_helpers_js()}
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const containsExpected = (actual, expected) => {{
    const actualText = normalize(actual);
    const expectedText = normalize(expected);
    if (!expectedText) return true;
    if (actualText === expectedText || actualText.includes(expectedText)) return true;
    const chunks = expectedText.split(/\\s+/).filter(Boolean);
    let offset = 0;
    return chunks.every(chunk => {{
      const index = actualText.indexOf(chunk, offset);
      if (index < 0) return false;
      offset = index + chunk.length;
      return true;
    }});
  }};
  const boxContainingText = (expected) => textboxes().find(box => containsExpected(box.innerText || box.textContent || '', expected));
  const findPostButton = () => {{
    const root = composeRoot();
    const candidates = Array.from(root.querySelectorAll('[data-testid="tweetButton"], button'));
    return candidates.find(el => isVisible(el) && isPostButtonText(el.innerText || el.textContent || ''));
  }};
  const findAddPostButton = () => {{
    const dialog = composeRoot();
    const directCandidates = Array.from(dialog.querySelectorAll(
      '[data-testid="addButton"], button[aria-label="Add post"], [role="button"][aria-label="Add post"], button[aria-label="投稿を追加"], [role="button"][aria-label="投稿を追加"]'
    ));
  const direct = directCandidates.find(el => {{
      if (!isVisible(el)) return false;
      return el.getAttribute('data-testid') === 'addButton' || /^(Add post|投稿を追加)$/.test(el.getAttribute('aria-label') || '');
    }});
    if (direct) return direct;
    const postButton = findPostButton();
    if (!postButton) return null;
    const postRect = postButton.getBoundingClientRect();
    const directNearPost = directCandidates.find(el => {{
      if (!isVisible(el) || el === postButton) return false;
      const rect = el.getBoundingClientRect();
      const closeToPost = rect.right <= postRect.left + 24 && Math.abs((rect.top + rect.bottom) / 2 - (postRect.top + postRect.bottom) / 2) < 48;
      return el.getAttribute('data-testid') === 'addButton' || closeToPost;
    }});
    if (directNearPost) return directNearPost;
    const buttons = Array.from(dialog.querySelectorAll('button, [role="button"], [tabindex="0"]')).filter(el => {{
      if (!isVisible(el) || !enabled(el) || el === postButton) return false;
      const aria = el.getAttribute('aria-label') || '';
      if (/media|gif|emoji|schedule|location|description|people|sensitive|disclosure|draft/i.test(aria)) return false;
      const rect = el.getBoundingClientRect();
      const nearPost = rect.right <= postRect.left + 8 && Math.abs((rect.top + rect.bottom) / 2 - (postRect.top + postRect.bottom) / 2) < 32;
      const smallButton = rect.width >= 24 && rect.width <= 56 && rect.height >= 24 && rect.height <= 56;
      return nearPost && smallButton;
    }});
    return buttons.sort((a, b) => b.getBoundingClientRect().right - a.getBoundingClientRect().right)[0] || null;
  }};
  const findAddAnotherPostTarget = () => {{
    const dialog = composeRoot();
    const textPattern = /^(Add another post|別のポストを追加|投稿を追加|さらに投稿を追加)$/;
    const candidates = Array.from(dialog.querySelectorAll('div, span, button, [role="button"], [tabindex="0"]')).filter(el => {{
      if (!isVisible(el)) return false;
      const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
      if (!textPattern.test(text)) return false;
      const rect = el.getBoundingClientRect();
      return rect.width >= 80 && rect.height >= 20;
    }});
    const label = candidates.sort((a, b) => {{
      const ar = a.getBoundingClientRect();
      const br = b.getBoundingClientRect();
      return (br.width * br.height) - (ar.width * ar.height);
    }})[0];
    if (!label) return null;
    return label.closest('button, [role="button"], [tabindex="0"]') || label.closest('div') || label;
  }};
  const findAddTreeTarget = async () => {{
    const addAnother = findAddAnotherPostTarget();
    if (addAnother) return {{ el: addAnother, kind: 'add-another-post' }};
    const button = findAddPostButton();
    if (!button) return null;
    if (!enabled(button)) {{
      if (previousBox) await settleTextbox(previousBox);
      return null;
    }}
    return {{ el: button, kind: 'plus-button' }};
  }};
  const findPollButton = () => {{
    const dialog = composeRoot();
    const candidates = Array.from(dialog.querySelectorAll('[data-testid="createPollButton"], button[aria-label="Add poll"], [role="button"][aria-label="Add poll"], button[aria-label="投票を追加"], [role="button"][aria-label="投票を追加"]'));
    const exact = candidates.find(el => isVisible(el) && enabled(el));
    if (exact) return exact;
    return Array.from(dialog.querySelectorAll('button, [role="button"]')).find(el => {{
      if (!isVisible(el) || !enabled(el)) return false;
      const aria = el.getAttribute('aria-label') || '';
      if (/poll|投票/i.test(aria)) return true;
      const paths = Array.from(el.querySelectorAll('path')).map(path => path.getAttribute('d') || '').join(' ');
      return /M6 5c-1\\.1 0-2 \\.895-2 2s\\.9 2 2 2/.test(paths);
    }}) || null;
  }};
  const findRemovePollButton = () => {{
    const dialog = composeRoot();
    return Array.from(dialog.querySelectorAll('button, [role="button"], div, span')).find(el => {{
      if (!isVisible(el)) return false;
      const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
      const aria = el.getAttribute('aria-label') || '';
      return /^(Remove poll|投票を削除)$/.test(text) || /Remove poll|投票を削除/.test(aria);
    }}) || null;
  }};
  const revealAddTargetViaPoll = async () => {{
    const pollButton = findPollButton();
    if (!pollButton) return null;
    await clickCenter(pollButton);
    await waitFor(() => findRemovePollButton(), 'Remove poll', 4000);
    await sleep(500);
    return await waitFor(() => findAddTreeTarget(), '投票表示後のツリー追加操作', 5000).catch(() => null);
  }};
  const clickCenter = async (el) => {{
    el.scrollIntoView({{ block: 'center', inline: 'center' }});
    await sleep(150);
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    if (typeof el.click === 'function') {{
      el.click();
      await sleep(600);
      return;
    }}
    const rawTarget = document.elementFromPoint(x, y);
    const target = rawTarget instanceof Element ? rawTarget : el;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
      target.dispatchEvent(new MouseEvent(type, {{
        bubbles: true,
        cancelable: true,
        clientX: x,
        clientY: y,
        view: window,
      }}));
    }}
    await sleep(500);
  }};
  const waitForPartBox = async (timeout = 4000) => {{
    const start = Date.now();
    while (Date.now() - start < timeout) {{
      const exact = textboxForPart(partIndex);
      if (exact) return exact;
      const boxes = textboxes();
      if (boxes.length > before) return boxes[boxes.length - 1];
      await sleep(250);
    }}
    return null;
  }};
  const clickAddTargetAndWaitForBox = async (target) => {{
    if (!target) return null;
    await clickCenter(target.el || target);
    await sleep(250);
    return await waitForPartBox(1800);
  }};
  const setText = async (el, text) => {{
    el.scrollIntoView({{ block: 'center' }});
    el.click();
    el.focus();
    await sleep(100);
    const selection = window.getSelection();
    const range = document.createRange();
    range.selectNodeContents(el);
    selection.removeAllRanges();
    selection.addRange(range);
    document.execCommand('delete', false, null);
    el.textContent = '';
    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'deleteContentBackward', data: null }}));
    const clearStart = Date.now();
    while (normalize(el.innerText || el.textContent || '') && Date.now() - clearStart < 1200) {{
      el.focus();
      document.execCommand('selectAll', false, null);
      document.execCommand('delete', false, null);
      el.textContent = '';
      el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'deleteContentBackward', data: null }}));
      await sleep(150);
    }}
    const residual = normalize(el.innerText || el.textContent || '');
    if (residual) throw new Error(`パート ${{partIndex}} の入力欄を空にできません: ${{residual.slice(0, 80)}}`);
    await sleep(250);
    document.execCommand('insertText', false, text || '');
    el.dispatchEvent(new InputEvent('input', {{ bubbles: true, inputType: 'insertText', data: text || '' }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    await settleTextbox(el);
    await sleep(350);
  }};

  await waitFor(() => textboxes().length > 0, '本文入力欄');
  const alreadyFilledBox = boxContainingText(text);
  if (alreadyFilledBox) {{
    return {{ ok: true, partIndex, skipped: true, reason: 'already-filled' }};
  }}
  const before = textboxes().length;
  const previousBox = textboxForPart(partIndex - 1) || textboxes()[textboxes().length - 1];
  const isNewPartBox = (box) => {{
    if (!box || box === previousBox) return false;
    const boxes = textboxes();
    const index = boxes.indexOf(box);
    if (index < before) return false;
    const testId = box.getAttribute('data-testid') || '';
    const exact = testId === `tweetTextarea_${{partIndex - 1}}`;
    return exact || boxes.length > before;
  }};
  if (previousBox) {{
    await settleTextbox(previousBox);
    await scrollPartControlsIntoView(previousBox);
  }}
  let addTarget = await waitFor(async () => {{
    const target = await findAddTreeTarget();
    if (target) return target;
    if (previousBox) await scrollPartControlsIntoView(previousBox);
    return null;
  }}, 'ツリー追加操作', 3000).catch(() => null);
  if (!addTarget) {{
    addTarget = await revealAddTargetViaPoll();
    if (addTarget && previousBox) await scrollPartControlsIntoView(previousBox);
  }}
  let nextBox = await clickAddTargetAndWaitForBox(addTarget);
  if (!nextBox) throw new Error(`パート ${{partIndex}} の入力欄 が見つかりません`);
  if (!isNewPartBox(nextBox)) {{
    throw new Error(`パート ${{partIndex}} の新規入力欄ではないため停止しました`);
  }}
  if (containsExpected(nextBox.innerText || nextBox.textContent || '', text)) {{
    return {{ ok: true, partIndex, skipped: true, reason: 'next-box-already-filled' }};
  }}
  await setText(nextBox, text || '');
  return {{ ok: true, partIndex }};
}}"""


def build_x_verify_part_text_script(text: str, part_index: int) -> str:
    text_json = json.dumps(text, ensure_ascii=False)
    return f"""async () => {{
  const expected = {text_json};
  const partIndex = {part_index};
{x_compose_dom_helpers_js()}
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const containsExpected = (actual, expected) => {{
    const actualText = normalize(actual);
    const expectedText = normalize(expected);
    if (!expectedText) return true;
    if (actualText === expectedText || actualText.includes(expectedText)) return true;
    const chunks = expectedText.split(/\\s+/).filter(Boolean);
    let offset = 0;
    return chunks.every(chunk => {{
      const index = actualText.indexOf(chunk, offset);
      if (index < 0) return false;
      offset = index + chunk.length;
      return true;
    }});
  }};
  const start = Date.now();
  while (Date.now() - start < 12000) {{
    const box = textboxForPart(partIndex);
    const actual = normalize(box?.innerText || box?.textContent || '');
    if (box && containsExpected(actual, expected)) return {{ ok: true, partIndex, actual }};
    await sleep(400);
  }}
  const box = textboxForPart(partIndex);
  const actual = normalize(box?.innerText || box?.textContent || '');
  throw new Error(`パート ${{partIndex}} の入力確認に失敗: ${{actual}}`);
}}"""


def build_x_verify_main_intent_ready_script(text: str) -> str:
    text_json = json.dumps(text, ensure_ascii=False)
    return f"""async () => {{
  const expected = {text_json};
{x_compose_dom_helpers_js()}
  const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const expectedHead = normalize(expected).slice(0, 24);
  const start = Date.now();
  while (Date.now() - start < 5000) {{
    const box = textboxForPart(1);
    const actual = normalize(box?.innerText || box?.textContent || '');
    const root = composeRoot();
    const postButton = Array.from(root.querySelectorAll('[data-testid="tweetButton"], button'))
      .find(el => isVisible(el) && isPostButtonText(el.innerText || el.textContent || ''));
    if (box && actual.length > 0 && postButton && (!expectedHead || actual.includes(expectedHead))) {{
      return {{ ok: true, actualLength: actual.length }};
    }}
    await sleep(250);
  }}
  const box = textboxForPart(1);
  const actual = normalize(box?.innerText || box?.textContent || '');
  throw new Error(`メイン投稿欄の短時間確認に失敗: ${{actual.slice(0, 80)}}`);
}}"""


def build_x_scroll_compose_to_bottom_script() -> str:
    return f"""async () => {{
{x_compose_dom_helpers_js()}
  const root = composeRoot();
  const addUnique = (items, item) => {{
    if (item && !items.includes(item)) items.push(item);
  }};
  const targets = [];
  addUnique(targets, document.scrollingElement);
  addUnique(targets, document.documentElement);
  addUnique(targets, document.body);
  let ancestor = root;
  while (ancestor) {{
    addUnique(targets, ancestor);
    ancestor = ancestor.parentElement;
  }}
  for (const el of Array.from(root.querySelectorAll('*'))) {{
    if (!isVisible(el)) continue;
    const style = window.getComputedStyle(el);
    if (/(auto|scroll)/.test(style.overflowY) && el.scrollHeight > el.clientHeight + 8) addUnique(targets, el);
  }}
  for (let i = 0; i < 4; i++) {{
    for (const target of targets) {{
      if (!target) continue;
      target.scrollTop = target.scrollHeight;
      target.dispatchEvent(new Event('scroll', {{ bubbles: true }}));
    }}
    window.scrollTo(0, document.documentElement.scrollHeight || document.body.scrollHeight || 0);
    await sleep(250);
  }}
  const bottomHints = Array.from(root.querySelectorAll('div, span, button, [role="button"]')).filter(el => {{
    if (!isVisible(el)) return false;
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    const aria = el.getAttribute('aria-label') || '';
    return /Add another post|別のポストを追加|Post all|すべてポスト|Post|投稿/.test(text) || /Add post|投稿を追加/.test(aria);
  }});
  const bottomHint = bottomHints.sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
  if (bottomHint) {{
    bottomHint.scrollIntoView({{ block: 'end', inline: 'nearest' }});
    await sleep(300);
    for (const target of targets) {{
      if (!target) continue;
      target.scrollTop = target.scrollHeight;
    }}
  }}
  await sleep(500);
  return {{ ok: true }};
}}"""


def build_x_remove_all_polls_script() -> str:
    return f"""async () => {{
{x_compose_dom_helpers_js()}
  const findRemovePollButtons = () => Array.from(composeRoot().querySelectorAll('button, [role="button"], div, span')).filter(el => {{
    if (!isVisible(el)) return false;
    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
    const aria = el.getAttribute('aria-label') || '';
    return /^(Remove poll|投票を削除)$/.test(text) || /Remove poll|投票を削除/.test(aria);
  }});
  const clickCenter = async (el) => {{
    el.scrollIntoView({{ block: 'center', inline: 'nearest' }});
    await sleep(200);
    const rect = el.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    if (typeof el.click === 'function') el.click();
    const rawTarget = document.elementFromPoint(x, y);
    const target = rawTarget instanceof Element ? rawTarget : el;
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {{
      target.dispatchEvent(new MouseEvent(type, {{
        bubbles: true,
        cancelable: true,
        clientX: x,
        clientY: y,
        view: window,
      }}));
    }}
    await sleep(700);
  }};
  let removed = 0;
  for (let i = 0; i < 6; i++) {{
    const buttons = findRemovePollButtons();
    if (!buttons.length) break;
    await clickCenter(buttons[buttons.length - 1]);
    removed += 1;
  }}
  const remaining = findRemovePollButtons().length;
  if (remaining) throw new Error(`Remove poll が残っています: ${{remaining}}`);
  return {{ ok: true, removed }};
}}"""


def build_x_wait_for_image_attachment_script() -> str:
    return f"""async () => {{
{x_compose_dom_helpers_js()}
  const hasAttachment = () => {{
    const root = composeRoot();
    const attachments = root.querySelector('[data-testid="attachments"]');
    if (!isVisible(attachments)) return false;
    const mediaGroup = attachments.querySelector('[role="group"][aria-label="Media"]') || attachments;
    const hasMediaControl = Array.from(attachments.querySelectorAll('button, [role="button"], a')).some(el => {{
      const aria = el.getAttribute('aria-label') || '';
      const testId = el.getAttribute('data-testid') || '';
      const text = (el.innerText || el.textContent || '').trim();
      return isVisible(el) && (/Remove media|Edit media|Flag sensitive media/i.test(aria) || /altTextWrapper|tagPeopleLabel/.test(testId) || /Edit|Add description|Tag people/.test(text));
    }});
    return Array.from(mediaGroup.querySelectorAll('img')).some(img => {{
      if (!isVisible(img)) return false;
      const rect = img.getBoundingClientRect();
      const src = img.getAttribute('src') || '';
      const largeEnough = rect.width >= 80 && rect.height >= 80;
      const isUploadedBlob = src.startsWith('blob:https://x.com/');
      return largeEnough && isUploadedBlob && hasMediaControl;
    }});
  }};
  const start = Date.now();
  while (Date.now() - start < 12000) {{
    if (hasAttachment()) return {{ ok: true }};
    await sleep(500);
  }}
  throw new Error('画像ペースト後の添付プレビューが確認できません');
}}"""


def build_x_focus_main_textbox_script() -> str:
    return f"""async () => {{
{x_compose_dom_helpers_js()}
  const start = Date.now();
  while (Date.now() - start < 12000) {{
    const first = textboxForPart(1);
    if (first) {{
      first.scrollIntoView({{ block: 'center' }});
      first.click();
      first.focus();
      await sleep(500);
      return {{ ok: true }};
    }}
    await sleep(300);
  }}
  throw new Error('画像ペースト前に本文入力欄をクリックできません');
}}"""


def build_x_click_post_script() -> str:
    return f"""async () => {{
{x_compose_dom_helpers_js()}
  await sleep(1200);
  const postButton = await waitFor(() => {{
    const root = composeRoot();
    const candidates = Array.from(root.querySelectorAll('[data-testid="tweetButton"], button'));
    return candidates.reverse().find(el => isVisible(el) && enabled(el) && isPostButtonText(el.innerText || el.textContent || ''));
  }}, '投稿ボタン');
  postButton.click();
  return {{ ok: true }};
}}"""


def append_quote_url_to_text(text: str, quote_url: str) -> str:
    clean_url = (quote_url or "").strip()
    clean_text = (text or "").rstrip()
    if not clean_url:
        return clean_text
    status_url_pattern = re.compile(
        r"https?://(?:x|twitter)\.com/(?:i/web/status|i/status|[^/\s]+/status)/\d+(?:\?\S*)?",
        re.IGNORECASE,
    )
    clean_text = status_url_pattern.sub("", clean_text)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()
    return f"{clean_text}\n\n{clean_url}" if clean_text else clean_url


STATUS_URL_PATTERN = re.compile(
    r"https?://(?:x|twitter)\.com/(?:i/web/status|i/status|[^/\s]+/status)/\d+(?:\?\S*)?",
    re.IGNORECASE,
)


def dedupe_status_urls_in_text(text: str) -> str:
    raw = text or ""
    urls = STATUS_URL_PATTERN.findall(raw)
    if len(urls) <= 1:
        return raw
    last_url = urls[-1].strip()
    clean_text = STATUS_URL_PATTERN.sub("", raw)
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()
    return f"{clean_text}\n\n{last_url}" if clean_text else last_url


def build_x_intent_post_url(text: str) -> str:
    return "https://x.com/intent/post?" + urlencode({"text": text or ""})


def should_use_playwright_auto_post() -> bool:
    return os.environ.get("X_AUTO_POST_DRIVER", "playwright").strip().lower() != "openclaw"


def run_playwright_auto_post(job: dict, draft: dict, parts: list[str], image_url: str) -> None:
    node = shutil.which("node")
    if not node:
        raise RuntimeError("node コマンドが見つからないため Playwright 自動投稿を実行できません")
    if not PLAYWRIGHT_AUTO_POST_SCRIPT.exists():
        raise RuntimeError(f"Playwright 自動投稿スクリプトが見つかりません: {PLAYWRIGHT_AUTO_POST_SCRIPT}")

    image_path = ""
    if image_url:
        resolved = resolve_draft_image_path(image_url)
        if not resolved:
            raise RuntimeError("画像URLをローカルファイルに解決できず、Playwrightで画像添付できません")
        image_path = str(resolved)

    screenshot_dir = Path.home() / ".openclaw" / "media" / "browser"
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "draft_id": job.get("draft_id", ""),
        "job_id": job.get("job_id", ""),
        "parts": parts,
        "image_path": image_path,
        "dry_run": os.environ.get("X_AUTO_POST_DRY_RUN", "").strip().lower() in {"1", "true", "yes"},
        "profile_dir": os.environ.get("X_PLAYWRIGHT_PROFILE_DIR", str(Path.home() / ".x-post-writer-playwright" / "x-profile")),
        "screenshot_dir": str(screenshot_dir),
    }

    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="x-auto-post-", delete=False, encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False)
        payload_path = fp.name

    timeout = max(180, 60 + len(parts) * 30)
    stdout_lines: list[str] = []
    try:
        proc = subprocess.Popen(
            [node, str(PLAYWRIGHT_AUTO_POST_SCRIPT), "--payload", payload_path],
            cwd=str(PLAYWRIGHT_AUTO_POST_SCRIPT.parent),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        with _auto_post_lock:
            job["_proc"] = proc

        deadline = time.time() + timeout
        for raw in proc.stdout:
            if time.time() > deadline:
                proc.kill()
                raise RuntimeError("Playwright 自動投稿がタイムアウトしました")
            line = raw.rstrip()
            stdout_lines.append(line)
            if line:
                append_auto_post_log(job, f"Playwright: {line[:500]}")
            try:
                msg = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue

            if msg.get("status") == "awaiting_confirm":
                with _auto_post_lock:
                    job.update({
                        "status": "pending_confirm",
                        "progress": 90,
                        "message": "投稿内容を確認してください",
                        "confirm_screenshot": msg.get("screenshot", ""),
                    })
                # ユーザーの confirm/cancel を待つ（最大5分）
                confirm_deadline = time.time() + 300
                answer = None
                while time.time() < confirm_deadline:
                    with _auto_post_lock:
                        answer = job.pop("_confirm_answer", None)
                    if answer is not None:
                        break
                    time.sleep(0.5)
                if answer != "confirm":
                    proc.stdin.write("cancel\n")
                    proc.stdin.flush()
                    proc.wait(timeout=10)
                    raise RuntimeError("投稿をキャンセルしました")
                proc.stdin.write("confirm\n")
                proc.stdin.flush()
                with _auto_post_lock:
                    job.update({"status": "running", "progress": 95, "message": "投稿中です"})

        proc.wait(timeout=30)
    finally:
        try:
            Path(payload_path).unlink()
        except OSError:
            pass
        with _auto_post_lock:
            job.pop("_proc", None)

    output = "\n".join(stdout_lines)
    if proc.returncode != 0:
        raise RuntimeError(output or "Playwright 自動投稿が失敗しました")

    last_json = None
    for line in reversed(stdout_lines):
        try:
            last_json = json.loads(line)
            break
        except (json.JSONDecodeError, ValueError):
            continue
    if not last_json or not last_json.get("ok"):
        raise RuntimeError(output or "Playwright 自動投稿の結果を確認できません")
    if last_json.get("dry_run"):
        raise RuntimeError("Playwright dry-run が完了しました。投稿は実行していません")


def run_auto_post_job(job_id: str) -> None:
    with _auto_post_lock:
        job = _auto_post_jobs[job_id]
        job.update({"status": "running", "progress": 8, "message": "ブラウザ自動投稿を開始しています"})
        append_auto_post_log(job, "ブラウザ自動投稿を開始します")

    try:
        draft = fetch_draft(job["draft_id"])
        if not draft:
            raise RuntimeError("下書きが見つかりません")
        parts = [str(part.get("content") or "") for part in draft.get("parts", [])]
        if not parts or not parts[0].strip():
            raise RuntimeError("投稿本文が空です")
        original_tweet = draft.get("original_tweet") or {}
        quote_url = str(original_tweet.get("tweet_url") or "").strip()
        if job.get("as_quote") and quote_url:
            parts[0] = append_quote_url_to_text(parts[0], quote_url)
        parts[0] = dedupe_status_urls_in_text(parts[0])
        image_url = next((part.get("image_url") for part in draft.get("parts", []) if part.get("image_url")), "")

        if should_use_playwright_auto_post():
            with _auto_post_lock:
                job.update({"progress": 14, "message": "Playwright でX投稿画面を操作しています"})
                append_auto_post_log(job, "Playwright 自動投稿を使います")
            run_playwright_auto_post(job, draft, parts, image_url)
            with _auto_post_lock:
                job.update({"progress": 88, "message": "投稿済みステータスを反映しています"})
                append_auto_post_log(job, "Playwright 側の投稿ボタンをクリックしました")
            update_draft_status(job["draft_id"], "published")
            send_discord(job["draft_id"], parts[0], draft.get("x_username", ""), job.get("user_id") or None)
            with _auto_post_lock:
                job.update({"status": "completed", "progress": 100, "message": "自動投稿が完了しました"})
                append_auto_post_log(job, "自動投稿が完了しました")
            return

        run_openclaw_browser(["start"], timeout=90)
        compose_url = build_x_intent_post_url(parts[0])
        use_intent_text = len(compose_url) <= 8000
        if not use_intent_text:
            compose_url = "https://x.com/compose/post"
        with _auto_post_lock:
            job.update({"progress": 18, "message": "X の投稿画面を開いています"})
            append_auto_post_log(job, f"{compose_url.split('?')[0]} を開きます")
        try:
            run_openclaw_browser(["open", compose_url], timeout=90)
        except RuntimeError as open_error:
            if is_openclaw_tab_missing_error(str(open_error)):
                open_chrome_url(compose_url)
                append_auto_post_log(
                    job,
                    "ChromeでX投稿画面を開きました。OpenClaw Browser Relay 拡張アイコンをクリックして badge ON にしてください。",
                    level="warn",
                )
            raise

        with _auto_post_lock:
            job.update({"progress": 45, "message": "メイン投稿の文章を確認しています"})
            append_auto_post_log(job, "メイン投稿の文章を確認します" if use_intent_text else "メイン投稿の文章を入力します")
        if not use_intent_text:
            run_openclaw_browser(["evaluate", "--fn", build_x_set_main_text_script(parts[0])], timeout=90)
            run_openclaw_browser(["evaluate", "--fn", build_x_verify_part_text_script(parts[0], 1)], timeout=60)
        else:
            run_openclaw_browser(["evaluate", "--fn", build_x_verify_main_intent_ready_script(parts[0])], timeout=30)
        capture_openclaw_screenshot(job, "メイン投稿入力後")

        with _auto_post_lock:
            job.update({"progress": 68, "message": "ツリーを追加しながら各パーツを入力しています"})
            append_auto_post_log(job, f"ツリーに {max(len(parts) - 1, 0)} パーツを入力します")
        for index, part_text in enumerate(parts[1:], start=2):
            with _auto_post_lock:
                job.update({
                    "progress": min(84, 68 + (index - 1) * 5),
                    "message": f"ツリーのパート {index} を入力しています",
            })
                append_auto_post_log(job, f"パート {index} を入力します")
            run_openclaw_browser(["evaluate", "--fn", build_x_add_tree_part_script(part_text, index)], timeout=60)
            run_openclaw_browser(["evaluate", "--fn", build_x_verify_part_text_script(part_text, index)], timeout=60)
            run_openclaw_browser(["evaluate", "--fn", build_x_scroll_compose_to_bottom_script()], timeout=60)
            capture_openclaw_screenshot(job, f"パート {index} 入力後")

        with _auto_post_lock:
            job.update({"progress": 83, "message": "一時的な投票UIを削除して投稿内容を再確認しています"})
            append_auto_post_log(job, "残っている Remove poll をすべて削除します")
        run_openclaw_browser(["evaluate", "--fn", build_x_remove_all_polls_script()], timeout=60)
        with _auto_post_lock:
            append_auto_post_log(job, "全パーツの本文を投稿前に再検証します")
        for verify_index, verify_text in enumerate(parts, start=1):
            run_openclaw_browser(["evaluate", "--fn", build_x_verify_part_text_script(verify_text, verify_index)], timeout=60)

        if image_url:
            copied_image = False
            with _auto_post_lock:
                job.update({"progress": 84, "message": "最後に画像をメイン投稿へ添付しています"})
                append_auto_post_log(job, "画像添付前にメイン投稿欄へ戻ってクリックします")
            run_openclaw_browser(["evaluate", "--fn", build_x_focus_main_textbox_script()], timeout=60)
            try:
                copied_image = copy_image_to_clipboard(image_url)
            except Exception as image_error:
                append_auto_post_log(job, f"画像をクリップボードへコピーできませんでした: {image_error}", level="warn")
            if copied_image:
                with _auto_post_lock:
                    append_auto_post_log(job, "既存画像を最後にクリップボードへコピーしました")
                    append_auto_post_log(job, "画像をメイン投稿欄にペーストします")
                paste_clipboard_to_chrome()
                with _auto_post_lock:
                    job.update({"progress": 86, "message": "画像添付を確認しています"})
                    append_auto_post_log(job, "画像添付プレビューが出たか確認します")
                try:
                    run_openclaw_browser(["evaluate", "--fn", build_x_wait_for_image_attachment_script()], timeout=60)
                except RuntimeError as paste_error:
                    with _auto_post_lock:
                        append_auto_post_log(
                            job,
                            f"最後のクリップボード貼り付けで画像プレビューを確認できませんでした。file input直指定に切り替えます: {paste_error}",
                            level="warn",
                        )
                        job.update({"progress": 87, "message": "画像をfile inputへ直接添付しています"})
                    if not upload_image_to_x_file_input(job["draft_id"], image_url):
                        raise paste_error
                    run_openclaw_browser(["evaluate", "--fn", build_x_wait_for_image_attachment_script()], timeout=60)
            else:
                with _auto_post_lock:
                    append_auto_post_log(job, "画像クリップボードコピーに失敗したため、file input直指定に切り替えます", level="warn")
                    job.update({"progress": 87, "message": "画像をfile inputへ直接添付しています"})
                if not upload_image_to_x_file_input(job["draft_id"], image_url):
                    raise RuntimeError("画像URLをローカルファイルに解決できず、最後の画像添付に失敗しました")
                run_openclaw_browser(["evaluate", "--fn", build_x_wait_for_image_attachment_script()], timeout=60)
            with _auto_post_lock:
                append_auto_post_log(job, "画像添付プレビューを確認しました")
            capture_openclaw_screenshot(job, "画像添付確認後")
            run_openclaw_browser(["evaluate", "--fn", build_x_verify_part_text_script(parts[0], 1)], timeout=60)

        with _auto_post_lock:
            job.update({"progress": 90, "message": "投稿ボタンを押しています"})
            append_auto_post_log(job, "投稿ボタンをクリックします")
        capture_openclaw_screenshot(job, "投稿直前")
        run_openclaw_browser(["evaluate", "--fn", build_x_click_post_script()], timeout=60)

        with _auto_post_lock:
            job.update({"progress": 88, "message": "投稿済みステータスを反映しています"})
            append_auto_post_log(job, "X 側の投稿ボタンをクリックしました")
        update_draft_status(job["draft_id"], "published")
        send_discord(job["draft_id"], parts[0], draft.get("x_username", ""), job.get("user_id") or None)

        with _auto_post_lock:
            job.update({"status": "completed", "progress": 100, "message": "自動投稿が完了しました"})
            append_auto_post_log(job, "自動投稿が完了しました")
    except Exception as exc:
        with _auto_post_lock:
            job.update({"status": "failed", "progress": 100, "message": str(exc)[:1000]})
            append_auto_post_log(job, str(exc)[:1000], level="error")
        handle_auto_post_failure(job, exc)


def start_auto_post_job(draft_id: str, user_id: str | None = None, as_quote: bool = False) -> dict:
    with _auto_post_lock:
        active = [
            job for job in _auto_post_jobs.values()
            if job.get("draft_id") == draft_id
            and bool(job.get("as_quote")) == bool(as_quote)
            and job.get("status") in {"queued", "running"}
        ]
        if active:
            return dict(active[-1])
        job_id = f"{int(time.time() * 1000)}-{draft_id}"
        _auto_post_jobs[job_id] = {
            "job_id": job_id,
            "draft_id": draft_id,
            "user_id": user_id or "",
            "as_quote": bool(as_quote),
            "status": "queued",
            "progress": 2,
            "message": "自動投稿ジョブを作成しました",
            "logs": [],
        }
        append_auto_post_log(_auto_post_jobs[job_id], "ジョブを作成しました")
    threading.Thread(target=run_auto_post_job, args=(job_id,), daemon=True).start()
    return {"job_id": job_id, **_auto_post_jobs[job_id]}


def get_auto_post_job(job_id: str) -> dict:
    job = _auto_post_jobs.get(job_id)
    if not job:
        raise ValueError("ジョブが見つかりません")
    return dict(job)


def setting_value(key: str, user_id: str | None = None) -> str:
    if user_id:
        value = get_user_setting(user_id, key)
        if value is not None:
            return value
    return os.environ.get(key, "")


def send_discord(draft_id, main_content, x_username, user_id: str | None = None):
    webhook = setting_value("DISCORD_WEBHOOK_X_DRAFT", user_id)
    if not webhook:
        return False, "DISCORD_WEBHOOK_X_DRAFT が未設定"

    preview_url = f"{PUBLIC_URL}?draft={draft_id}"
    metadata = load_draft_metadata(str(draft_id)) or {}
    image_prompt_block = ""
    image_prompts = metadata.get("image_prompts") or []
    if image_prompts:
        first = image_prompts[0]
        image_prompt_block = (
            "\n\n**画像プロンプト案内:**\n"
            f"- file: {first.get('file_path', 'なし')}\n"
            f"- copy: {str(first.get('copy', ''))[:120]}\n"
            "```"
            f"\n{str(first.get('prompt', ''))[:700]}\n"
            "```"
        )

    message = {
        "content": (
            f"📝 **@{x_username}** の下書きが投稿準備されました\n\n"
            f"🔗 プレビュー: {preview_url}\n\n"
            f"**投稿内容（メイン）:**\n"
            f"```\n{main_content[:800]}\n```"
            f"{image_prompt_block}"
        )
    }

    body = json.dumps(message).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status in (200, 204), None
    except Exception as e:
        return False, str(e)


def get_oembed_html(tweet_url: str) -> str | None:
    """X の oEmbed API からレンダリング用 HTML を取得する。"""
    api = f"https://publish.twitter.com/oembed?url={urlquote(tweet_url)}&omit_script=true&theme=dark&dnt=true"
    try:
        req = urllib.request.Request(api, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("html") or None
    except Exception:
        return None


def resolve_local_image(path_text: str) -> Path | None:
    """許可されたローカル画像パスだけをプレビュー配信用に解決する。"""
    if not path_text:
        return None
    path = normalize_local_image_path_text(path_text)
    if not path.is_absolute():
        return None
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        return None
    if resolved.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico"}:
        return None
    for root in LOCAL_IMAGE_ROOTS:
        try:
            resolved.relative_to(root.expanduser().resolve())
            return resolved
        except ValueError:
            continue
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def cookie_header(self, name: str, value: str, *, max_age: int = 60 * 60 * 24 * 30) -> str:
        secure = " Secure;" if (self.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
        return f"{name}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax;{secure}"

    def clear_cookie_header(self, name: str) -> str:
        return f"{name}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"

    def current_session_payload(self) -> dict | None:
        cookies = parse_cookies(self.headers.get("Cookie") or "")
        return verify_session_token(cookies.get(AUTH_SESSION_COOKIE, ""))

    def current_user(self) -> dict | None:
        payload = self.current_session_payload()
        if not payload:
            return None
        try:
            return get_app_user(str(payload.get("user_id") or ""))
        except Exception:
            return None

    def current_user_id(self) -> str | None:
        user = self.current_user()
        return user["user_id"] if user else None

    def absolute_base_url(self) -> str:
        proto = self.headers.get("X-Forwarded-Proto") or ("https" if "ngrok" in (self.headers.get("Host") or "") else "http")
        host = self.headers.get("Host") or f"localhost:{PORT}"
        return f"{proto}://{host}"

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_redirect(self, location: str, headers: list[tuple[str, str]] | None = None):
        self.send_response(302)
        self.send_header("Location", location)
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()

    def send_html(self, body: str, status=200, headers: list[tuple[str, str]] | None = None):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(encoded))
        for key, value in headers or []:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(encoded)

    def send_file(self, path, content_type):
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_response(404)
            self.end_headers()

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def is_local_request(self):
        host = (self.headers.get("Host") or "").lower()
        return (
            host.startswith("localhost:")
            or host.startswith("127.0.0.1:")
            or host in ("localhost", "127.0.0.1", "[::1]", "[::1]:8765")
        )

    def require_local(self):
        if self.is_local_request():
            return True
        self.send_json({"ok": False, "error": "この操作は localhost からのみ使えます"}, 403)
        return False

    def require_auth(self):
        if not auth_enabled():
            self.send_json({"ok": False, "error": "Googleログイン設定が未完了です"}, 503)
            return False
        if self.current_user():
            return True
        self.send_json({"ok": False, "error": "ログインが必要です"}, 401)
        return False

    def is_public_api_path(self, path: str) -> bool:
        return path in ("/api/auth/me", "/api/public-url")

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def start_google_login(self):
        if not auth_enabled():
            self.send_html("<html><body><h2>Google OAuth が未設定です</h2></body></html>", 400)
            return
        state = secrets.token_urlsafe(24)
        redirect_uri = f"{self.absolute_base_url()}/auth/google/callback"
        params = {
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_OAUTH_SCOPES,
            "state": state,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "select_account",
        }
        self.send_redirect(
            "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params),
            headers=[("Set-Cookie", self.cookie_header(AUTH_STATE_COOKIE, state, max_age=600))],
        )

    def finish_google_login(self, qs: dict):
        if not auth_enabled():
            self.send_html("<html><body><h2>Google OAuth が未設定です</h2></body></html>", 400)
            return
        cookies = parse_cookies(self.headers.get("Cookie") or "")
        expected_state = cookies.get(AUTH_STATE_COOKIE, "")
        state = qs.get("state", [""])[0]
        code = qs.get("code", [""])[0]
        if not code or not expected_state or not hmac.compare_digest(state, expected_state):
            self.send_html("<html><body><h2>認証 state が一致しません</h2></body></html>", 400)
            return

        redirect_uri = f"{self.absolute_base_url()}/auth/google/callback"
        token_body = urlencode({
            "code": code,
            "client_id": os.environ["GOOGLE_OAUTH_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }).encode("utf-8")
        try:
            token_req = urllib.request.Request(
                GOOGLE_TOKEN_URL,
                data=token_body,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            with urllib.request.urlopen(token_req, timeout=10) as resp:
                token_payload = json.loads(resp.read().decode("utf-8"))
            access_token = token_payload.get("access_token")
            if not access_token:
                raise RuntimeError("access_token が返りませんでした")
            user_req = urllib.request.Request(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            with urllib.request.urlopen(user_req, timeout=10) as resp:
                profile = json.loads(resp.read().decode("utf-8"))
            user = upsert_app_user(profile)
            session = sign_session_payload({
                "user_id": user["user_id"],
                "email": user.get("email") or "",
                "exp": int(time.time()) + 60 * 60 * 24 * 30,
            })
        except Exception as exc:
            self.send_html(f"<html><body><h2>Googleログイン失敗</h2><p>{esc_html(str(exc))}</p></body></html>", 500)
            return

        self.send_redirect(
            "/",
            headers=[
                ("Set-Cookie", self.cookie_header(AUTH_SESSION_COOKIE, session)),
                ("Set-Cookie", self.clear_cookie_header(AUTH_STATE_COOKIE)),
            ],
        )

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path.startswith("/api/") and not self.is_public_api_path(path):
            if not self.require_auth():
                return

        if path in ("/", "/index.html"):
            self.send_file(PREVIEW_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/style.css":
            self.send_file(PREVIEW_DIR / "style.css", "text/css")
        elif path == "/app.js":
            self.send_file(PREVIEW_DIR / "app.js", "application/javascript")
        elif path == "/auth/google/start":
            self.start_google_login()
        elif path == "/auth/google/callback":
            self.finish_google_login(qs)
        elif path == "/auth/logout":
            self.send_redirect("/", headers=[("Set-Cookie", self.clear_cookie_header(AUTH_SESSION_COOKIE))])
        elif path == "/api/auth/me":
            user = self.current_user()
            self.send_json({
                "ok": True,
                "auth_enabled": auth_enabled(),
                "user": user,
            })
        elif path == "/api/accounts":
            self.send_json({"ok": True, "accounts": fetch_accounts()})
        elif path == "/api/env-settings":
            if not self.require_local():
                return
            user_id = self.current_user_id()
            self.send_json({
                "ok": True,
                "path": str(ENV_PATH),
                "items": list_settings_for_user(user_id),
                "storage": "user_settings" if user_id else "env",
                "user_id": user_id,
            })
        elif path == "/api/drafts":
            try:
                self.send_json(fetch_drafts(
                    qs.get("account", [""])[0].strip(),
                    limit=int(qs.get("limit", ["20"])[0] or 20),
                    offset=int(qs.get("offset", ["0"])[0] or 0),
                    query=qs.get("q", [""])[0].strip(),
                    image_filter=qs.get("image", [""])[0].strip(),
                ))
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/draft":
            draft_id = qs.get("id", [None])[0]
            if not draft_id:
                self.send_json({"error": "id パラメータが必要です"}, 400)
                return
            try:
                data = fetch_draft(draft_id)
                self.send_json(data if data else {"error": "見つかりません"}, 200 if data else 404)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)
        elif path == "/api/public-url":
            self.send_json({"url": PUBLIC_URL})
        elif path == "/api/character-settings":
            account_key = qs.get("account", [""])[0]
            fallback_url = qs.get("fallback_url", [""])[0]
            if not account_key:
                self.send_json({"error": "account パラメータが必要です"}, 400)
                return
            self.send_json({"ok": True, "setting": merged_character_setting(account_key, fallback_url)})
        elif path == "/api/image-generation/status":
            job_id = qs.get("id", [""])[0]
            if not job_id:
                self.send_json({"error": "id パラメータが必要です"}, 400)
                return
            try:
                self.send_json({"ok": True, **get_image_generation_job(job_id)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/image-prompt/rewrite/status":
            job_id = qs.get("id", [""])[0]
            if not job_id:
                self.send_json({"error": "id パラメータが必要です"}, 400)
                return
            try:
                self.send_json({"ok": True, **get_image_rewrite_job(job_id)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/auto-post/status":
            if not self.require_local():
                return
            self.send_json(
                {"ok": False, "error": "自動投稿機能は無効化されています。Xを開いて手動で投稿してください。"},
                410,
            )
        elif path == "/api/oembed":
            tweet_url = qs.get("url", [None])[0]
            if not tweet_url:
                self.send_json({"error": "url パラメータが必要です"}, 400)
                return
            html = get_oembed_html(tweet_url)
            if html:
                self.send_json({"ok": True, "html": html})
            else:
                self.send_json({"ok": False, "error": "oEmbed取得失敗"})
        elif path == "/local-image":
            image_path = resolve_local_image(qs.get("path", [""])[0])
            if not image_path:
                self.send_json({"error": "画像が見つからないか、許可されていないパスです"}, 404)
                return
            content_type = mimetypes.guess_type(str(image_path))[0] or "application/octet-stream"
            self.send_file(image_path, content_type)
        elif path == "/oauth2/callback":
            # oauth2_setup.py からのリダイレクトを受け取り、コードを一時保存する
            code  = qs.get("code",  [None])[0]
            state = qs.get("state", [None])[0]
            _oauth2_pending["code"]  = code
            _oauth2_pending["state"] = state
            body = "<html><body><h2>✅ 認証完了！このタブを閉じてターミナルに戻ってください。</h2></body></html>".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/oauth2-callback":
            # oauth2_setup.py がポーリングしてコードを取得する。取得後はクリア
            if _oauth2_pending.get("code"):
                data = dict(_oauth2_pending)
                _oauth2_pending.clear()
                self.send_json({"ok": True, **data})
            else:
                self.send_json({"ok": False})
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path.startswith("/api/") and not self.require_auth():
            return

        if path == "/api/draft":
            draft_id = qs.get("id", [None])[0]
            if not draft_id:
                self.send_json({"error": "id パラメータが必要です"}, 400)
                return
            try:
                delete_draft(draft_id)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/") and not self.require_auth():
            return

        if path == "/api/draft/part":
            body = self.read_body()
            part_id = body.get("part_id")
            content = body.get("content")
            if not part_id or content is None:
                self.send_json({"error": "part_id と content が必要です"}, 400)
                return
            try:
                ok = update_part_content(part_id, content)
                self.send_json({"ok": ok})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_PATCH(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/") and not self.require_auth():
            return

        if path == "/api/draft/status":
            body = self.read_body()
            draft_id = body.get("draft_id")
            status = body.get("status")
            if not draft_id or status not in ("draft", "published"):
                self.send_json({"error": "draft_id と status (draft|published) が必要です"}, 400)
                return
            try:
                ok = update_draft_status(draft_id, status)
                if not ok:
                    self.send_json({"ok": False, "error": "対象の下書きが見つかりません"}, 404)
                    return
                updated = fetch_draft(str(draft_id)) or {}
                self.send_json({
                    "ok": True,
                    "status": updated.get("status") or status,
                    "published_at": updated.get("published_at"),
                })
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/image-prompt":
            body = self.read_body()
            draft_id = body.get("draft_id")
            prompt = (body.get("prompt") or "").strip()
            copy_text = (body.get("copy") or "").strip()
            if not draft_id or not prompt:
                self.send_json({"error": "draft_id と prompt が必要です"}, 400)
                return
            try:
                image_prompt = update_image_prompt_metadata(draft_id, prompt, copy_text)
                self.send_json({"ok": True, "image_prompt": image_prompt})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/character-settings":
            body = self.read_body()
            account_key = (body.get("account_key") or "").strip()
            if not account_key:
                self.send_json({"error": "account_key が必要です"}, 400)
                return
            try:
                setting = update_character_setting(account_key, body)
                self.send_json({"ok": True, "setting": setting})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/env-settings":
            if not self.require_local():
                return
            body = self.read_body()
            updates = body.get("updates")
            if not isinstance(updates, dict):
                self.send_json({"ok": False, "error": "updates が必要です"}, 400)
                return
            try:
                user_id = self.current_user_id()
                if user_id:
                    result = save_user_settings(user_id, updates)
                    self.send_json({
                        "ok": True,
                        **result,
                        "path": str(ENV_PATH),
                        "storage": "user_settings",
                        "items": list_settings_for_user(user_id),
                    })
                else:
                    result = save_env_settings(updates)
                    self.send_json({"ok": True, **result, "storage": "env", "items": list_settings_for_user(None)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/") and not self.require_auth():
            return

        if path == "/api/reference-image/upload":
            if not self.require_local():
                return
            try:
                length = int(self.headers.get("Content-Length", "0") or 0)
            except ValueError:
                length = 0
            if length <= 0:
                self.send_json({"ok": False, "error": "画像ファイルが必要です"}, 400)
                return
            if length > 13 * 1024 * 1024:
                self.send_json({"ok": False, "error": "参照画像は12MB以下にしてください"}, 413)
                return
            try:
                raw_body = self.rfile.read(length)
                result = parse_reference_image_upload(self.headers.get("Content-Type", ""), raw_body)
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 400)
            return

        body = self.read_body()

        if path == "/api/notify":
            draft_id = body.get("draft_id")
            main_content = body.get("main_content", "")
            x_username = body.get("x_username", "")
            ok, err = send_discord(draft_id, main_content, x_username, self.current_user_id())
            if ok:
                self.send_json({"ok": True})
            else:
                self.send_json({"ok": False, "error": err}, 500)
        elif path == "/api/draft/text-rewrite":
            draft_id = body.get("draft_id")
            part_id = body.get("part_id")
            instruction = (body.get("instruction") or "").strip()
            remember_rule = bool(body.get("remember_rule"))
            if not draft_id or not part_id or not instruction:
                self.send_json({"error": "draft_id、part_id、instruction が必要です"}, 400)
                return
            try:
                rewritten = rewrite_draft_text_with_codex(
                    str(draft_id),
                    str(part_id),
                    instruction,
                )
                if not update_part_content(str(part_id), rewritten):
                    self.send_json({"ok": False, "error": "対象の本文パーツが見つかりません"}, 404)
                    return
                account_rule_path = ""
                account_rule_error = ""
                if remember_rule:
                    try:
                        account_rule_path = str(append_text_rewrite_rule_to_account(str(draft_id), instruction))
                    except Exception as memory_error:
                        account_rule_error = str(memory_error)
                self.send_json({
                    "ok": True,
                    "part_id": str(part_id),
                    "content": rewritten,
                    "account_rule_path": account_rule_path,
                    "account_rule_error": account_rule_error,
                })
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/draft/text-rewrite-all":
            draft_id = body.get("draft_id")
            instruction = (body.get("instruction") or "").strip()
            remember_rule = bool(body.get("remember_rule"))
            if not draft_id or not instruction:
                self.send_json({"error": "draft_id、instruction が必要です"}, 400)
                return
            try:
                rewritten_parts = rewrite_draft_all_text_with_codex(
                    str(draft_id),
                    instruction,
                )
                if not update_part_contents(rewritten_parts):
                    self.send_json({"ok": False, "error": "対象の本文パーツが見つかりません"}, 404)
                    return
                account_rule_path = ""
                account_rule_error = ""
                if remember_rule:
                    try:
                        account_rule_path = str(append_text_rewrite_rule_to_account(str(draft_id), instruction, scope="draft"))
                    except Exception as memory_error:
                        account_rule_error = str(memory_error)
                self.send_json({
                    "ok": True,
                    "parts": rewritten_parts,
                    "account_rule_path": account_rule_path,
                    "account_rule_error": account_rule_error,
                })
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/draft/part":
            draft_id = body.get("draft_id")
            content  = (body.get("content") or "").strip()
            if not draft_id or not content:
                self.send_json({"error": "draft_id と content が必要です"}, 400)
                return
            try:
                add_draft_part(draft_id, content)
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/obsidian/save":
            if not self.require_local():
                return
            draft_id = body.get("draft_id")
            if not draft_id:
                self.send_json({"error": "draft_id が必要です"}, 400)
                return
            try:
                result = save_draft_to_obsidian(str(draft_id))
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/window/split-posting":
            if not self.require_local():
                return
            target_url = (body.get("target_url") or "").strip()
            preview_url = (body.get("preview_url") or "").strip()
            try:
                result = arrange_posting_windows(target_url, preview_url)
                self.send_json({"ok": True, **result})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/image-prompt/rewrite/start":
            draft_id = body.get("draft_id")
            prompt = (body.get("prompt") or "").strip()
            instruction = (body.get("instruction") or "").strip()
            copy_text = (body.get("copy") or "").strip()
            character_reference_url = (body.get("character_reference_url") or "").strip()
            remember_rewrite_preference = bool(body.get("remember_rewrite_preference"))
            mode = "regenerate" if (body.get("mode") or "rewrite") == "regenerate" else "rewrite"
            selected_logo_names = body.get("selected_logo_names")
            if mode != "regenerate":
                self.send_json({"ok": False, "error": "画像プロンプトのAIリライトは無効です。修正依頼または画像プロンプト再生成を使ってください。"}, 410)
                return
            if selected_logo_names is not None and not isinstance(selected_logo_names, list):
                self.send_json({"error": "selected_logo_names は配列で指定してください"}, 400)
                return
            if not draft_id:
                self.send_json({"error": "draft_id が必要です"}, 400)
                return
            try:
                self.send_json({"ok": True, **start_image_rewrite_job(
                    draft_id=str(draft_id),
                    current_prompt=prompt,
                    instruction=instruction,
                    copy_text=copy_text,
                    character_reference_url=character_reference_url,
                    remember_rewrite_preference=remember_rewrite_preference,
                    mode=mode,
                    user_id=self.current_user_id(),
                    selected_logo_names=[str(name) for name in selected_logo_names] if selected_logo_names is not None else None,
                )})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/image-prompt/rewrite/cancel":
            job_id = (body.get("job_id") or "").strip()
            if not job_id:
                self.send_json({"error": "job_id が必要です"}, 400)
                return
            try:
                self.send_json({"ok": True, **cancel_image_rewrite_job(job_id)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/image-prompt/rewrite":
            draft_id = body.get("draft_id")
            prompt = (body.get("prompt") or "").strip()
            instruction = (body.get("instruction") or "").strip()
            copy_text = (body.get("copy") or "").strip()
            character_reference_url = (body.get("character_reference_url") or "").strip()
            remember_rewrite_preference = bool(body.get("remember_rewrite_preference"))
            mode = "regenerate" if (body.get("mode") or "rewrite") == "regenerate" else "rewrite"
            selected_logo_names = body.get("selected_logo_names")
            if mode != "regenerate":
                self.send_json({"ok": False, "error": "画像プロンプトのAIリライトは無効です。修正依頼または画像プロンプト再生成を使ってください。"}, 410)
                return
            if selected_logo_names is not None and not isinstance(selected_logo_names, list):
                self.send_json({"error": "selected_logo_names は配列で指定してください"}, 400)
                return
            if not draft_id:
                self.send_json({"error": "draft_id が必要です"}, 400)
                return
            try:
                rewritten = rewrite_image_prompt_with_codex(
                    draft_id=draft_id,
                    current_prompt=prompt,
                    instruction=instruction,
                    copy_text=copy_text,
                    character_reference_url=character_reference_url,
                    mode=mode,
                    user_id=self.current_user_id(),
                    selected_logo_names=[str(name) for name in selected_logo_names] if selected_logo_names is not None else None,
                )
                account_memory_path = ""
                account_memory_error = ""
                self.send_json({
                    "ok": True,
                    "prompt": rewritten,
                    "account_memory_path": account_memory_path,
                    "account_memory_error": account_memory_error,
                })
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/draft/image":
            draft_id = body.get("draft_id")
            image_path = (body.get("image_path") or "").strip()
            if not draft_id or not image_path:
                self.send_json({"error": "draft_id と image_path が必要です"}, 400)
                return
            try:
                image_url = attach_generated_image(draft_id, image_path)
                self.send_json({"ok": True, "image_url": image_url})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/draft/image/delete":
            draft_id = body.get("draft_id")
            try:
                position = int(body.get("position") or 1)
            except (TypeError, ValueError):
                position = 1
            if not draft_id:
                self.send_json({"error": "draft_id が必要です"}, 400)
                return
            try:
                ok = update_draft_image(draft_id, position, "")
                if not ok:
                    self.send_json({"ok": False, "error": "対象の画像付き投稿パーツが見つかりません"}, 404)
                    return
                self.send_json({"ok": True})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/logos/detect":
            copy_text = (body.get("copy") or "").strip()
            prompt = (body.get("prompt") or "").strip()
            if not copy_text and not prompt:
                draft_id = body.get("draft_id")
                draft = fetch_draft(str(draft_id)) if draft_id else None
                if draft:
                    first_prompt = (draft.get("image_prompts") or [{}])[0] if isinstance(draft.get("image_prompts"), list) else {}
                    copy_text = (first_prompt.get("copy") or "").strip()
                    prompt = (first_prompt.get("prompt") or "").strip()
            try:
                candidates = extract_logo_candidate_names(copy_text, prompt, user_id=self.current_user_id())
                self.send_json({"ok": True, "candidates": candidates})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/logos/fetch-register":
            tools = body.get("tools") or []
            if not isinstance(tools, list) or not tools:
                self.send_json({"error": "tools 配列が必要です"}, 400)
                return
            results = []
            for item in tools[:8]:
                if isinstance(item, str):
                    name = item.strip()
                    source_url = ""
                elif isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    source_url = str(item.get("source_url") or "").strip()
                else:
                    continue
                if not name:
                    continue
                try:
                    results.append({"ok": True, **fetch_and_register_logo(name, self.current_user_id(), source_url)})
                except Exception as exc:
                    results.append({"ok": False, "name": name, "error": str(exc)})
            self.send_json({"ok": True, "results": results})
        elif path == "/api/logos/fetch-candidates":
            tools = body.get("tools") or []
            if not isinstance(tools, list) or not tools:
                self.send_json({"error": "tools 配列が必要です"}, 400)
                return
            results = []
            for item in tools[:8]:
                if isinstance(item, str):
                    name = item.strip()
                    source_url = ""
                elif isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    source_url = str(item.get("source_url") or "").strip()
                else:
                    continue
                if not name:
                    continue
                try:
                    results.append({"ok": True, **fetch_logo_candidate(name, self.current_user_id(), source_url)})
                except Exception as exc:
                    results.append({"ok": False, "name": name, "source_url": source_url, "error": str(exc)})
            self.send_json({"ok": True, "results": results})
        elif path == "/api/logos/register":
            try:
                preset = register_logo_from_payload(self.current_user_id(), body)
                self.send_json({"ok": True, **preset})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/image-generation/start":
            draft_id = body.get("draft_id")
            prompt = (body.get("prompt") or "").strip()
            copy_text = (body.get("copy") or "").strip()
            character_reference_url = (body.get("character_reference_url") or "").strip()
            character_traits = (body.get("character_traits") or "").strip()
            character_negative = (body.get("character_negative") or "").strip()
            character_placement = (body.get("character_placement") or "").strip()
            feedback = (body.get("feedback") or "").strip()
            current_image_url = (body.get("current_image_url") or "").strip()
            reference_image_url = (body.get("reference_image_url") or "").strip()
            reference_image_path = (body.get("reference_image_path") or "").strip()
            selected_logo_names = body.get("selected_logo_names")
            if selected_logo_names is not None and not isinstance(selected_logo_names, list):
                self.send_json({"error": "selected_logo_names は配列で指定してください"}, 400)
                return
            if not draft_id or not prompt:
                self.send_json({"error": "draft_id と prompt が必要です"}, 400)
                return
            try:
                job = start_image_generation_job(
                    draft_id,
                    copy_text,
                    prompt,
                    character_reference_url,
                    character_traits,
                    character_negative,
                    character_placement,
                    feedback,
                    current_image_url,
                    reference_image_url,
                    reference_image_path,
                    self.current_user_id(),
                    selected_logo_names,
                )
                self.send_json({"ok": True, **job})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/image-generation/cancel":
            job_id = (body.get("job_id") or "").strip()
            if not job_id:
                self.send_json({"error": "job_id が必要です"}, 400)
                return
            try:
                self.send_json({"ok": True, **cancel_image_generation_job(job_id)})
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
        elif path == "/api/auto-post/start":
            if not self.require_local():
                return
            self.send_json(
                {"ok": False, "error": "自動投稿機能は無効化されています。Xを開いて手動で投稿してください。"},
                410,
            )
        elif path == "/api/auto-post/confirm":
            if not self.require_local():
                return
            self.send_json(
                {"ok": False, "error": "自動投稿機能は無効化されています。Xを開いて手動で投稿してください。"},
                410,
            )
        else:
            if path.startswith("/api/"):
                self.send_json({"ok": False, "error": f"APIが見つかりません: {path}"}, 404)
            else:
                self.send_response(404)
                self.end_headers()


if __name__ == "__main__":
    try:
        ensure_multi_user_schema()
        seed_global_logo_presets()
        print("✅ 複数ユーザー用DBスキーマを確認しました")
    except Exception as exc:
        print(f"⚠️  複数ユーザー用DBスキーマ確認をスキップ: {exc}")
    print(f"✅ プレビューサーバー起動: http://localhost:{PORT}")
    if PUBLIC_URL != f"http://localhost:{PORT}":
        print(f"🌐 公開URL: {PUBLIC_URL}")
    if not DISCORD_WEBHOOK:
        print("⚠️  DISCORD_WEBHOOK_X_DRAFT が未設定のため Discord 通知は無効です")
    print("   終了するには Ctrl+C を押してください\n")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバーを停止しました")
