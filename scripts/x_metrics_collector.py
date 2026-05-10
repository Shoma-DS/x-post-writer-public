# X APIで自分のアカウントの投稿メトリクスを取得しNeon PostgreSQLに保存するスクリプト。
# accounts_config.jsonからアカウント一覧を読み込み、投稿後72時間以内のメイン投稿を対象に
# impression/like/retweet/bookmark/replyに加え、取得可能なクリック系指標も
# スナップショットとして記録し、後から投稿量と成果の関係を分析できる形にする。
# 実行後に今日のAPI使用コスト概算を表示する。

import json
import os
import sys
import argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg2
import tweepy

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / "accounts_config.json"
sys.path.insert(0, str(SCRIPT_DIR))

# OAuth 2.0 リフレッシュトークン管理を fetch_bookmarks.py から再利用
from fetch_bookmarks import (
    build_bookmarks_client,
    refresh_oauth2_token,
)
SNAPSHOT_WINDOW_HOURS = 72  # 3日間
COST_PER_READ = 0.005  # $0.005/件（投稿読み取り、2026年2月〜従量課金制）
TWEET_FIELDS = [
    "created_at",
    "public_metrics",
    "non_public_metrics",
    "organic_metrics",
    "text",
    "in_reply_to_user_id",
    "conversation_id",
    "referenced_tweets",
]
TRACKED_POST_TYPES = ("single", "long")


def metric_value(metrics, key):
    if not metrics:
        return None
    if isinstance(metrics, dict):
        return metrics.get(key)
    return getattr(metrics, key, None)


def load_config():
    if not CONFIG_FILE.exists():
        print(f"❌ 設定ファイルが見つかりません: {CONFIG_FILE}", file=sys.stderr)
        sys.exit(1)

    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
        json_text = "\n".join(
            line for line in text.splitlines() if not line.lstrip().startswith("//")
        )
        config = json.loads(json_text)
    except json.JSONDecodeError as exc:
        print(f"❌ accounts_config.json の書き方が正しくありません: {exc}", file=sys.stderr)
        sys.exit(1)

    accounts = config.get("accounts")
    if not isinstance(accounts, list):
        print("❌ accounts_config.json に accounts 配列がありません", file=sys.stderr)
        sys.exit(1)
    return config


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


def upsert_account(cur, x_username, display_name, profile_image_url=None):
    if profile_image_url:
        cur.execute(
            """
            INSERT INTO accounts (x_username, display_name, profile_image_url)
            VALUES (%s, %s, %s)
            ON CONFLICT (x_username) DO UPDATE
              SET display_name = EXCLUDED.display_name,
                  profile_image_url = EXCLUDED.profile_image_url
            RETURNING account_id
            """,
            (x_username, display_name, profile_image_url),
        )
    else:
        cur.execute(
            """
            INSERT INTO accounts (x_username, display_name)
            VALUES (%s, %s)
            ON CONFLICT (x_username) DO UPDATE SET display_name = EXCLUDED.display_name
            RETURNING account_id
            """,
            (x_username, display_name),
        )
    return cur.fetchone()[0]


def upsert_tweet(cur, tweet_id, account_id, content, posted_at, post_type,
                 in_reply_to_tweet_id=None, conversation_id=None, in_reply_to_user_id=None):
    cur.execute(
        """
        INSERT INTO tweets (tweet_id, account_id, content, posted_at, post_type,
                            in_reply_to_tweet_id, conversation_id, in_reply_to_user_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (tweet_id) DO UPDATE
        SET account_id = EXCLUDED.account_id,
            content = EXCLUDED.content,
            posted_at = EXCLUDED.posted_at,
            post_type = EXCLUDED.post_type,
            in_reply_to_tweet_id = EXCLUDED.in_reply_to_tweet_id,
            conversation_id = EXCLUDED.conversation_id,
            in_reply_to_user_id = EXCLUDED.in_reply_to_user_id
        """,
        (tweet_id, account_id, content, posted_at, post_type,
         in_reply_to_tweet_id, conversation_id, in_reply_to_user_id),
    )


