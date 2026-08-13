# HANDOFF — 現在地と次の作業

> このファイルは一時的な引き継ぎメモです。収集モジュールが完成したら削除して構いません。
> 恒久的な設計制約は `CLAUDE.md` にあります。**まずそちらを読んでください。**

---

## 0. 前提の確認

以下がリポジトリにコミット済みであることを確認してください。

```
CLAUDE.md
.gitignore
.env.example
requirements.txt
collectors/__init__.py
publishers/__init__.py
publishers/discord.py          ← 実装済み・動作確認済み
config/                        ← 空
data/sample_digest.json
deploy/daily-digest.service
deploy/daily-digest.timer
deploy/README.md
```

不足がある場合は `daily-digest-scaffold.tar.gz` を展開して補完してください。

動作確認コマンド:

```bash
python3 -m publishers.discord data/sample_digest.json --dry-run
# 期待される出力(stderr): [dry-run] embeds=4 chars=950/6000
```

---

## 1. このプロジェクトが解決したい問題

作者は毎朝、時事ニュース・マーケット・ゲーム・AI の情報をまとめて把握したい。
**最大の要件は、通勤電車内での閲覧である。** 電波が途切れる区間があるため、
「リンクだけ送られてきて開けない」状態は失敗とみなす。

この要件が、以下のほぼ全ての設計判断の根拠になっている。

---

## 2. 決定済みの事項（再検討不要）

| 決定 | 理由 |
|---|---|
| Selenium / ヘッドレスブラウザを使わない | Raspberry Pi 4B 上で毎日回すには重く、DOM 変更で壊れる。RSS と公開 API でほぼ賄える |
| Discord へは 1 日 1 メッセージ | 分割するとモバイルのキャッシュが部分的に欠け、オフライン閲覧が破綻する |
| 配信先は Discord + PWA サイトの二段構え | Discord は日常用、PWA は Service Worker による確実なオフライン閲覧用 |
| 要約は初期スコープ外 | まずタイトル + リンクで動かす。`summary` フィールドは予約済みで、後から埋めるだけで表示される |
| 収集側と配信側は `digest.json` のみで結合 | 情報源の追加が配信側に波及しないようにするため |

**却下した案:** セクションごとに Discord へ分割投稿する案は、キャッシュ欠けのリスクから却下済み。

---

## 3. digest.json スキーマ（収集側と配信側の契約）

これが本プロジェクトの中心。実例は `data/sample_digest.json` を参照。

### ルート

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `generated_at` | string | ○ | ISO 8601、タイムゾーン付き |
| `digest_url` | string \| null | | PWA サイトの当日ページ URL |
| `sections` | array | ○ | 表示順に並べる |

### セクション

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `id` | string | ○ | `market` / `news` / `game` / `ai`。配信側の色分けキー |
| `title` | string | ○ | 表示名 |
| `emoji` | string | | 見出しの先頭に付く |
| `type` | string | ○ | `links` または `market` |
| `items` | array | ○ | 空配列可（空セクションは配信時にスキップされる） |

### items（type: links）

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `title` | string | ○ | 空文字なら配信側が破棄 |
| `url` | string | ○ | 絶対 URL。空文字なら破棄 |
| `source` | string | | 媒体名 |
| `summary` | string \| null | | 現状は常に null |
| `published_at` | string \| null | | ISO 8601 |

### items（type: market）

| フィールド | 型 | 必須 | 説明 |
|---|---|---|---|
| `name` | string | ○ | 銘柄・指数名 |
| `value` | string | ○ | 整形済みの文字列。**数値ではなく文字列**（桁区切りを収集側で行う） |
| `change` | string | | `+1.24%` / `-0.35%` のように符号必須。符号で騰落マークが決まる |

**変更する場合:** `collectors/`、`publishers/discord.py`、`data/sample_digest.json`
の三点を必ず同時に更新すること。

---

## 4. 次の作業（優先順）

### Task 1: `config/feeds.yaml` + `collectors/feeds.py`

RSS を取得し、`links` 型セクションの配列を返す。

**受け入れ条件**

