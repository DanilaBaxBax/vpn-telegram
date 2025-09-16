#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот для управления WireGuard через /root/vpn_setup.sh

UI:
- Пагинация списка клиентов (12 на страницу), переключатель Активные/Все.
- Поиск: /find <mask>
- Карточка клиента: .conf / QR / 📊 Статистика / Удалить (с подтверждением) / ← Назад.
- Общая статистика: /stats или кнопка «📈 Общая статистика» внизу списка.
- Все вызовы bash-скрипта идут через /bin/bash.

Команды:
  /list [active|all] [page]
  /find <mask>
  /add <username> [--ip 10.8.0.X] [--ipv6]
  /revoke <username>
  /getconf <username>
  /getqr <username>
  /show <username>
  /stats
  /whoami
"""

import asyncio
import html
import logging
import os
import re
import shlex
import subprocess
from datetime import datetime
from glob import glob
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple, Optional, Dict
import sqlite3

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message,
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler,
)
from telegram.error import BadRequest

# ---------- Настройки ----------
VPN_SCRIPT = Path("/root/vpn_setup.sh")  # если другой файл — сделай симлинк на это имя
BASH = os.environ.get("BASH", "/bin/bash")  # всегда запускаем скрипт через bash
CLIENTS_DIR = Path("/etc/wireguard/clients")

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "Your_Token"  # ENV имеет приоритет
).strip()
SUPPORT_CONTACT_LINK = os.environ.get(
    "SUPPORT_CONTACT_LINK",
    "https://t.me/baxbax_VPN_support"
).strip()

# ограничение доступа (опционально): ADMIN_IDS="123,456"
_ADMIN_ENV = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = {int(x) for x in _ADMIN_ENV.split(",") if x.strip().isdigit()} if _ADMIN_ENV else set()

PAGE_SIZE = 12
ONLINE_WINDOW = 300  # секунд, считаем «онлайн», если рукопожатие было <= 5 мин назад
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

# Путь к БД пользовательского бота (если он установлен на той же машине)
USER_DB_PATH = Path("/var/lib/vpn-user-bot/db.sqlite3")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("vpn-bot")

# ---------- Утилиты ----------
def is_admin(update: Update) -> bool:
    if not ADMIN_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ADMIN_IDS

def validate_username(u: str) -> bool:
    return bool(USERNAME_RE.fullmatch(u))

async def run_cmd(args: Sequence[str], timeout: int = 180) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(shlex.quote(a) for a in args))
    return await asyncio.to_thread(
        subprocess.run,
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )

async def run_script(script_args: Sequence[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return await run_cmd([BASH, str(VPN_SCRIPT), *script_args], timeout=timeout)

def detect_iface() -> str:
    files = sorted(glob("/etc/wireguard/*.conf"))
    return Path(files[0]).stem if files else "wg0"

def client_dir(username: str) -> Path:
    return CLIENTS_DIR / username

def client_conf_path(username: str) -> Path:
    return client_dir(username) / f"{username}.conf"

def client_pubkey_path(username: str) -> Path:
    return client_dir(username) / "public.key"

def client_qr_path(username: str) -> Path:
    return client_dir(username) / "qr.png"

def list_all_clients_fs() -> List[str]:
    if not CLIENTS_DIR.exists():
        return []
    names: List[str] = []
    for d in CLIENTS_DIR.iterdir():
        if d.is_dir() and validate_username(d.name) and (d / f"{d.name}.conf").exists():
            names.append(d.name)
    names.sort(key=str.lower)
    return names

async def get_active_peer_keys() -> Tuple[str, Set[str]]:
    iface = detect_iface()
    proc = await run_cmd(["wg", "show", iface, "peers"])
    keys: Set[str] = set()
    if proc.returncode == 0 and proc.stdout.strip():
        for token in proc.stdout.replace("\n", " ").split():
            token = token.strip()
            if token:
                keys.add(token)
    return iface, keys

async def list_active_clients() -> List[str]:
    _, peer_keys = await get_active_peer_keys()
    names: List[str] = []
    for name in list_all_clients_fs():
        pub = client_pubkey_path(name)
        if pub.exists() and pub.read_text().strip() in peer_keys:
            names.append(name)
    return names

def paginate(items: Sequence[str], page: int, size: int) -> Sequence[str]:
    start = max(page - 1, 0) * size
    end = start + size
    return items[start:end]

def human_bytes(n: int) -> str:
    s = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if s < 1024.0 or unit == "TB":
            return f"{s:.2f} {unit}"
        s /= 1024.0
    return f"{n} B"

def fmt_dt(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def fmt_age(seconds: float) -> str:
    seconds = int(max(0, seconds))
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m: parts.append(f"{m}м")
    if not parts: parts.append(f"{s}с")
    return " ".join(parts)

# ---------- Разметка ----------
def build_list_markup(items: Sequence[str], page: int, total_pages: int, scope: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for name in items:
        rows.append([InlineKeyboardButton(f"{name}", callback_data=f"user:{name}:{scope}:{page}")])
    nav: List[InlineKeyboardButton] = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f"list:{scope}:{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("Дальше »", callback_data=f"list:{scope}:{page+1}"))
    if nav:
        rows.append(nav)
    # общая статистика
    rows.append([InlineKeyboardButton("📈 Общая статистика", callback_data=f"gstats:{scope}:{page}")])
    # переключатель область
    toggle_scope = "all" if scope == "active" else "active"
    rows.append([InlineKeyboardButton(f"Показать: {('Активные' if scope=='active' else 'Все')} ▸ сменить",
                                      callback_data=f"list:{toggle_scope}:1")])
    return InlineKeyboardMarkup(rows)

def build_user_markup(name: str, scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬇️ .conf",      callback_data=f"act:getconf:{name}:{scope}:{page}"),
            InlineKeyboardButton("🧾 QR",          callback_data=f"act:getqr:{name}:{scope}:{page}"),
            InlineKeyboardButton("📊 Статистика",  callback_data=f"act:stats:{name}:{scope}:{page}"),
        ],
        [   InlineKeyboardButton("🗑 Удалить",     callback_data=f"act:askrevoke:{name}:{scope}:{page}") ],
        [   InlineKeyboardButton("← Назад к списку", callback_data=f"list:{scope}:{page}") ],
    ])

def build_user_confirm_markup(name: str, scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"act:revoke:{name}:{scope}:{page}"),
            InlineKeyboardButton("✖️ Отмена",      callback_data=f"user:{name}:{scope}:{page}"),
        ]
    ])

async def notify_user_revoked_if_possible(username: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Если имя похоже на u<tg_id>, отметить подписки как 'canceled' в БД user‑бота
    и попытаться отправить пользователю уведомление.
    Ошибки не пробрасываем (чтобы UI не показывал общую ошибку).
    """
    m = re.fullmatch(r"u(\d+)", username)
    if not m:
        return
    try:
        tg_id = int(m.group(1))
    except Exception:
        return

    # Обновление БД: пометить активные как canceled
    try:
        if USER_DB_PATH.exists():
            con = sqlite3.connect(str(USER_DB_PATH))
            with con:
                con.execute("UPDATE subscriptions SET status='canceled' WHERE tg_id=? AND status='active'", (tg_id,))
            con.close()
    except Exception as e:
        log.warning("Failed to mark subscriptions canceled for tg_id=%s: %s", tg_id, e)

    # Уведомление пользователя (если он писал этому боту)
    try:
        await context.bot.send_message(
            chat_id=tg_id,
            text=(
                "Ваш доступ к VPN был отозван администратором.\n"
                f"Если вы считаете это ошибкой, напишите в саппорт: {SUPPORT_CONTACT_LINK}"
            ),
            disable_web_page_preview=True,
        )
    except Exception as e:
        # user мог не начинать чат с админ‑ботом — это нормально
        log.info("Could not DM user %s about revoke (ignored): %s", tg_id, e)

