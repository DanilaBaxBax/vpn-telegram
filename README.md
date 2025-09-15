# vpn-telegram — WireGuard + Telegram боты

Готовый набор для быстрого запуска WireGuard VPN-сервера и управления им через Telegram:

- Серверный скрипт установки и управления WireGuard (добавление/удаление клиентов, экспорт конфигов, QR, статистика).
- Админ‑бот в Telegram: список клиентов с пагинацией, поиск, выдача .conf/QR, статистика, удаление.
- Пользовательский бот в Telegram: покупка подписок (опционально через Telegram Payments), промокоды, самовыдача .conf/QR, авто‑отзыв по окончании подписки.

Проект ориентирован на Debian/Ubuntu (systemd, apt). Скрипты запускаются от root.

## Структура

```
.
├── server-part/
│   └── vpn_conf.sh              # скрипт установки/управления WireGuard
├── admin-bot/
│   └── vpn_bot_admin.py         # админ-бот Telegram
├── user-bot/
│   └── vpn_bot_user.py          # пользовательский бот Telegram
├── README.md
├── LICENSE
└── readme.txt                   # краткие заметки по серверному скрипту
```

## Быстрый старт

1) Установка WireGuard сервера (root):

```bash
cp server-part/vpn_conf.sh /root/vpn_setup.sh
chmod +x /root/vpn_setup.sh

# iface/порт/подсеть/DNS можно менять флагами
/root/vpn_setup.sh install --port 51820 --subnet 10.8.0.0/24 --dns 1.1.1.1,9.9.9.9

# (опц.) Добавить клиента и сразу показать QR в терминале
/root/vpn_setup.sh add alice --qr
```

2) Поднять админ‑бота (рекомендуется в venv):

```bash
apt-get update -y && apt-get install -y python3-venv python3-pip
python3 -m venv /opt/vpn-bot/.venv
source /opt/vpn-bot/.venv/bin/activate
pip install --upgrade pip
pip install python-telegram-bot==20.7

# Запуск (важно: бот должен уметь вызывать /root/vpn_setup.sh с правами root)
export BOT_TOKEN=XXX:YYYY
export ADMIN_IDS="123456789"  # (опц.) список chat_id через запятую
sudo -E /opt/vpn-bot/.venv/bin/python admin-bot/vpn_bot_admin.py
```

3) Поднять пользовательского бота (подписки/промокоды):

```bash
source /opt/vpn-bot/.venv/bin/activate
pip install python-telegram-bot==20.7

export BOT_TOKEN=XXX:YYYY
# (опц.) онлайн‑оплата через Telegram Payments
export PAYMENT_PROVIDER_TOKEN=YOUR_PROVIDER_TOKEN
export CURRENCY=RUB                # по умолчанию RUB
export PAY_TEST_ZERO=1             # тестовый режим: инвойс на 0

sudo -E /opt/vpn-bot/.venv/bin/python user-bot/vpn_bot_user.py
```



## Серверный скрипт WireGuard

Файл: `server-part/vpn_conf.sh` (копируется в `/root/vpn_setup.sh`). Требует root. Делает:

- Устанавливает зависимости: `wireguard`, `wireguard-tools`, `qrencode`, `iptables`, `iptables-persistent`.
- Включает форвардинг, настраивает NAT через iptables; открывает UDP‑порт в UFW (если включён).
- Создаёт ключи сервера, конфиг интерфейса `wg0` (по умолчанию) и запускает `wg-quick@wg0`.
- Управляет клиентами: генерация ключей/конфигов, выдача статичных IP, QR‑код, экспорт файла, revoke.

Команды:

```bash
/root/vpn_setup.sh install [--iface wg0] [--port 51820] [--subnet 10.8.0.0/24] [--dns 1.1.1.1,9.9.9.9]
/root/vpn_setup.sh add <username> [--ip 10.8.0.X] [--qr] [--ipv6]
/root/vpn_setup.sh revoke <username> [--keep] [--purge]
/root/vpn_setup.sh list
/root/vpn_setup.sh show <username> [--qr]
/root/vpn_setup.sh export <username> [--path /dir]
/root/vpn_setup.sh status | restart
```

Флаги и директории:

- `--iface` — имя интерфейса (по умолчанию `wg0`).
- `--port` — UDP‑порт сервера.
- `--subnet` — подсеть клиентов (сервер получает `.1/24`).
- `--dns` — DNS‑серверы для клиентов (в `.conf` попадёт первый).
- Клиенты и ключи: `/etc/wireguard/clients/<username>/` (`.conf`, `qr.png`, `public.key`, `private.key`, `psk.key`, `peer.conf`).
- Ключи сервера: `/etc/wireguard/keys/server-<iface>.{key,pub}`.

