#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram-бот для управления WireGuard через /root/vpn_setup.sh

Особенности UI:
- Пагинация списка клиентов (12 на страницу), переключатель Активные/Все.
- Список и карточки редактируются в одном сообщении (минимум спама).
- Поиск: /find <mask>
- Карточка клиента: Скачать .conf / Скачать QR / Удалить (с подтверждением) / Назад.

Команды:
  /list [active|all] [page]   — список клиентов
  /find <mask>                — поиск по имени
  /add <username> [--ip 10.8.0.X] [--ipv6]
  /revoke <username>
  /getconf <username>
  /getqr <username>
  /show <username>            — показать замаскированный конфиг
  /whoami

Зависимости: python-telegram-bot==20.7
"""

import asyncio
import html
import logging
import os
import re
import shlex
import subprocess
from glob import glob
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message,
)
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, CallbackQueryHandler,
)

# ---------- Настройки ----------
VPN_SCRIPT = Path("/root/vpn_conf.sh")  # если у тебя файл /root/vpn_conf.sh — сделай симлинк на это имя
BASH = os.environ.get("BASH", "/bin/bash")  # запускаем скрипт через bash
CLIENTS_DIR = Path("/etc/wireguard/clients")

BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "7927839580:AAE1GOB57eZJy0u1qL6hz33jc68IoBowEPg"  # ENV имеет приоритет
).strip()

# Ограничение доступа: ADMIN_IDS="123,456" (опционально)
_ADMIN_ENV = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = {int(x) for x in _ADMIN_ENV.split(",") if x.strip().isdigit()} if _ADMIN_ENV else set()

PAGE_SIZE = 12
USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("vpn-bot")


# ---------- Утилиты ----------
def assert_token():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан")

def is_admin(update: Update) -> bool:
    if not ADMIN_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid in ADMIN_IDS

def validate_username(u: str) -> bool:
    return bool(USERNAME_RE.fullmatch(u))

async def run_cmd(args: Sequence[str], timeout: int = 120) -> subprocess.CompletedProcess:
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

async def run_script(script_args: Sequence[str], timeout: int = 180) -> subprocess.CompletedProcess:
    # Всегда через bash — надёжнее
    return await run_cmd([BASH, str(VPN_SCRIPT), *script_args], timeout=timeout)

def detect_iface() -> str:
    files = sorted(glob("/etc/wireguard/*.conf"))
    return Path(files[0]).stem if files else "wg0"

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

def client_conf_path(username: str) -> Path:
    return CLIENTS_DIR / username / f"{username}.conf"

def client_pubkey_path(username: str) -> Path:
    return CLIENTS_DIR / username / "public.key"

def client_qr_path(username: str) -> Path:
    return CLIENTS_DIR / username / "qr.png"

def list_all_clients_fs() -> List[str]:
    if not CLIENTS_DIR.exists():
        return []
    names: List[str] = []
    for d in CLIENTS_DIR.iterdir():
        if d.is_dir() and validate_username(d.name) and (d / f"{d.name}.conf").exists():
            names.append(d.name)
    names.sort(key=str.lower)
    return names

async def list_active_clients() -> List[str]:
    iface, active_keys = await get_active_peer_keys()
    names: List[str] = []
    for name in list_all_clients_fs():
        pub = client_pubkey_path(name)
        if pub.exists() and pub.read_text().strip() in active_keys:
            names.append(name)
    return names

def paginate(items: Sequence[str], page: int, size: int) -> Sequence[str]:
    start = max(page - 1, 0) * size
    end = start + size
    return items[start:end]

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
    toggle_scope = "all" if scope == "active" else "active"
    rows.append([InlineKeyboardButton(f"Показать: {('Активные' if scope=='active' else 'Все')} ▸ сменить",
                                      callback_data=f"list:{toggle_scope}:1")])
    return InlineKeyboardMarkup(rows)

def build_user_markup(name: str, scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("⬇️ Скачать .conf", callback_data=f"act:getconf:{name}:{scope}:{page}"),
            InlineKeyboardButton("🧾 Скачать QR",    callback_data=f"act:getqr:{name}:{scope}:{page}"),
        ],
        [   InlineKeyboardButton("🗑 Удалить",        callback_data=f"act:askrevoke:{name}:{scope}:{page}") ],
        [   InlineKeyboardButton("← Назад к списку",  callback_data=f"list:{scope}:{page}") ]
    ])

def build_user_confirm_markup(name: str, scope: str, page: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"act:revoke:{name}:{scope}:{page}"),
            InlineKeyboardButton("✖️ Отмена",      callback_data=f"user:{name}:{scope}:{page}"),
        ]
    ])

def list_title(scope: str, page: int, total: int, total_pages: int) -> str:
    return f"Список клиентов ({'Активные' if scope=='active' else 'Все'}): стр. {page}/{total_pages} • всего: {total}\n" \
           f"Подсказки: /find <маска>, /add <имя>, /revoke <имя>"

def ensure_single_message(msg: Message | None) -> bool:
    return msg is not None and msg.message_id is not None


# ---------- Команды ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        await update.message.reply_text("Доступ запрещён."); return
    await update.message.reply_text(
        "Привет! Я панель управления WireGuard.\n"
        "• /list — список (пагинация)\n"
        "• /find <маска> — поиск\n"
        "• /add <username> [--ip ...] [--ipv6]\n"
        "• /revoke <username>\n"
        "• /getconf <username> /getqr <username>\n"
        "• /show <username>\n"
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
    # первые 30 результатов
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
                await q.message.edit_text(f"🗑 {name} удалён.")
                await q.message.edit_reply_markup(None)
            else:
                await q.message.reply_text(f"❌ Ошибка удаления:\n<pre>{html.escape(proc.stderr or proc.stdout)}</pre>", parse_mode="HTML")
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


# ---------- Ошибки ----------
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Exception in handler", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await update.effective_chat.send_message("⚠️ Произошла ошибка, проверьте логи на сервере.")
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

    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^(list|user|act):"))

    app.add_error_handler(on_error)

    app.run_polling()

if __name__ == "__main__":
    main()

