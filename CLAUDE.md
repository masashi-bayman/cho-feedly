# daily-digest

毎朝、ニュース・マーケット・ゲーム・AI の情報を収集し、Discord と PWA サイトへ配信する。

## 実行環境

- Raspberry Pi 4B / Raspberry Pi OS (64bit)
- Python 3.11+
- systemd timer で毎朝 06:00 (JST) に起動
- デプロイは GitHub からの `git pull`

## アーキテクチャ

```
collectors/  →  curator/  →  data/digest.json  →  publishers/
```

`digest.json` が収集側と配信側の唯一の契約。**このスキーマを変更する場合は、
collectors と publishers の両方、および data/sample_digest.json を必ず同時に更新すること。**

- `collectors/` : 情報源ごとのモジュール。1 ファイル 1 情報源が原則
- `curator/` : ローカル LLM による選別。セクション配列を受け取って返すだけの
  変換で、失敗時は入力をそのまま返す。無くても全体は動く
- `publishers/` : 出力先ごとのモジュール。収集方法を一切知らない
- `config/feeds.yaml` : 購読する RSS の一覧。コード変更なしで増減できること
- `run_daily.py` : 全体のオーケストレーション

## 設計上の制約

### 障害耐性を最優先する

情報源が 1 つ落ちても全体を止めない。収集モジュールは例外を握りつぶして
空リストを返し、ログに残すこと。「全滅させない」ことを常に優先する。

### Selenium / ヘッドレスブラウザは使わない

Pi のリソースを食い、DOM 変更で壊れやすい。
優先順位は RSS → 公開 API → requests + BeautifulSoup。
どうしても必要な場合のみ Playwright を検討する。

### Discord API の上限

超過するとリクエスト全体が 400 で失敗する。`publishers/discord.py` の
定数を参照し、送信前に必ず文字数を積算すること。

- 1 メッセージあたり Embed 10 個
- Embed 合計 6000 文字 / description 4096 文字 / field value 1024 文字

### 1 日 1 メッセージにまとめる

通勤中のオフライン閲覧が主用途。分割投稿するとキャッシュが欠ける。

### スクレイピング時のマナー

User-Agent を明示し、同一ホストへの連続リクエストは 1 秒以上あける。
robots.txt を尊重する。

## 秘匿情報

`DISCORD_WEBHOOK_URL` と `ANTHROPIC_API_KEY` は `.env` から読む。
**コード、コミットメッセージ、ログ、テストデータのいずれにも実値を書かないこと。**
エラーメッセージに Webhook URL を含めない。

## 動作確認

```bash
python3 -m publishers.discord data/sample_digest.json --dry-run
```

送信を伴う変更を加えたときは、必ず `--dry-run` で確認してから実送信する。
