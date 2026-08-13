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

## タイムゾーン

`OnCalendar` はシステムのタイムゾーンに従う。JST になっているか確認する。

```bash
timedatectl                              # Time zone: Asia/Tokyo を確認
sudo timedatectl set-timezone Asia/Tokyo # 違っていたら設定
```
