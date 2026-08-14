# Raspberry Pi へのデプロイ

## 初回

```bash
cd /home/pi
git clone https://github.com/masashi-bayman/cho-feedly.git
cd cho-feedly

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env          # Webhook URL などを設定
chmod 600 .env     # 他ユーザーから読めないようにする
```

タイマーに載せる前に、送信せずに通しで動くか確認する。

```bash
.venv/bin/python run_daily.py --dry-run
```

収集まで走り、Discord へ送る内容が表示される。`data/digest.json` は書かれない。
ここまで通ったらタイマーに載せる。

```bash
sudo cp deploy/daily-digest.service deploy/daily-digest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daily-digest.timer
```

`User=pi` と `/home/pi/cho-feedly` を前提にしている。ユーザー名や置き場所が
違う場合は `deploy/daily-digest.service` の `User` `WorkingDirectory`
`EnvironmentFile` `ExecStart` を書き換えること。

## 更新

```bash
cd /home/pi/cho-feedly
git pull
.venv/bin/pip install -r requirements.txt   # 依存が増えたときのみ
```

サービスは oneshot なので再起動は不要。次回のタイマー起動から新しいコードが動く。

## 確認

```bash
systemctl list-timers daily-digest.timer   # 次回実行予定
sudo systemctl start daily-digest.service  # 即時実行（実際に Discord へ送る）
journalctl -u daily-digest.service -n 50   # ログ確認
```

## 終了コードの読み方

`journalctl` や `systemctl status` で見る。

| コード | 意味 | 対処 |
|---|---|---|
| 0 | 配信できた。一部の情報源が落ちていても、取れた分を配信できれば 0 | なし |
| 1 | 収集が全滅した、または配信に失敗した | 下の切り分けへ |
| 2 | 設定ファイルが読めない | `config/*.yaml` の書式を確認 |

収集が全滅したときは、どちらの情報源が落ちたかを個別に確認する。

```bash
.venv/bin/python -m collectors.feeds  --check
.venv/bin/python -m collectors.market --check
```

情報源とは無関係に切り分けたい場合は、保存済みの応答だけで通す。
これが通れば、コード側ではなくネットワークか情報源の問題。

```bash
.venv/bin/python run_daily.py --fixtures tests/fixtures \
  --generated-at 2026-08-13T06:00:00+09:00 --dry-run
```

## 生成物

| パス | 内容 |
|---|---|
| `data/digest.json` | 当日分。PWA 側もこれを読む |
| `data/archive/YYYY-MM-DD.json` | 日付ごとの控え |

いずれも `.gitignore` 済みなので `git pull` の邪魔にはならない。
書き込みは一時ファイル経由なので、途中で落ちても壊れた JSON は残らない。

## PWA サイト

### プログラムと生成物を混ぜない

置き場所は 2 つあり、役割も所有者も違う。**混ぜてはいけない。**

| | 場所 | 所有者 | 中身 | 更新方法 |
|---|---|---|---|---|
| プログラム | `/home/pi/cho-feedly` | `pi` | Python コード、`config/`、`.env` | `git pull` |
| 生成物 | `/var/www/daily` | `pi` | HTML、`sw.js`、日付ごとのページ | 毎朝 `run_daily.py` が書く |

**リポジトリを web の公開領域へコピーしてはいけない。** `.env` には Discord の
Webhook URL が入っている。公開ディレクトリへ置くと、設定次第で外から読める
場所に鍵を置くことになる。`config/*.yaml` や `data/*.json` も同様に、
外へ出す必要がないものが出てしまう。

**このプロジェクトに「一度コピーするビルド成果物」は存在しない。**
HTML は毎朝 `render_html.py` がその日の分を書き出す。だから所定の場所を
`.env` で教えるだけでよく、コピー作業は要らない。

### 出力先を作る

`/var/www/portal/` の中には置かないこと。あの領域はデプロイスクリプトが
`git reset --hard` するので、置いた生成物が消える。独立した場所にする。

```bash
sudo mkdir -p /var/www/daily
sudo chown -R pi:pi /var/www/daily
```

**所有者は `pi` にすること。`www-data` にしてはいけない。**
`run_daily.py` は systemd から `pi` として動くので、`www-data` の持ち物にすると
書き込めずに生成が失敗する。nginx は読めれば十分で、既定の権限
（ディレクトリ 755 / ファイル 644）で読める。

### .env

```
DIGEST_BASE_URL=https://自分のドメイン/daily
DIGEST_OUTPUT_DIR=/var/www/daily
```

`DIGEST_BASE_URL` は Discord のメッセージに載るリンク、
`DIGEST_OUTPUT_DIR` は実際のファイルの出力先。**この 2 つが同じ場所を指すこと。**

### nginx に配ってもらう

`/var/www/daily` は nginx の `root`（`/var/www/portal/public`）の外にあるので、
**このままでは 404 になる。** `alias` で結び付ける location を足す。

`root` を書いてある設定ファイルを探して、その `server { }` の中に追記する。

```bash
sudo grep -rn "/var/www/portal/public" /etc/nginx/
```

```nginx
location /daily/ {
    alias /var/www/daily/;
    try_files $uri $uri/ =404;
    index index.html;
}
```

`alias` の末尾のスラッシュは省略しないこと。`location /daily/` と
`alias /var/www/daily/;` で、`/daily/2026-08-14/` が
`/var/www/daily/2026-08-14/index.html` に対応する。

`try_files` を書いているのは、PHP アプリが同居しているため。これが無いと
`location /` の `try_files` がディレクトリへのアクセスを PHP のフロント
コントローラへ回してしまい、やはり 404 になる。

反映する前に必ず構文を確認する。**`syntax is ok` が出ない限り reload しないこと。**
設定を壊すと、同居している既存サイトごと落ちる。

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 確認

```bash
.venv/bin/python run_daily.py            # 生成 + 配信
ls /var/www/daily                        # index.html sw.js manifest.webmanifest 日付ディレクトリ
curl -I http://localhost/daily/          # Cloudflare を通さず nginx だけ確認。200 なら成功
```

digest.json だけから作り直せるので、表示を直したいときは収集し直さなくてよい。

```bash
.venv/bin/python render_html.py                  # data/digest.json から再生成
.venv/bin/python render_html.py --dry-run        # 書かずに、書く予定を確認
.venv/bin/python render_html.py data/archive/2026-08-13.json   # 過去分から
```

### Service Worker について

**HTTPS でのみ動作する。** Cloudflare Tunnel 経由の公開 URL では動くが、
Pi のローカル IP に `http://` で直接アクセスした場合は登録されず、
オフライン閲覧もできない。動作確認は必ず公開 URL 側で行うこと。

一度開いたページはキャッシュに積まれ、以後は圏外でも読める。キャッシュ名は
日付で変えていないので、過去に開いた日のページも消えずに残る。

まだ開いたことがないページを圏外で開くと、その旨の案内が出る。

## タイムゾーン

`OnCalendar` はシステムのタイムゾーンに従う。JST になっているか確認する。

```bash
timedatectl                              # Time zone: Asia/Tokyo を確認
sudo timedatectl set-timezone Asia/Tokyo # 違っていたら設定
```
