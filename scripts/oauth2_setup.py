# X OAuth 2.0 User Context のアクセストークンを取得する一回限りのセットアップスクリプト。
# コールバックはプレビューサーバー（port 8765）の /oauth2/callback で受け取る。
# ngrok の別起動は不要（start_preview.sh で起動済みの ngrok をそのまま使う）。
# 使い方: python oauth2_setup.py --account GUTARA

import argparse
import json
import os
import re
import shlex
import sys
import time
import webbrowser
from pathlib import Path

import requests
import tweepy

SCRIPT_DIR  = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "accounts_config.json"
REPO_ROOT   = SCRIPT_DIR.parent
ENV_FILE    = REPO_ROOT / ".env"

NGROK_DOMAIN  = "your-domain.ngrok-free.dev"
REDIRECT_URI  = f"https://{NGROK_DOMAIN}/oauth2/callback"
PREVIEW_API   = "http://localhost:8765/api/oauth2-callback"
SCOPES        = ["bookmark.read", "tweet.read", "users.read", "offline.access"]
POLL_INTERVAL = 2    # 秒
POLL_TIMEOUT  = 180  # 秒


def load_config():
    with open(CONFIG_FILE, encoding="utf-8") as f:
        raw = "".join(l for l in f if not l.strip().startswith("//"))
        return json.loads(raw)


def load_dotenv(path=ENV_FILE):
    if not path.exists():
        return {}

    loaded = {}
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


def dotenv_quote(value):
    if value == "":
        return '""'
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\"'\"'") + "'"


def save_env_values(updates):
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    pattern = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*).*$")
    seen = set()
    next_lines = []

    for line in lines:
        match = pattern.match(line)
        if not match:
            next_lines.append(line)
            continue
        prefix, key, sep = match.groups()
        if key not in updates:
            next_lines.append(line)
            continue
        next_lines.append(f"{prefix}{key}{sep}{dotenv_quote(updates[key])}")
        seen.add(key)

    for key in sorted(set(updates) - seen):
        next_lines.append(f"{key}={dotenv_quote(updates[key])}")

    ENV_FILE.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")


def save_tokens_to_settings(account_cfg, access_token, refresh_token=None):
    account_id = account_cfg["id"]
    access_token_env = account_cfg.get("bookmarks_access_token_env") or f"X_BOOKMARKS_ACCESS_TOKEN_{account_id}"
    refresh_token_env = account_cfg.get("bookmarks_refresh_token_env") or f"X_BOOKMARKS_REFRESH_TOKEN_{account_id}"
    try:
        updates = {access_token_env: access_token}
        if refresh_token:
            updates[refresh_token_env] = refresh_token
        save_env_values(updates)
        print(".env にトークンを保存しました")
    except Exception as e:
        print(f"⚠️  .env の更新に失敗しました: {e}", file=sys.stderr)


def poll_callback():
    """プレビューサーバーから OAuth コールバックのコードを受け取るまでポーリングする。"""
    deadline = time.time() + POLL_TIMEOUT
    while time.time() < deadline:
        try:
            r = requests.get(PREVIEW_API, timeout=5)
            data = r.json()
            if data.get("ok") and data.get("code"):
                return data["code"], data.get("state")
        except requests.RequestException:
            pass
        time.sleep(POLL_INTERVAL)
    return None, None


def main():
    for key, value in load_dotenv().items():
        os.environ.setdefault(key, value)

    parser = argparse.ArgumentParser(description="X OAuth 2.0 セットアップ")
    parser.add_argument("--account", default="GUTARA", help="アカウントID")
    args = parser.parse_args()

    config = load_config()
    account_cfg = next((a for a in config["accounts"] if a["id"] == args.account), None)
    if not account_cfg:
        print(f"❌ アカウント '{args.account}' が見つかりません", file=sys.stderr)
        sys.exit(1)

    account_id = account_cfg["id"]
    client_id_env = account_cfg.get("oauth2_client_id_env") or f"X_OAUTH2_CLIENT_ID_{account_id}"
    client_secret_env = account_cfg.get("oauth2_client_secret_env") or f"X_OAUTH2_CLIENT_SECRET_{account_id}"
    client_id = os.environ.get(client_id_env) or os.environ.get("X_OAUTH2_CLIENT_ID")
    client_secret = os.environ.get(client_secret_env) or os.environ.get("X_OAUTH2_CLIENT_SECRET")

    if not client_id:
        print(f"""
❌ OAuth 2.0 Client ID が未設定です。

X Developer Portal でアプリの OAuth 2.0 を有効にして、
以下の環境変数を repo 直下の .env に追加してください:

  {client_id_env}=ここにClient IDを貼り付け
  {client_secret_env}=ここにClient Secretを貼り付け

保存してからもう一度実行してください。
""", file=sys.stderr)
        sys.exit(1)

    # プレビューサーバーが起動しているか確認
    try:
        requests.get("http://localhost:8765/api/public-url", timeout=3)
    except requests.RequestException:
        print(
            "❌ プレビューサーバーが起動していません。\n"
            "   先に以下を実行してください:\n"
            f"   bash \"$X_POST_WRITER_ROOT/scripts/start_preview.sh\"",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"🔐 @{account_cfg['x_username']} の OAuth 2.0 認証を開始します")
    print(f"   スコープ: {', '.join(SCOPES)}")
    print(f"   コールバック: {REDIRECT_URI}\n")

    handler = tweepy.OAuth2UserHandler(
        client_id=client_id,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        client_secret=client_secret or None,
    )

    auth_url = handler.get_authorization_url()

    print("🌐 ブラウザを開いてXでログイン・認証してください...")
    webbrowser.open(auth_url)
    print(f"   （ブラウザが開かない場合は以下のURLを手動でコピーしてください）")
    print(f"   {auth_url}\n")
    print(f"   認証完了まで待機中（最大{POLL_TIMEOUT}秒）...")

    code, state = poll_callback()

    if not code:
        print(f"❌ {POLL_TIMEOUT}秒以内に認証が完了しませんでした。もう一度実行してください。", file=sys.stderr)
        sys.exit(1)

    print("✅ 認証コードを受信しました。トークンを取得中...")

    try:
        token = handler.fetch_token(
            f"{REDIRECT_URI}?code={code}&state={state}"
        )
    except Exception as e:
        print(f"❌ トークン取得に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)

    access_token  = token.get("access_token")
    refresh_token = token.get("refresh_token")

    save_tokens_to_settings(account_cfg, access_token, refresh_token)

    print(f"\n✅ OAuth 2.0 認証が完了しました！")
    print(f"   アクセストークンとリフレッシュトークンを .env に保存しました。")
    print(f"   sync_profile_images.py を実行してください。\n")


if __name__ == "__main__":
    main()
