# fixtures

収集モジュールが `--fixtures` で読む、保存済みの応答です。
**ネットワークなしで収集ロジックを通しで動かす**ためのものです。

```bash
# RSS 側
python3 -m collectors.feeds --fixtures tests/fixtures \
  --generated-at 2026-08-13T06:00:00+09:00

# マーケット側
python3 -m collectors.market --fixtures tests/fixtures
```

RSS 側では `--generated-at` を必ず付けてください。24 時間フィルタの基準がこれです。
省略すると現在時刻が基準になり、収録した記事が全部「古い」と判定されて空になります。
マーケット側は日付に依存しないので不要です。

## ファイル名の決まり

- RSS: `{セクション id}__{フィード name}.xml` — `feeds.yaml` の値から決まる
- マーケット: `market__{銘柄記号}.json` — `market.yaml` の値から決まる

いずれもファイル名に使えない文字は `_` になります（`^N225` → `market__N225.json`）。
`name` や `symbol` を変えたらファイル名も変えてください。対応するファイルが無い
場合は `NO FIXTURE` として読み飛ばされます（落ちません）。

## 現物の取り込み

Raspberry Pi 上で実際の応答を取ってきて、そのまま fixture にできます。

```bash
python3 -m collectors.feeds  --check --save-fixtures tests/fixtures
python3 -m collectors.market --check --save-fixtures tests/fixtures
```

購読先や銘柄を変えたとき、あるいは「本番では動いていないのに fixture では通る」
という状況になったときは、これで現物に入れ替えてください。

## いま収録しているもの

意図的に落とし穴を踏ませてあります。各ファイル冒頭のコメントに、その fixture が
どの分岐を通すかを書いています。

| ファイル | 通す分岐 |
|---|---|
| `news__NHK_主要.xml` | RFC822 の TZ 付き日時 / 相対 URL の絶対化 / 24 時間より前の記事の除外 |
| `game__AUTOMATON.xml` | Atom で `published` が無く `updated` だけ / タイトルの HTML エンティティ |
| `ai__Hacker_News.xml` | TZ なし日時を UTC とみなす / 日時欄そのものが無いエントリ |
| `ai__はてブ_テクノロジー.xml` | TZ なし日時を JST とみなす / JST か UTC かで結果が変わる境界のケース / 別フィードと同じ記事を指す重複 |
| `news__Google_News.xml` | 見出し末尾の `- 媒体名` を落とした重複検出。**現在 Google News は `enabled: false` なので読まれません**。復活させたときのために残しています |

`4Gamer` / `Game*Spark` / `arXiv cs.AI` の fixture は**わざと置いていません**。
一部のフィードが取れなくても他が返ることを、実行するたびに確認するためです。

## 期待される結果

上のコマンドで、news 2 件 / game 1 件 / ai 3 件の計 6 件になります。確認どころ:

- NHK の 2 件目の URL が `https://www3.nhk.or.jp/news/html/20260813/k0002.html` に
  絶対化されている
- ログに `重複として 1 件を除外しました` が出ている。はてブが Hacker News と
  同じ記事を `?utm_source=hatena` 付きで持っているが、追跡パラメータを外した
  うえで同一と判定され、先に宣言した Hacker News 側が残る
- はてブの「境界のケース」が入っていない（`assume_timezone: Asia/Tokyo` が効いている）
- `Show HN:` の `published_at` が `null` で、AI セクションの末尾にある
- `4Gamer` などが `NO FIXTURE` で警告になるが、他のフィードの結果は返っている

## マーケット側

| ファイル | 通す分岐 |
|---|---|
| `market__N225.json` | 日足がそろった通常のケース |
| `market__GSPC.json` | 休場日の `null` が混ざったケース。`null` を除いた末尾 2 本で前日比を計算する |
| `market__USDJPY_X.json` | 日足が 1 本も確定していないケース。`meta` の値で補う |
| `market__TPX.json` | 記号が存在しないケース。この 1 銘柄だけ落として他は返る |

`python3 -m collectors.market --fixtures tests/fixtures` で 4 銘柄中 3 件になります。
値は `data/sample_digest.json` と一致するように作ってあるので、
桁区切りと符号の付き方をそのまま見比べられます。

TOPIX が落ちるのは**意図した結果**です。1 銘柄が取れなくても他が返ることを、
実行するたびに確認するためのケースです。