def build_stats_markup(name: str, scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↻ Обновить", callback_data=f"act:stats:{name}:{scope}:{page}")],
        [InlineKeyboardButton("← Назад",    callback_data=f"user:{name}:{scope}:{page}")],
    ])

def build_global_stats_markup(scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("↻ Обновить", callback_data=f"gstats:{scope}:{page}")],
        [InlineKeyboardButton("← Назад к списку", callback_data=f"list:{scope}:{page}")],
    ])

def list_title(scope: str, page: int, total: int, total_pages: int) -> str:
    return f"Список клиентов ({'Активные' if scope=='active' else 'Все'}): стр. {page}/{total_pages} • всего: {total}\n" \
           f"Подсказки: /find <маска>, /add <имя>, /revoke <имя>"

def ensure_single_message(msg: Message | None) -> bool:
    return msg is not None and msg.message_id is not None

# ---------- wg dump ----------
async def wg_dump_map() -> Tuple[str, Dict[str, dict]]:
    """
    Возвращает (iface, map[pubkey] -> dict(fields)) по `wg show <iface> dump`
    Поля peer-строки:
      0 public_key, 1 preshared_key, 2 endpoint, 3 allowed_ips,
      4 latest_handshake (unix), 5 rx, 6 tx, 7 persistent_keepalive
    """
    iface = detect_iface()
    proc = await run_cmd(["wg", "show", iface, "dump"])
    peers: Dict[str, dict] = {}
    if proc.returncode != 0:
        return iface, peers
    for line in proc.stdout.strip().splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 4:
            # header/interface line — пропускаем
            continue
        if len(parts) >= 8:
            pub = parts[0]
            peers[pub] = {
                "endpoint": parts[2] if parts[2] != "(none)" else "",
                "allowed_ips": parts[3],
                "latest_handshake": int(parts[4]) if parts[4].isdigit() else 0,
                "rx": int(parts[5]) if parts[5].isdigit() else 0,
                "tx": int(parts[6]) if parts[6].isdigit() else 0,
                "keepalive": int(parts[7]) if parts[7].isdigit() else 0,
            }
    return iface, peers

