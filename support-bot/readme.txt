Установка зависимостей:

    sudo apt-get install -y python3-venv python3-pip
    python3 -m venv /opt/vpn-bot/.venv
    source /opt/vpn-bot/.venv/bin/activate
    pip install --upgrade pip
    pip install "python-telegram-bot==20.7"

Запуск (нужно задать ENV переменные):

    export BOT_TOKEN=xxx:yyy
    export ADMIN_IDS="123,456"         # айди саппорт-агентов (через запятую)
    export SUPPORT_NOTIFY_TARGET="@baxbax_VPN_support"  # куда слать уведомления о новых тикетах (опц.)
    /opt/vpn-bot/.venv/bin/python support-bot/vpn_bot_support.py

Команды пользователя:
- /start, /help — помощь
- /new <текст> — создать новый тикет
- просто сообщение в чат — создаст тикет, если его нет, или добавит в открытый
- /mytickets — последние тикеты

Команды саппорта (ADMIN_IDS):
- /tickets [open|all] [page] — список тикетов
- /view <id> — карточка тикета
- /reply <id> <текст> — ответ пользователю
- /close <id> — закрыть тикет

Инлайн-кнопки в уведомлениях саппорту: Ответить / Закрыть.