- [ ] 購読先は YAML で管理し、追加・削除にコード変更が不要
- [ ] 1 つのフィードが 404 / タイムアウト / パース失敗しても、他のフィードの結果は返る
- [ ] 過去 24 時間以内の記事のみ抽出（境界は `generated_at` 基準）
- [ ] 同一 URL の重複を排除。異なる媒体が同じ記事を配信するケースがある
- [ ] 同一ホストへの連続アクセスは 1 秒以上あける
- [ ] `--dry-run` 相当で、ネットワークなしでも構造を確認できる手段があること

**購読先の初期案**（実際の URL は取得時に検証すること）

```yaml
sections:
  - id: news
    title: 時事ニュース
    emoji: "📰"
    feeds:
      - { name: NHK 主要, url: "..." }
      - { name: Google News, url: "..." }
  - id: game
    title: ゲーム
    emoji: "🎮"
    feeds:
      - { name: 4Gamer, url: "..." }
      - { name: AUTOMATON, url: "..." }
      - { name: Game*Spark, url: "..." }
  - id: ai
    title: AI
    emoji: "🤖"
    feeds:
      - { name: arXiv cs.AI, url: "..." }
      - { name: Hacker News, url: "..." }
      - { name: はてブ テクノロジー, url: "..." }
```

**既知の落とし穴**

- `published_parsed` が存在しないフィードがある。その場合は `updated_parsed`、
  それもなければ取得時刻でフォールバックする
- タイムゾーンなしの日時を返すフィードがある。JST とみなすか UTC とみなすかで
  24 時間フィルタの結果が変わる。素朴に `fromisoformat` すると naive/aware の
  比較で `TypeError` になるので注意
- 相対 URL を返すフィードがある。`urljoin` で絶対化する
- タイトルに HTML エンティティが残ることがある

---

### Task 2: `collectors/market.py`

**受け入れ条件**

- [ ] 日経平均 (`^N225`)、TOPIX、S&P500 (`^GSPC`)、USD/JPY (`USDJPY=X`) を取得
- [ ] 取得失敗した銘柄はスキップし、他の銘柄は返る
- [ ] `value` は桁区切り済みの文字列、`change` は符号付きパーセント文字列
- [ ] 休場日・データ欠損時にクラッシュしない

**投資信託について（未決事項）**

国内の公募投信は yfinance では基本的に取得できない。監視銘柄が確定していないため、
**Task 2 の初期実装では指数と為替のみとし、投信は後回しにすること。**
銘柄確定後、運用会社が公開している基準価額 CSV を取得する実装を追加する。
その際、CSV が Shift_JIS の場合があるためエンコーディングに注意。

---

### Task 3: `run_daily.py`

**受け入れ条件**

- [ ] `.env` を読み込む（`python-dotenv` を使う場合は requirements.txt に追加）
- [ ] 収集 → `data/digest.json` 出力 → Discord 配信 の順に実行
- [ ] 収集がすべて失敗した場合は配信せず、非ゼロで終了
- [ ] 一部が失敗した場合は、取得できた分だけ配信して正常終了
- [ ] 生成物を `data/archive/YYYY-MM-DD.json` に保存（`.gitignore` 済み）
- [ ] ログは journalctl で追える形式。**Webhook URL を絶対に出力しない**

---

### Task 4: `render_html.py`（PWA）

Discord とは独立に、静的 HTML を生成して既存のポータルサイト配下に出力する。

**受け入れ条件**

- [ ] `digest.json` から当日ページを生成し、`DIGEST_OUTPUT_DIR` へ出力
- [ ] Service Worker でページ本体とアセットをキャッシュし、オフラインで開ける
- [ ] スマートフォンでの一覧性を優先（1 画面でセクションを俯瞰できること）
- [ ] 過去分へのリンクを持つインデックスページ

作者の環境では Cloudflare Tunnel 経由で公開される。
**Service Worker は HTTPS でのみ動作する**点に注意。

---

## 5. 開発時の注意

- `publishers/discord.py` を変更したら、必ず `--dry-run` で確認してから実送信する
- Discord の Webhook はレート制限がある。テスト時に連投しない
- `.env` を作成したら `chmod 600` を推奨
- 実行環境は Raspberry Pi 4B。メモリと CPU に余裕はない
- 作者は QA / 品質保証の実務経験者。「壊れたときにどう振る舞うか」を
  明示した実装・説明を好む