def upsert_account_daily_metrics(cur, account_id, public_metrics, today):
    """フォロワー数と累積メトリクスを account_metrics_daily に当日分として保存する。"""
    followers = public_metrics.get("followers_count") if public_metrics else None
    following = public_metrics.get("following_count") if public_metrics else None
    tweet_count = public_metrics.get("tweet_count") if public_metrics else None
    listed = public_metrics.get("listed_count") if public_metrics else None

    # 累積メトリクス: DBに保存済みの全ツイートの最新スナップショットを合算
    cur.execute(
        """
        SELECT
            COALESCE(SUM(s.impression_count), 0),
            COALESCE(SUM(s.like_count), 0),
            COALESCE(SUM(s.retweet_count), 0),
            COALESCE(SUM(s.bookmark_count), 0),
            COALESCE(SUM(s.reply_count), 0),
            COALESCE(SUM(s.quote_count), 0),
            COALESCE(SUM(s.profile_click_count), 0),
            COALESCE(SUM(s.url_link_click_count), 0),
            COALESCE(SUM(s.engagement_count), 0)
        FROM tweets t
        JOIN LATERAL (
            SELECT
                impression_count,
                like_count,
                retweet_count,
                bookmark_count,
                reply_count,
                quote_count,
                profile_click_count,
                url_link_click_count,
                engagement_count
            FROM tweet_metrics_snapshots
            WHERE tweet_id = t.tweet_id
            ORDER BY hours_after_post DESC
            LIMIT 1
        ) s ON TRUE
        WHERE t.account_id = %s
          AND t.post_type = ANY(%s)
        """,
        (account_id, list(TRACKED_POST_TYPES)),
    )
    row = cur.fetchone()
    if row:
        cum_imp, cum_like, cum_rt, cum_bm, cum_reply, cum_quote, cum_profile_click, cum_url_click, cum_engagement = row
    else:
        cum_imp = cum_like = cum_rt = cum_bm = cum_reply = cum_quote = cum_profile_click = cum_url_click = cum_engagement = 0

    cur.execute(
        """
        SELECT
            COUNT(*) FILTER (WHERE post_type = ANY(%s)),
            COUNT(*) FILTER (WHERE post_type = 'reply'),
            COUNT(*) FILTER (WHERE post_type = 'thread'),
            COUNT(*) FILTER (WHERE post_type = 'long')
        FROM tweets
        WHERE account_id = %s
          AND posted_at >= %s::date
          AND posted_at < (%s::date + INTERVAL '1 day')
        """,
        (list(TRACKED_POST_TYPES), account_id, today, today),
    )
    post_row = cur.fetchone()
    daily_posts, daily_replies, daily_threads, daily_longs = post_row if post_row else (0, 0, 0, 0)

    cur.execute(
        """
        INSERT INTO account_metrics_daily
          (account_id, snapshot_date, followers_count, following_count,
           tweet_count, listed_count,
           cumulative_impressions, cumulative_likes, cumulative_retweets, cumulative_bookmarks,
           cumulative_replies, cumulative_quotes, cumulative_profile_clicks, cumulative_url_link_clicks,
           cumulative_engagements, daily_post_count, daily_reply_count, daily_thread_count, daily_long_count)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (account_id, snapshot_date) DO UPDATE
          SET followers_count       = EXCLUDED.followers_count,
              following_count       = EXCLUDED.following_count,
              tweet_count           = EXCLUDED.tweet_count,
              listed_count          = EXCLUDED.listed_count,
              cumulative_impressions= EXCLUDED.cumulative_impressions,
              cumulative_likes      = EXCLUDED.cumulative_likes,
              cumulative_retweets   = EXCLUDED.cumulative_retweets,
              cumulative_bookmarks  = EXCLUDED.cumulative_bookmarks,
              cumulative_replies    = EXCLUDED.cumulative_replies,
              cumulative_quotes     = EXCLUDED.cumulative_quotes,
              cumulative_profile_clicks = EXCLUDED.cumulative_profile_clicks,
              cumulative_url_link_clicks = EXCLUDED.cumulative_url_link_clicks,
              cumulative_engagements = EXCLUDED.cumulative_engagements,
              daily_post_count      = EXCLUDED.daily_post_count,
              daily_reply_count     = EXCLUDED.daily_reply_count,
              daily_thread_count    = EXCLUDED.daily_thread_count,
              daily_long_count      = EXCLUDED.daily_long_count
        """,
        (account_id, today, followers, following, tweet_count, listed,
         cum_imp, cum_like, cum_rt, cum_bm, cum_reply, cum_quote,
         cum_profile_click, cum_url_click, cum_engagement,
         daily_posts, daily_replies, daily_threads, daily_longs),
    )


