# X APIから全アカウントのプロフィール画像URLを取得してNeon DBに保存するone-shotスクリプト。
# OAuth 2.0 User Context で取得し、アクセストークンが期限切れなら自動でリフレッシュする。
# 実行: python sync_profile_images.py

import os
import sys
from pathlib import Path

import psycopg2
import tweepy

SCRIPT_DIR = Path(__file__).parent
sys.path.insert(0, str(SCRIPT_DIR))

# fetch_bookmarks.py の OAuth 2.0 管理ロジックを再利用する
from fetch_bookmarks import (
    build_bookmarks_client,
    load_config,
    refresh_oauth2_token,
)


def get_db_conn():
    url = os.environ.get("NEON_DATABASE_URL")
    if not url:
        print("❌ NEON_DATABASE_URL が設定されていません", file=sys.stderr)
        sys.exit(1)
    try:
        return psycopg2.connect(url)
    except psycopg2.Error as exc:
        print(f"❌ Neon PostgreSQL に接続できません: {exc}", file=sys.stderr)
        sys.exit(1)


def fetch_profile_image(client, x_username):
    """OAuth 2.0 クライアントでプロフィール画像URLを取得する。"""
    me = client.get_me(
        user_fields=["username", "profile_image_url"],
        user_auth=False,
    )
    if not me or not me.data:
        return None, None
    actual_username = me.data.username or x_username
    raw_img = getattr(me.data, "profile_image_url", None) or ""
    # _normal (48px) → _400x400 (400px) に変換
    profile_image_url = raw_img.replace("_normal.", "_400x400.") if raw_img else None
    return actual_username, profile_image_url


def save_profile_image(conn, actual_username, profile_image_url):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE accounts SET profile_image_url = %s WHERE LOWER(x_username) = LOWER(%s)",
            (profile_image_url, actual_username),
        )
        conn.commit()


def sync_account(account_cfg, conn):
    x_username = account_cfg["x_username"]
    account_id = account_cfg["id"]

    # OAuth 2.0 クライアントを構築
    client = build_bookmarks_client(account_cfg)

    try:
        actual_username, profile_image_url = fetch_profile_image(client, x_username)
    except tweepy.Unauthorized:
        # アクセストークン期限切れ → リフレッシュして1回リトライ
        new_token, err = refresh_oauth2_token(account_cfg)
        if err:
            print(
                f"❌ @{x_username}: トークン自動更新失敗: {err}\n"
                f"   以下で再認証してください:\n"
                f"   python \"$X_POST_WRITER_ROOT/scripts/oauth2_setup.py\" --account {account_id}",
                file=sys.stderr,
            )
            return
        client = build_bookmarks_client(account_cfg, access_token=new_token)
        actual_username, profile_image_url = fetch_profile_image(client, x_username)

    if not profile_image_url:
        print(f"⚠️  @{x_username}: プロフィール画像URLを取得できませんでした")
        return

    try:
        save_profile_image(conn, actual_username, profile_image_url)
        print(f"✅ @{actual_username}: アイコンを設定しました ({profile_image_url})")
    except psycopg2.Error as exc:
        conn.rollback()
        print(f"❌ @{x_username}: DB保存エラー: {exc}", file=sys.stderr)


def main():
    config = load_config()
    conn = get_db_conn()

    try:
        for account in config["accounts"]:
            print(f"\n▶ {account['x_username']} を処理中...")
            sync_account(account, conn)
    finally:
        conn.close()

    print("\n✅ 全アカウントの処理が完了しました。")


if __name__ == "__main__":
    main()
