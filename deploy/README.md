# Raspberry Pi へのデプロイ

## 初回

```bash
cd /home/pi
git clone git@github.com:<user>/daily-digest.git
cd daily-digest

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env          # Webhook URL などを設定
chmod 600 .env     # 他ユーザーから読めないようにする

sudo cp deploy/daily-digest.service deploy/daily-digest.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now daily-digest.timer
```

## 更新

```bash
cd /home/pi/daily-digest
git pull
.venv/bin/pip install -r requirements.txt   # 依存が増えたときのみ
```

サービスは oneshot なので再起動は不要。次回のタイマー起動から新しいコードが動く。

## 確認

```bash
systemctl list-timers daily-digest.timer   # 次回実行予定
sudo systemctl start daily-digest.service  # 即時実行
journalctl -u daily-digest.service -n 50   # ログ確認
```

## タイムゾーン

`OnCalendar` はシステムのタイムゾーンに従う。JST になっているか確認する。

```bash
timedatectl                              # Time zone: Asia/Tokyo を確認
sudo timedatectl set-timezone Asia/Tokyo # 違っていたら設定
```