def insert_snapshot(cur, tweet_id, hours_after, public_metrics, non_public_metrics=None, organic_metrics=None):
    cur.execute(
        """
        INSERT INTO tweet_metrics_snapshots
          (tweet_id, snapshot_at, hours_after_post,
           impression_count, like_count, retweet_count, bookmark_count, reply_count, quote_count,
           profile_click_count, url_link_click_count, engagement_count,
           organic_impression_count, organic_like_count, organic_retweet_count, organic_reply_count,
           organic_profile_click_count, organic_url_link_click_count)
        VALUES (%s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            tweet_id,
            round(hours_after, 2),
            metric_value(public_metrics, "impression_count"),
            metric_value(public_metrics, "like_count"),
            metric_value(public_metrics, "retweet_count"),
            metric_value(public_metrics, "bookmark_count"),
            metric_value(public_metrics, "reply_count"),
            metric_value(public_metrics, "quote_count"),
            metric_value(non_public_metrics, "user_profile_clicks"),
            metric_value(non_public_metrics, "url_link_clicks"),
            metric_value(non_public_metrics, "engagements"),
            metric_value(organic_metrics, "impression_count"),
            metric_value(organic_metrics, "like_count"),
            metric_value(organic_metrics, "retweet_count"),
            metric_value(organic_metrics, "reply_count"),
            metric_value(organic_metrics, "user_profile_clicks"),
            metric_value(organic_metrics, "url_link_clicks"),
        ),
    )


def should_skip_tweets_api(cur, account_id, now) -> bool:
    """DBの状態からツイートAPI取得をスキップすべきか判断する。
    初日（投稿後24h以内）は3時間ごと、2日目以降は1日1回。
    対象はメイン投稿のみ。DBが空・未取得ツイートがある場合は必ず呼ぶ。
    """
    cur.execute(
        """
        SELECT
            t.tweet_id,
            EXTRACT(EPOCH FROM (NOW() - t.posted_at)) / 3600  AS hours_after,
            EXTRACT(EPOCH FROM (NOW() - MAX(s.snapshot_at))) / 3600 AS elapsed_since_last
        FROM tweets t
        LEFT JOIN tweet_metrics_snapshots s ON t.tweet_id = s.tweet_id
        WHERE t.account_id = %s
          AND t.post_type = ANY(%s)
          AND t.posted_at >= NOW() - INTERVAL '3 days'
        GROUP BY t.tweet_id, t.posted_at
        """,
        (account_id, list(TRACKED_POST_TYPES)),
    )
    rows = cur.fetchall()

    if not rows:
        return False  # DBにツイートがない → 新投稿チェックのため呼ぶ

    for _tweet_id, hours_after, elapsed_since_last in rows:
        if elapsed_since_last is None:
            return False  # スナップショット未取得のツイートがある
        interval = 3.0 if float(hours_after) <= 24 else 24.0
        if float(elapsed_since_last) >= interval:
            return False  # この投稿は次の収集タイミングに達している

    return True  # 全ツイートがまだ次の収集タイミングでない → スキップ


def build_client(api_key, api_secret, access_token, access_token_secret):
    return tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_token_secret,
        wait_on_rate_limit=True,
    )


def fetch_recent_tweets(client, user_id, since):
    tweets = []
    pagination_token = None

    while True:
        response = client.get_users_tweets(
            id=user_id,
            start_time=since,
            max_results=100,
            tweet_fields=TWEET_FIELDS,
            exclude=["replies"],
            user_auth=True,
            pagination_token=pagination_token,
        )

        page_tweets = response.data or []
        tweets.extend(page_tweets)

        meta = response.meta or {}
        pagination_token = meta.get("next_token")
        if not pagination_token:
            return tweets


def _fetch_profile_image_url(account_cfg, x_username):
    """OAuth 2.0（リフレッシュトークン対応）でプロフィール画像URLを取得して返す。失敗時は None。"""
    try:
        client = build_bookmarks_client(account_cfg)
        me = client.get_me(user_fields=["profile_image_url"], user_auth=False)
    except tweepy.Unauthorized:
        new_token, err = refresh_oauth2_token(account_cfg)
        if err:
            print(f"⚠️  @{x_username}: プロフィール画像取得スキップ（トークン更新失敗: {err}）", file=sys.stderr)
            return None
        client = build_bookmarks_client(account_cfg, access_token=new_token)
        me = client.get_me(user_fields=["profile_image_url"], user_auth=False)
    except Exception as exc:
        print(f"⚠️  @{x_username}: プロフィール画像取得スキップ（{exc}）", file=sys.stderr)
        return None

    if not me or not me.data:
        return None
    raw_img = getattr(me.data, "profile_image_url", None) or ""
    return raw_img.replace("_normal.", "_400x400.") if raw_img else None


def collect_account(account_cfg, conn, force=False):
    env_token = account_cfg["token_env"]
    env_secret = account_cfg["token_secret_env"]
    access_token = os.environ.get(env_token)
    access_token_secret = os.environ.get(env_secret)

    if not access_token or not access_token_secret:
        print(
            f"⚠️  {account_cfg['x_username']}: 環境変数 {env_token} / {env_secret} が未設定のためスキップ",
            file=sys.stderr,
        )
        return 0

    api_key = os.environ.get("X_API_KEY")
    api_secret = os.environ.get("X_API_SECRET")
    if not api_key or not api_secret:
        print("❌ X_API_KEY / X_API_SECRET が設定されていません", file=sys.stderr)
        sys.exit(1)

    client = build_client(api_key, api_secret, access_token, access_token_secret)

    try:
        # OAuth 1.0a のユーザー文脈で自分のユーザーIDとフォロワー数を取得する。
        me = client.get_me(user_fields=["name", "username", "public_metrics"], user_auth=True)
        if not me.data:
            print(f"❌ {account_cfg['x_username']}: ユーザー情報取得失敗", file=sys.stderr)
            return 0

        user_id = me.data.id
        actual_username = me.data.username or account_cfg["x_username"]
        display_name = me.data.name or actual_username

        # profile_image_url は OAuth 2.0 User Context（リフレッシュトークン対応）で取得する。
        profile_image_url = _fetch_profile_image_url(account_cfg, actual_username)

        if actual_username.lower() != account_cfg["x_username"].lower():
            print(
                f"⚠️  設定の x_username と認証アカウントが違います: "
                f"{account_cfg['x_username']} -> {actual_username}",
                file=sys.stderr,
            )

        now = datetime.now(timezone.utc)
        public_metrics = getattr(me.data, "public_metrics", None)
        today = now.astimezone(timezone(timedelta(hours=9))).date()

        with conn.cursor() as cur:
            account_id = upsert_account(cur, actual_username, display_name, profile_image_url)

            if not force and should_skip_tweets_api(cur, account_id, now):
                # フォロワーデータだけ更新してツイート取得はスキップ
                upsert_account_daily_metrics(cur, account_id, public_metrics, today)
                conn.commit()
                print(f"⏭  {actual_username}: 収集間隔未達のためスキップ（コスト節約）")
                return 0

            since = now - timedelta(hours=SNAPSHOT_WINDOW_HOURS)
            tweets = fetch_recent_tweets(client, user_id, since)
            read_count = len(tweets)

            for tweet in tweets:
                posted_at = tweet.created_at
                if posted_at is None:
                    continue

                hours_after = (now - posted_at).total_seconds() / 3600
                if hours_after > SNAPSHOT_WINDOW_HOURS:
                    continue

                # スレッド・リプライ判定
                in_reply_to_user_id = str(tweet.in_reply_to_user_id) if tweet.in_reply_to_user_id else None
                conversation_id = str(tweet.conversation_id) if tweet.conversation_id else None
                # referenced_tweets から返信元のツイートIDを取得
                in_reply_to_tweet_id = None
                if tweet.referenced_tweets:
                    for ref in tweet.referenced_tweets:
                        if ref.type == "replied_to":
                            in_reply_to_tweet_id = str(ref.id)
                            break

                if in_reply_to_user_id and in_reply_to_user_id != str(user_id):
                    # 他人へのリプライ → 除外
                    post_type = "reply"
                elif in_reply_to_tweet_id:
                    # 自分のスレッドへの続き
                    post_type = "thread"
                elif len(tweet.text) > 200:
                    post_type = "long"
                else:
                    post_type = "single"

                upsert_tweet(cur, tweet.id, account_id, tweet.text, posted_at, post_type,
                             in_reply_to_tweet_id, conversation_id, in_reply_to_user_id)

                if post_type not in TRACKED_POST_TYPES:
                    continue

                insert_snapshot(
                    cur,
                    tweet.id,
                    hours_after,
                    getattr(tweet, "public_metrics", None),
                    getattr(tweet, "non_public_metrics", None),
                    getattr(tweet, "organic_metrics", None),
                )

            # フォロワー推移と累積メトリクスを当日分として保存
            upsert_account_daily_metrics(cur, account_id, public_metrics, today)
            conn.commit()
    except tweepy.TooManyRequests as exc:
        conn.rollback()
        print(f"❌ {account_cfg['x_username']}: API制限に達しました: {exc}", file=sys.stderr)
        return 0
    except (
        tweepy.BadRequest,
        tweepy.Forbidden,
        tweepy.NotFound,
        tweepy.TwitterServerError,
        tweepy.Unauthorized,
        tweepy.TweepyException,
    ) as exc:
        conn.rollback()
        print(f"❌ {account_cfg['x_username']}: X API エラー: {exc}", file=sys.stderr)
        return 0
    except psycopg2.Error as exc:
        conn.rollback()
        print(f"❌ {account_cfg['x_username']}: DB書き込みエラー: {exc}", file=sys.stderr)
        return 0

    print(f"✅ {actual_username}: {read_count} 件処理完了")
    return read_count


def build_parser():
    parser = argparse.ArgumentParser(description="Collect X post metrics into Neon PostgreSQL")
    parser.add_argument("--force", action="store_true", help="収集間隔に関係なくX APIを呼び、最新スナップショットを保存する")
    return parser


def main():
    args = build_parser().parse_args()
    config = load_config()
    conn = get_db_conn()

    total_reads = 0
    try:
        for account in config["accounts"]:
            print(f"\n▶ {account['x_username']} を処理中...")
            total_reads += collect_account(account, conn, force=args.force)
    finally:
        conn.close()

    cost_usd = total_reads * COST_PER_READ
    cost_jpy = cost_usd * 150
    print(f"\n💰 今回のAPI使用コスト概算: ${cost_usd:.4f}（約{cost_jpy:.1f}円）/ 取得件数: {total_reads}件")


if __name__ == "__main__":
    main()
