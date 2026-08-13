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

`.env` の `DIGEST_OUTPUT_DIR` を設定すると、`run_daily.py` が Discord へ
配信する前に静的 HTML を生成する。未設定なら生成しない（Discord だけ動く）。

```
DIGEST_BASE_URL=https://自分のドメイン/daily
DIGEST_OUTPUT_DIR=/var/www/portal/public/daily
```

`DIGEST_BASE_URL` は Discord のメッセージに載るリンクに使う。
`DIGEST_OUTPUT_DIR` へ実際のファイルが出る。両方が同じ場所を指すようにする。

**出力先は nginx が配っているディレクトリ（`root`）の配下でなければならない。**
配下でないと、生成はできてもブラウザからは 404 になる。root の場所を確認する:

```bash
sudo nginx -T 2>/dev/null | grep "root "
```

出力先は `pi` ユーザーが書ける必要がある。

```bash
sudo mkdir -p /var/www/portal/public/daily
sudo chown pi:pi /var/www/portal/public/daily
```

### ディレクトリを開いても 404 になる場合

PHP アプリが同居していると、`location /` の `try_files` が
ディレクトリへのアクセスを PHP のフロントコントローラに回してしまうことがある。
その場合は nginx の server ブロックに、静的配信を優先する location を足す。

```nginx
location /daily/ {
    try_files $uri $uri/ =404;
    index index.html;
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
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
