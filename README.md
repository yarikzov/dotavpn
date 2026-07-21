# 🚀 VPN Telegram Bot — Инструкция по установке

## Структура файлов

```
vpn_bot/
├── bot.py           — основной бот (хендлеры Telegram)
├── xui_api.py       — API-клиент для 3X-UI панели
├── crypto_api.py    — API-клиент для CryptoPay (@CryptoBot)
├── db.py            — база данных SQLite
├── requirements.txt — зависимости Python
├── .env.example     — пример переменных окружения
└── webapp/
    └── index.html   — Telegram Mini App (WebApp)
```

---

## 1. Системные требования

- Ubuntu 22.04+ / Debian 11+
- Python 3.10+
- 3X-UI панель (установленная отдельно)
- Домен с SSL для WebApp (или используйте GitHub Pages)

---

## 2. Установка зависимостей

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv nginx certbot

cd /opt
git clone ... vpn_bot   # или скопируйте файлы
cd vpn_bot

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

## 3. Настройка переменных окружения

```bash
cp .env.example .env
nano .env
```

Заполните:
- `BOT_TOKEN` — от @BotFather
- `ADMIN_ID` — ваш Telegram user ID (узнать: @userinfobot)
- `WEBAPP_URL` — HTTPS-адрес вашего webapp/index.html
- `XUI_URL` — URL панели 3X-UI (например, `http://1.2.3.4:54321`)
- `XUI_USERNAME`, `XUI_PASSWORD` — логин/пароль 3X-UI
- `XUI_INBOUND_ID` — ID inbound'а в панели (обычно 1)
- `CRYPTO_PAY_TOKEN` — токен от @CryptoBot (команда /pay → Create App)

---

## 4. Размещение WebApp

### Вариант A: nginx (рекомендуется)

```bash
sudo cp -r webapp /var/www/html/vpn-webapp
# Отредактируйте /var/www/html/vpn-webapp/index.html:
# замените BOT_USERNAME = 'YourVPNBot' на ваш username бота

# nginx конфиг:
sudo nano /etc/nginx/sites-available/vpn-webapp
```

```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;
    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;
    root /var/www/html/vpn-webapp;
    index index.html;
    location / { try_files $uri $uri/ =404; }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/vpn-webapp /etc/nginx/sites-enabled/
sudo certbot --nginx -d yourdomain.com
sudo systemctl reload nginx
```

Обновите `WEBAPP_URL=https://yourdomain.com/index.html` в `.env`.

### Вариант B: GitHub Pages (бесплатно)

1. Создайте репо, положите `webapp/index.html`
2. Включите GitHub Pages → Branch: main
3. WEBAPP_URL = `https://yourname.github.io/your-repo/index.html`

---

## 5. Настройка @BotFather

В @BotFather:
```
/newbot → создайте бота
/setmenubutton → выберите бота → укажите WEBAPP_URL → название "🚀 VPN Panel"
```

---

## 6. Настройка 3X-UI

1. Откройте панель → Inbounds → добавьте inbound VLESS + Reality
2. Запомните ID inbound'а (обычно 1), укажите в `XUI_INBOUND_ID`
3. Убедитесь что API включён: Settings → Panel Settings → Enable API = true

---

## 7. Запуск бота

```bash
source venv/bin/activate
python bot.py
```

### Автозапуск через systemd

```bash
sudo nano /etc/systemd/system/vpn-bot.service
```

```ini
[Unit]
Description=VPN Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/vpn_bot
EnvironmentFile=/opt/vpn_bot/.env
ExecStart=/opt/vpn_bot/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-bot
sudo systemctl start vpn-bot
sudo systemctl status vpn-bot
```

---

## 8. Проверка работы

1. Напишите боту `/start` — должен появиться приветственный текст и кнопка WebApp
2. Откройте WebApp — должен загрузиться интерфейс
3. Нажмите "Купить подписку" → должен создаться счёт в CryptoPay
4. После оплаты — проверьте, что клиент появился в 3X-UI панели

---

## 9. Команды бота

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню + WebApp |
| `/mystats` | Статистика трафика |
| `/admin` | Панель администратора (только ADMIN_ID) |

---

## 10. Логи

```bash
tail -f /opt/vpn_bot/bot.log
```

---

## Безопасность

- Никогда не коммитьте `.env` в репозиторий
- Используйте firewall: `ufw allow 22 && ufw allow 443 && ufw enable`
- Регулярно обновляйте 3X-UI и зависимости Python
- Бэкап БД: `cp vpn_bot.db vpn_bot.db.bak`