Примеры:

```bash
/root/vpn_setup.sh add alice --qr           # создать и вывести QR
/root/vpn_setup.sh show alice --qr          # показать конфиг и QR позже
/root/vpn_setup.sh export alice --path /root
/root/vpn_setup.sh revoke alice             # удалить peer (и файлы по умолчанию)
```

Примечания:

- `--ipv6` добавляет `::/0` в AllowedIPs клиента (IPv6‑адрес интерфейсу скрипт не назначает).
- Повторный `add` существующего клиента ре‑активирует peer и пере‑сгенерирует QR при необходимости.

## Админ‑бот (`admin-bot/vpn_bot_admin.py`)

Возможности:

- Список клиентов с пагинацией (12 на страницу), переключатель Активные/Все, поиск `/find <mask>`.
- Карточка клиента: выдача `.conf` и `QR`, «📊 Статистика», удаление с подтверждением.
- Общая статистика: количество клиентов, онлайн за последние N минут, суммарный трафик, топ‑10.

Переменные окружения:

- `BOT_TOKEN` — обязательный токен Telegram‑бота.
- `ADMIN_IDS` — (опц.) список chat_id через запятую. Если не задан — доступ открыт всем, кто знает бота.
- `BASH` — путь к bash (по умолчанию `/bin/bash`).

Запуск (пример):

```bash
export BOT_TOKEN=XXX:YYYY
export ADMIN_IDS="123456789,987654321"
sudo -E /opt/vpn-bot/.venv/bin/python admin-bot/vpn_bot_admin.py
```

## Пользовательский бот (`user-bot/vpn_bot_user.py`)

Возможности:

- Команды: `/buy`, `/plans`, `/redeem <CODE>`, `/status`, `/myvpn`.
- Выдача `.conf`/`QR` при активной подписке; БД SQLite по адресу `/var/lib/vpn-user-bot/db.sqlite3`.
- Подписки: покупка через Telegram Payments (по желанию), промокоды, авто‑ревок просроченных пользователей.

Переменные окружения:

- `BOT_TOKEN` — обязательный.
- `PAYMENT_PROVIDER_TOKEN` — (опц.) токен провайдера платежей Telegram.
- `CURRENCY` — валюта платежа (по умолчанию `RUB`).
- `PAY_TEST_ZERO` — `1` для тестового режима (инвойсы на 0), иначе реальные цены.
- `ADMIN_IDS` — (опц.) кто может управлять промокодами (`/addpromo`, `/delpromo`, `/promoinfo`).

Запуск (пример):

```bash
export BOT_TOKEN=XXX:YYYY
export PAY_TEST_ZERO=1
# при наличии платежей:
# export PAYMENT_PROVIDER_TOKEN=YOUR_PROVIDER_TOKEN
sudo -E /opt/vpn-bot/.venv/bin/python user-bot/vpn_bot_user.py
```

## Systemd (необязательно)

Пример юнита для админ‑бота (`/etc/systemd/system/vpn-admin-bot.service`):

```ini
[Unit]
Description=VPN Admin Telegram Bot
After=network-online.target

[Service]
User=root
WorkingDirectory=/root
Environment=BOT_TOKEN=XXX:YYYY
Environment=ADMIN_IDS=123456789
ExecStart=/opt/vpn-bot/.venv/bin/python /root/vpn-telegram/admin-bot/vpn_bot_admin.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Пример юнита для пользовательского бота (`/etc/systemd/system/vpn-user-bot.service`):

```ini
[Unit]
Description=VPN User Telegram Bot
After=network-online.target

[Service]
User=root
WorkingDirectory=/root
Environment=BOT_TOKEN=XXX:YYYY
Environment=PAY_TEST_ZERO=1
# Environment=PAYMENT_PROVIDER_TOKEN=YOUR_PROVIDER_TOKEN
ExecStart=/opt/vpn-bot/.venv/bin/python /root/vpn-telegram/user-bot/vpn_bot_user.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now vpn-admin-bot.service
systemctl enable --now vpn-user-bot.service
```

## Частые вопросы

- «Бот не видит скрипт `/root/vpn_setup.sh`»: сделайте симлинк на ваш файл (`ln -s /root/vpn_conf.sh /root/vpn_setup.sh`).
- «Нет QR»: убедитесь, что установлен `qrencode` (ставится автоматически при `install`).
- «Порт закрыт»: проверьте UFW/файрвол и откройте UDP‑порт, указанный при установке.
- «Права доступа»: боты должны запускаться с root (или иметь право безпарольного `sudo` для запуска `/root/vpn_setup.sh`).

## Лицензия

См. `LICENSE`.