def map_pub_to_user() -> Dict[str, str]:
    """Сопоставление public.key -> username по каталогу клиентов."""
    mapping: Dict[str, str] = {}
    if not CLIENTS_DIR.exists():
        return mapping
    for d in CLIENTS_DIR.iterdir():
        if not d.is_dir():
            continue
        name = d.name
        pub = d / "public.key"
        if pub.exists():
            try:
                key = pub.read_text().strip()
                if key:
                    mapping[key] = name
            except Exception:
                pass
    return mapping

# ---------- Текст статистики по клиенту ----------
async def build_stats_text(username: str) -> str:
    pub_path = client_pubkey_path(username)
    conf_path = client_conf_path(username)
    if not pub_path.exists():
        return "Нет public.key — возможно, клиент удалён."
    pubkey = pub_path.read_text().strip()

    iface, dump_map = await wg_dump_map()
    st = dump_map.get(pubkey, {})
    now = datetime.now().timestamp()

    # из клиента: Address, AllowedIPs
    addr = "-"
    allowed_user = "-"
    if conf_path.exists():
        try:
            for line in conf_path.read_text().splitlines():
                if line.strip().startswith("Address"):
                    addr = line.split("=", 1)[1].strip()
                if line.strip().startswith("AllowedIPs"):
                    allowed_user = line.split("=", 1)[1].strip()
        except Exception:
            pass

    hs = st.get("latest_handshake", 0)
    if hs:
        hs_text = f"{fmt_dt(hs)} (≈ {fmt_age(now-hs)} назад)"
    else:
        hs_text = "ещё не было"

    rx = st.get("rx", 0)
    tx = st.get("tx", 0)
    total = rx + tx
    keepalive = st.get("keepalive", 0)
    keepalive_text = f"{keepalive}s" if keepalive else "выкл"
    endpoint = st.get("endpoint") or "-"

    lines = [
        f"📊 <b>Статистика: {html.escape(username)}</b>",
        f"Интерфейс: <code>{iface}</code>",
        f"Адрес клиента: <code>{html.escape(addr)}</code>",
        f"Endpoint (последний): <code>{html.escape(endpoint)}</code>",
        f"Последнее рукопожатие: {hs_text}",
        f"Трафик: RX {human_bytes(rx)} | TX {human_bytes(tx)} | Σ {human_bytes(total)}",
        f"Keepalive: {keepalive_text}",
        f"AllowedIPs (client): <code>{html.escape(allowed_user)}</code>",
    ]
    return "\n".join(lines)

