#!/usr/bin/env bash
# Установка JSQUP на сервер с Ubuntu/Debian. Запуск: sudo bash install.sh
set -euo pipefail

DOMAIN="${DOMAIN:-jsqvpn.online}"
REPO="${REPO:-https://github.com/jaredsss/jsqup.git}"
BRANCH="${BRANCH:-claude/review-files-7dov7v}"
DIR="${DIR:-/opt/jsqup}"
PORT="${PORT:-8080}"
ADMIN_ID="${ADMIN_ID:-1780244880}"

if [ "$(id -u)" -ne 0 ]; then echo "Запусти через sudo"; exit 1; fi

echo "==> Пакеты"
apt-get update -q
apt-get install -y -q git python3 python3-venv nginx certbot python3-certbot-nginx

echo "==> Код"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" reset -q --hard "origin/$BRANCH"
elif [ -d "$DIR" ] && [ -n "$(ls -A "$DIR")" ]; then
  # Папка есть, но не из git: сохраняем копию и подтягиваем код поверх, база и .env остаются
  BACKUP="$DIR.bak-$(date +%Y%m%d-%H%M%S)"
  echo "    $DIR уже существует, копия сохранена в $BACKUP"
  cp -a "$DIR" "$BACKUP"
  git -C "$DIR" init -q
  git -C "$DIR" remote add origin "$REPO" 2>/dev/null || git -C "$DIR" remote set-url origin "$REPO"
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q -f -B "$BRANCH" "origin/$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi

id -u jsqup >/dev/null 2>&1 || useradd --system --home "$DIR" --shell /usr/sbin/nologin jsqup

echo "==> Python-окружение"
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

if [ ! -f "$DIR/.env" ]; then
  echo "==> Настройки"
  read -rsp "Токен бота от @BotFather: " BOT_TOKEN; echo
  [ -n "$BOT_TOKEN" ] || { echo "Токен пустой"; exit 1; }
  read -rp "Твой Telegram ID для заявок на вывод [$ADMIN_ID]: " IN_ADMIN
  cat > "$DIR/.env" <<ENV
BOT_TOKEN=$BOT_TOKEN
WEBAPP_URL=https://$DOMAIN/
ADMIN_ID=${IN_ADMIN:-$ADMIN_ID}
HOST=127.0.0.1
PORT=$PORT
# Адрес Bot API (по умолчанию https://oceanment.space, для настоящего Telegram: https://api.telegram.org)
# TG_API_BASE=https://api.telegram.org
# Маржа игр
# UPG_EDGE=0.06
# MINES_EDGE=0.06
# CRASH_EDGE=0.07
ENV
  chmod 600 "$DIR/.env"
else
  echo "==> $DIR/.env уже есть, оставляю как есть"
fi
chown -R jsqup:jsqup "$DIR"

echo "==> Служба systemd"
cat > /etc/systemd/system/jsqup.service <<UNIT
[Unit]
Description=JSQUP bot
After=network-online.target
Wants=network-online.target

[Service]
User=jsqup
WorkingDirectory=$DIR
EnvironmentFile=$DIR/.env
ExecStart=$DIR/venv/bin/python server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable -q jsqup
systemctl restart jsqup

echo "==> nginx для $DOMAIN"
cat > /etc/nginx/sites-available/jsqup <<NGINX
server {
    listen 80;
    server_name $DOMAIN;
    client_max_body_size 1m;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/jsqup /etc/nginx/sites-enabled/jsqup
nginx -t
systemctl reload nginx

echo "==> HTTPS-сертификат"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect || \
  echo "!! Сертификат не выдан. Проверь, что A-запись $DOMAIN указывает на этот сервер, и запусти: certbot --nginx -d $DOMAIN"

sleep 2
if systemctl is-active -q jsqup; then
  echo "Готово: бот запущен, мини-приложение на https://$DOMAIN/"
else
  echo "!! Бот не запустился. Логи: journalctl -u jsqup -n 50"
fi
