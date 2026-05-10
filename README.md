# X Post Writer

Xのブックマークや既存下書きから、投稿下書き、プレビュー、メトリクス確認を行うローカルアプリです。

## Setup

```bash
python3 -m pip install -r scripts/requirements.txt
cd scripts && npm install
cp .env.example ../.env
cp accounts_config.example.json scripts/accounts_config.json
```

`../.env` にX API、OAuth 2.0、Neon、通知用Webhookを設定してください。

## Preview

```bash
bash scripts/start_preview.sh
```

固定ngrokドメインを使う場合:

```bash
NGROK_DOMAIN="your-domain.ngrok-free.dev" bash scripts/start_preview.sh
```

## OAuth 2.0

```bash
python3 scripts/oauth2_setup.py --account SAMPLE
```

## Safety

- `.env` はコミットしないでください。
- `accounts/` には公開してよいサンプルだけを置いてください。
- `runtime/`、`node_modules/`、`__pycache__/`、`bookmarks_latest.json` は配布物に含めないでください。