# ---------- Общая статистика ----------
async def build_global_stats_text() -> str:
    all_clients = list_all_clients_fs()
    total_clients = len(all_clients)

    iface, dump_map = await wg_dump_map()
    pub2user = map_pub_to_user()
    now = datetime.now().timestamp()

    known_peers = {k: v for k, v in dump_map.items() if k in pub2user}
    peers_count = len(known_peers)

    online_recent = 0
    rx_total = 0
    tx_total = 0
    leaderboard: List[Tuple[str, int, int, int]] = []  # (username_or_key, rx, tx, hs)

    for pub, st in known_peers.items():
        rx = int(st.get("rx", 0))
        tx = int(st.get("tx", 0))
        hs = int(st.get("latest_handshake", 0))
        if hs and now - hs <= ONLINE_WINDOW:
            online_recent += 1
        rx_total += rx
        tx_total += tx
        username = pub2user.get(pub, pub[:8])
        leaderboard.append((username, rx, tx, hs))

    # топ по суммарному трафику
    leaderboard.sort(key=lambda x: (x[1] + x[2]), reverse=True)
    top = leaderboard[:10]

    lines = [
        f"📈 <b>Общая статистика</b>",
        f"Интерфейс: <code>{iface}</code>",
        f"Клиентов (всего по папкам): <b>{total_clients}</b>",
        f"Peers в wg (из известных клиентов): <b>{peers_count}</b>",
        f"Онлайн (рукопожатие ≤ {ONLINE_WINDOW//60} мин): <b>{online_recent}</b>",
        f"Σ трафик (известные): RX {human_bytes(rx_total)} | TX {human_bytes(tx_total)} | Σ {human_bytes(rx_total+tx_total)}",
        "",
        "<b>Топ-10 по трафику</b>",
    ]
    if not top:
        lines.append("— данных нет —")
    else:
        for i, (name, rx, tx, hs) in enumerate(top, 1):
            age = f"{fmt_age(now-hs)} назад" if hs else "нет рукопожатий"
            lines.append(f"{i}. {html.escape(name)} — RX {human_bytes(rx)}, TX {human_bytes(tx)}, Σ {human_bytes(rx+tx)}; HS: {age}")

    return "\n".join(lines)

# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    await update.message.reply_text(
        "Привет! Я панель управления WireGuard.\n"
        "• /list — список (пагинация)\n"
        "• /find <маска> — поиск\n"
        "• /add <username> [--ip ...] [--ipv6]\n"
        "• /revoke <username>\n"
        "• /getconf <username> /getqr <username>\n"
        "• /show <username>\n"
        "• /stats — общая статистика\n"
    )

async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    args = context.args
    scope = (args[0].lower() if args and args[0].lower() in ("active", "all") else "active")
    page = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 1

    items = await (list_active_clients() if scope == "active" else asyncio.to_thread(list_all_clients_fs))
    total = len(items)
    total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
    page = min(max(page, 1), total_pages)
    page_items = paginate(items, page, PAGE_SIZE)

    text = list_title(scope, page, total, total_pages)
    markup = build_list_markup(page_items, page, total_pages, scope)

    msg = await update.message.reply_text("Загружаю…")
    if ensure_single_message(msg):
        await msg.edit_text(text)
        await msg.edit_reply_markup(markup)

async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    if not context.args:     return await update.message.reply_text("Использование: /find <маска>")
    mask = " ".join(context.args).lower()
    all_names = await asyncio.to_thread(list_all_clients_fs)
    matches = [n for n in all_names if mask in n.lower()]
    if not matches:
        return await update.message.reply_text("Ничего не найдено.")
    matches = matches[:30]
    kb = [[InlineKeyboardButton(n, callback_data=f"user:{n}:all:1")] for n in matches]
    await update.message.reply_text(f"Найдено: {len(matches)}", reply_markup=InlineKeyboardMarkup(kb))

def _parse_add_args(args: List[str]):
    if not args: return "", []
    i = 0
    name_parts: List[str] = []
    while i < len(args) and not args[i].startswith("--"):
        name_parts.append(args[i]); i += 1
    username = "".join(name_parts)
    flags: List[str] = []
    while i < len(args):
        tok = args[i]
        if tok == "--ipv6":
            flags.append("--ipv6"); i += 1
        elif tok == "--ip":
            if i + 1 >= len(args): raise ValueError("Флаг --ip требует значение, напр.: --ip 10.8.0.10")
            flags.extend(["--ip", args[i + 1]]); i += 2
        else:
            raise ValueError(f"Неизвестный аргумент: {tok}")
    return username, flags

