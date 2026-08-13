# fixtures

`collectors/feeds.py --fixtures` が読む、保存済みの RSS/Atom です。
**ネットワークなしで収集ロジックを通しで動かす**ためのものです。

```bash
python3 -m collectors.feeds --fixtures tests/fixtures \
  --generated-at 2026-08-13T06:00:00+09:00
```

`--generated-at` は必ず付けてください。24 時間フィルタの基準がこれです。
省略すると現在時刻が基準になり、収録した記事が全部「古い」と判定されて空になります。

## ファイル名の決まり

`{セクション id}__{フィード name}.xml`。`feeds.yaml` の値から機械的に決まります
（ファイル名に使えない文字は `_` になります）。フィードの `name` を変えたら
ファイル名も変えてください。対応するファイルが無いフィードは `NO FIXTURE` として
読み飛ばされます（落ちません）。

## 現物の取り込み

Raspberry Pi 上で実際のフィードを取ってきて、そのまま fixture にできます。

```bash
python3 -m collectors.feeds --check --save-fixtures tests/fixtures
```

購読先を変えたとき、あるいは「本番では動いていないのに fixture では通る」という
状況になったときは、これで現物に入れ替えてください。

## いま収録しているもの

意図的に落とし穴を踏ませてあります。各ファイル冒頭のコメントに、その fixture が
どの分岐を通すかを書いています。

| ファイル | 通す分岐 |
|---|---|
| `news__NHK_主要.xml` | RFC822 の TZ 付き日時 / 相対 URL の絶対化 / 24 時間より前の記事の除外 |
| `news__Google_News.xml` | 中継 URL のため URL では一致しない重複を、見出しの `- 媒体名` を落として検出 / `utm_*` の除去 |
| `game__AUTOMATON.xml` | Atom で `published` が無く `updated` だけ / タイトルの HTML エンティティ |
| `ai__Hacker_News.xml` | TZ なし日時を UTC とみなす / 日時欄そのものが無いエントリ |
| `ai__はてブ_テクノロジー.xml` | TZ なし日時を JST とみなす / JST か UTC かで結果が変わる境界のケース |

`4Gamer` / `Game*Spark` / `arXiv cs.AI` の fixture は**わざと置いていません**。
一部のフィードが取れなくても他が返ることを、実行するたびに確認するためです。

## 期待される結果

上のコマンドで、news 3 件 / game 1 件 / ai 3 件の計 7 件になります。確認どころ:

- NHK の 2 件目の URL が `https://www3.nhk.or.jp/news/html/20260813/k0002.html` に
  絶対化されている
- 「政府、半導体分野への追加投資を決定」が **NHK 側の直リンク 1 件だけ**で、
  Google News 側が消えている（`feeds.yaml` で NHK を先に宣言しているため）
- はてブの「境界のケース」が入っていない（`assume_timezone: Asia/Tokyo` が効いている）
- `Show HN:` の `published_at` が `null` で、AI セクションの末尾にある