async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    try:
        username, flags = _parse_add_args(context.args)
    except ValueError as e:
        return await update.message.reply_text(str(e))
    if not username: return await update.message.reply_text("Использование: /add <username> [--ip ...] [--ipv6]")
    if not validate_username(username): return await update.message.reply_text("Некорректное имя (A-Za-z0-9._-).")
    await update.message.chat.send_action("typing")
    proc = await run_script(["add", username, *flags])
    if proc.returncode == 0:
        msg = proc.stdout.strip() or "Готово."
        await update.message.reply_text(f"✅ {html.escape(msg)}", parse_mode="HTML",
                                        reply_markup=build_user_markup(username, "active", 1))
    else:
        await update.message.reply_text(f"❌ Ошибка:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")

async def cmd_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    if not context.args:     return await update.message.reply_text("Использование: /revoke <username>")
    username = context.args[0]
    if not validate_username(username): return await update.message.reply_text("Некорректное имя.")
    await update.message.chat.send_action("typing")
    proc = await run_script(["revoke", username])  # по умолчанию: purge
    if proc.returncode == 0:
        await update.message.reply_text(f"🗑 {username} удалён.")
        try:
            await notify_user_revoked_if_possible(username, context)
        except Exception as e:
            log.warning("notify_user_revoked failed (cmd) for %s: %s", username, e)
    else:
        await update.message.reply_text(f"❌ Ошибка:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")

async def cmd_getconf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    if not context.args:     return await update.message.reply_text("Использование: /getconf <username>")
    username = context.args[0]
    if not validate_username(username): return await update.message.reply_text("Некорректное имя.")
    path = client_conf_path(username)
    if not path.exists():
        await update.message.reply_text("Конфиг не найден — пробую создать…")
        proc = await run_script(["add", username])
        if proc.returncode != 0 or not path.exists():
            return await update.message.reply_text(f"❌ Не удалось получить конфиг:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")
    await update.message.chat.send_action("upload_document")
    with path.open("rb") as f:
        await update.message.reply_document(document=InputFile(f, filename=path.name), caption=f"{username}.conf")

async def cmd_getqr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    if not context.args:     return await update.message.reply_text("Использование: /getqr <username>")
    username = context.args[0]
    if not validate_username(username): return await update.message.reply_text("Некорректное имя.")
    qr = client_qr_path(username)
    if not qr.exists():
        await update.message.reply_text("QR не найден — пробую создать…")
        proc = await run_script(["add", username])
        if proc.returncode != 0 or not qr.exists():
            return await update.message.reply_text(f"❌ Не удалось получить QR:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")
    await update.message.chat.send_action("upload_photo")
    with qr.open("rb") as f:
        await update.message.reply_photo(photo=InputFile(f, filename=qr.name), caption=f"{username} — QR")

async def cmd_show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    if not context.args:     return await update.message.reply_text("Использование: /show <username>")
    username = context.args[0]
    if not validate_username(username): return await update.message.reply_text("Некорректное имя.")
    proc = await run_script(["show", username])
    if proc.returncode == 0:
        out = proc.stdout.strip() or "Пусто."
        await update.message.reply_text(f"<pre>{html.escape(out)}</pre>", parse_mode="HTML")
    else:
        await update.message.reply_text(f"❌ Ошибка:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update): return await update.message.reply_text("Доступ запрещён.")
    text = await build_global_stats_text()
    await update.message.reply_text(text, parse_mode="HTML")

# ---------- Callback (кнопки) ----------
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return await update.callback_query.answer("Доступ запрещён", show_alert=True)
    q = update.callback_query
    await q.answer()
    data = (q.data or "")
    try:
        kind, *rest = data.split(":")
    except ValueError:
        return

    # list:<scope>:<page>
    if kind == "list":
        scope, page_s = rest
        page = int(page_s) if page_s.isdigit() else 1
        items = await (list_active_clients() if scope == "active" else asyncio.to_thread(list_all_clients_fs))
        total = len(items)
        total_pages = max((total + PAGE_SIZE - 1) // PAGE_SIZE, 1)
        page = min(max(page, 1), total_pages)
        page_items = paginate(items, page, PAGE_SIZE)
        await q.message.edit_text(list_title(scope, page, total, total_pages))
        await q.message.edit_reply_markup(build_list_markup(page_items, page, total_pages, scope))
        return

    # user:<name>:<scope>:<page>
    if kind == "user":
        name, scope, page_s = rest
        page = int(page_s) if page_s.isdigit() else 1
        await q.message.edit_text(f"Управление: {name}")
        await q.message.edit_reply_markup(build_user_markup(name, scope, page))
        return

    # gstats:<scope>:<page> — общая статистика
    if kind == "gstats":
        scope, page_s = rest
        page = int(page_s) if page_s.isdigit() else 1
        text = await build_global_stats_text()
        await q.message.edit_text(text, parse_mode="HTML")
        await q.message.edit_reply_markup(build_global_stats_markup(scope, page))
        return

    # act:<action>:<name>:<scope>:<page>
    if kind == "act":
        action, name, scope, page_s = rest
        page = int(page_s) if page_s.isdigit() else 1

        if action == "askrevoke":
            await q.message.edit_text(f"Удалить {name}?")
            await q.message.edit_reply_markup(build_user_confirm_markup(name, scope, page))
            return

        if action == "revoke":
            proc = await run_script(["revoke", name])
            if proc.returncode == 0:
                # Обновить сообщение, не аварийничая на несущественных ошибках
                try:
                    await q.message.edit_text(f"🗑 {name} удалён.")
                except Exception as e:
                    log.debug("edit_text after revoke ignored: %s", e)
                try:
                    await q.message.edit_reply_markup(None)
                except Exception as e:
                    log.debug("edit_reply_markup after revoke ignored: %s", e)

                # Попробовать синхронизировать БД пользовательского бота и уведомить пользователя
                try:
                    await notify_user_revoked_if_possible(name, context)
                except Exception as e:
                    log.warning("notify_user_revoked failed for %s: %s", name, e)
            else:
                await q.message.reply_text(
                    f"❌ Ошибка удаления:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>",
                    parse_mode="HTML",
                )
            return

        if action == "getconf":
            path = client_conf_path(name)
            if not path.exists():
                proc = await run_script(["add", name])
                if proc.returncode != 0 or not path.exists():
                    return await q.message.reply_text(f"❌ Не удалось получить конфиг:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")
            with path.open("rb") as f:
                await q.message.reply_document(document=InputFile(f, filename=path.name), caption=f"{name}.conf")
            return

        if action == "getqr":
            qr = client_qr_path(name)
            if not qr.exists():
                proc = await run_script(["add", name])
                if proc.returncode != 0 or not qr.exists():
                    return await q.message.reply_text(f"❌ Не удалось получить QR:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")
            with qr.open("rb") as f:
                await q.message.reply_photo(photo=InputFile(f, filename=qr.name), caption=f"{name} — QR")
            return

        if action == "stats":
            text = await build_stats_text(name)
            await q.message.edit_text(text, parse_mode="HTML")
            await q.message.edit_reply_markup(build_stats_markup(name, scope, page))
            return

# ---------- Ошибки ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    err = context.error
    # Игнорируем шумные/безопасные ошибки редактирования сообщений
    if isinstance(err, BadRequest):
        msg = str(err).lower()
        if "message is not modified" in msg or "query is too old" in msg:
            log.debug("Ignored BadRequest: %s", err)
            return
    log.exception("Exception in handler", exc_info=err)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message("⚠️ Произошла ошибка. Пожалуйста, повторите действие позже.")
    except Exception:
        pass

# ---------- main ----------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN не задан")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("whoami",  lambda u, c: u.message.reply_text(f"Ваш chat_id: {u.effective_user.id}")))
    app.add_handler(CommandHandler("list",    cmd_list))
    app.add_handler(CommandHandler("find",    cmd_find))
    app.add_handler(CommandHandler("add",     cmd_add))
    app.add_handler(CommandHandler("revoke",  cmd_revoke))
    app.add_handler(CommandHandler("getconf", cmd_getconf))
    app.add_handler(CommandHandler("getqr",   cmd_getqr))
    app.add_handler(CommandHandler("show",    cmd_show))
    app.add_handler(CommandHandler("stats",   cmd_stats))

    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^(list|user|act|gstats):"))

    app.add_error_handler(on_error)

    app.run_polling()

if __name__ == "__main__":
    main()
