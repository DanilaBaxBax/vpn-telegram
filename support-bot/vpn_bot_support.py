#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Support Bot — приём тикетов от пользователей и ответы саппорта.

Зависимости: python-telegram-bot==20.7

ENV:
  BOT_TOKEN                — обязательный
  ADMIN_IDS                — CSV со списком chat_id саппорт-агентов (опционально)
  SUPPORT_NOTIFY_TARGET    — @username канала/группы для уведомлений (опц.)

Хранилище: /var/lib/vpn-support-bot/db.sqlite3
"""

import asyncio
import html
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple, Dict

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
_ADMIN_ENV = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = {int(x) for x in _ADMIN_ENV.split(",") if x.strip().isdigit()} if _ADMIN_ENV else set()
NOTIFY_TARGET = os.environ.get("SUPPORT_NOTIFY_TARGET", "@baxbax_VPN_support").strip()

DB_DIR = Path("/var/lib/vpn-support-bot"); DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "db.sqlite3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("support-bot")

def db() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con

def db_init():
    con = db()
    with con:
        con.executescript(
            """
CREATE TABLE IF NOT EXISTS tickets (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id       INTEGER NOT NULL,
  tg_username TEXT,
  subject     TEXT,
  status      TEXT NOT NULL DEFAULT 'open', -- open|closed
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (tg_id, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ticket_id   INTEGER NOT NULL,
  from_role   TEXT NOT NULL,   -- user|staff
  from_id     INTEGER NOT NULL,
  text        TEXT,
  created_at  INTEGER NOT NULL,
  FOREIGN KEY (ticket_id) REFERENCES tickets(id)
);
            """
        )
    con.close()

def now() -> int:
    return int(time.time())

def is_staff(update: Update) -> bool:
    u = update.effective_user
    return bool(ADMIN_IDS) and u and u.id in ADMIN_IDS

def ticket_create(tg_id: int, tg_username: Optional[str], subject: str, first_text: Optional[str]) -> int:
    ts = now()
    con = db()
    with con:
        cur = con.execute(
            "INSERT INTO tickets (tg_id, tg_username, subject, status, created_at, updated_at) VALUES (?, ?, ?, 'open', ?, ?)",
            (tg_id, tg_username or "", subject[:200] if subject else "", ts, ts)
        )
        tid = cur.lastrowid
        if first_text:
            con.execute(
                "INSERT INTO messages (ticket_id, from_role, from_id, text, created_at) VALUES (?, 'user', ?, ?, ?)",
                (tid, tg_id, first_text, ts)
            )
    con.close()
    return tid

def ticket_get_open_for_user(tg_id: int) -> Optional[sqlite3.Row]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM tickets WHERE tg_id=? AND status='open' ORDER BY updated_at DESC LIMIT 1", (tg_id,))
    row = cur.fetchone(); con.close(); return row

def ticket_add_message(ticket_id: int, from_role: str, from_id: int, text: str):
    ts = now(); con = db()
    with con:
        con.execute(
            "INSERT INTO messages (ticket_id, from_role, from_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, from_role, from_id, text, ts)
        )
        con.execute("UPDATE tickets SET updated_at=? WHERE id=?", (ts, ticket_id))
    con.close()

def ticket_close(ticket_id: int):
    ts = now(); con = db()
    with con:
        con.execute("UPDATE tickets SET status='closed', updated_at=? WHERE id=?", (ts, ticket_id))
    con.close()

def ticket_get(ticket_id: int) -> Optional[sqlite3.Row]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,))
    row = cur.fetchone(); con.close(); return row

def list_tickets(status: str, offset: int, limit: int) -> List[sqlite3.Row]:
    con = db(); cur = con.cursor()
    if status == 'all':
        cur.execute("SELECT * FROM tickets ORDER BY updated_at DESC LIMIT ? OFFSET ?", (limit, offset))
    else:
        cur.execute("SELECT * FROM tickets WHERE status=? ORDER BY updated_at DESC LIMIT ? OFFSET ?", (status, limit, offset))
    rows = cur.fetchall(); con.close(); return rows

def list_user_tickets(tg_id: int, limit: int = 5) -> List[sqlite3.Row]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM tickets WHERE tg_id=? ORDER BY updated_at DESC LIMIT ?", (tg_id, limit))
    rows = cur.fetchall(); con.close(); return rows

# In-memory map: admin user_id -> ticket_id awaiting reply text
REPLY_AWAIT: Dict[int, int] = {}

def kb_staff_for_ticket(tid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Ответить", callback_data=f"spt:reply:{tid}"),
         InlineKeyboardButton("Закрыть",  callback_data=f"spt:close:{tid}")]
    ])

async def notify_staff_new_or_update(tid: int, text: str, context: ContextTypes.DEFAULT_TYPE, prefix: str) -> None:
    t = ticket_get(tid)
    if not t:
        return
    uname = t["tg_username"] or f"u{t['tg_id']}"
    head = f"{prefix} #{tid} от {html.escape(uname)}"
    kb = kb_staff_for_ticket(tid)
    # to admins
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=aid, text=f"{head}\n\n{text}", reply_markup=kb)
        except Exception as e:
            log.debug("notify admin %s failed: %s", aid, e)
    # to channel/group
    if NOTIFY_TARGET:
        try:
            await context.bot.send_message(chat_id=NOTIFY_TARGET, text=f"{head}\n\n{text}")
        except Exception as e:
            log.debug("notify channel failed: %s", e)

# ---------------- User side ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Напишите ваше сообщение — мы создадим тикет и ответим здесь.\n"
        "Команды: /new <текст>, /mytickets"
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    text = " ".join(context.args) if context.args else ""
    subject = text.split("\n", 1)[0] if text else "Ticket from user"
    tid = ticket_create(u.id, u.username, subject, text if text else None)
    await update.message.reply_text(f"Создан тикет #{tid}. Пишите сюда для продолжения диалога.")
    if text:
        await notify_staff_new_or_update(tid, text, context, prefix="🆕 Новый тикет")
    else:
        await notify_staff_new_or_update(tid, "(без текста)", context, prefix="🆕 Новый тикет")

async def cmd_mytickets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u = update.effective_user
    rows = list_user_tickets(u.id)
    if not rows:
        return await update.message.reply_text("У вас пока нет тикетов. Используйте /new <текст>.")
    lines = ["Ваши тикеты:"]
    for r in rows:
        dt = datetime.fromtimestamp(int(r["updated_at"])).strftime("%Y-%m-%d %H:%M")
        lines.append(f"• #{r['id']} [{r['status']}] — {r['subject'][:40]} — {dt}")
    await update.message.reply_text("\n".join(lines))

async def on_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or update.message.text is None:
        return
    u = update.effective_user
    text = update.message.text
    t = ticket_get_open_for_user(u.id)
    if not t:
        tid = ticket_create(u.id, u.username, text.split("\n",1)[0][:200], text)
        await update.message.reply_text(f"Создан тикет #{tid}. Мы ответим здесь.")
        await notify_staff_new_or_update(tid, text, context, prefix="🆕 Новый тикет")
        return
    ticket_add_message(int(t["id"]), "user", u.id, text)
    await update.message.reply_text(f"Сообщение добавлено в тикет #{t['id']}")
    await notify_staff_new_or_update(int(t["id"]), text, context, prefix="✉️ Обновление тикета")

# ---------------- Staff side ----------------
async def cmd_tickets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_staff(update):
        return await update.message.reply_text("Доступ запрещён.")
    args = context.args
    scope = (args[0].lower() if args and args[0].lower() in ("open","all") else "open")
    page = int(args[1]) if len(args) >= 2 and args[1].isdigit() else 1
    size = 10
    rows = list_tickets(scope, offset=(page-1)*size, limit=size)
    if not rows:
        return await update.message.reply_text("Нет тикетов по заданному фильтру.")
    lines = [f"Список тикетов ({scope}), стр. {page}"]
    for r in rows:
        dt = datetime.fromtimestamp(int(r["updated_at"])).strftime("%m-%d %H:%M")
        lines.append(f"• #{r['id']} [{r['status']}] u{r['tg_id']} — {r['subject'][:50]} — {dt}")
    await update.message.reply_text("\n".join(lines))

async def cmd_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_staff(update):
        return await update.message.reply_text("Доступ запрещён.")
    if not context.args:
        return await update.message.reply_text("Использование: /view <id>")
    try:
        tid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Некорректный id")
    t = ticket_get(tid)
    if not t:
        return await update.message.reply_text("Нет такого тикета")
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM messages WHERE ticket_id=? ORDER BY id DESC LIMIT 10", (tid,))
    msgs = cur.fetchall(); con.close()
    lines = [f"Тикет #{t['id']} [{t['status']}] от u{t['tg_id']} ({t['tg_username'] or '-'})",
             f"Тема: {t['subject']}", "Последние сообщения:"]
    for m in reversed(msgs):
        ts = datetime.fromtimestamp(int(m["created_at"])).strftime("%m-%d %H:%M")
        lines.append(f"[{m['from_role']}] {ts}: {m['text']}")
    await update.message.reply_text("\n".join(lines), reply_markup=kb_staff_for_ticket(tid))

async def cmd_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_staff(update):
        return await update.message.reply_text("Доступ запрещён.")
    if len(context.args) < 2:
        return await update.message.reply_text("Использование: /reply <id> <текст>")
    try:
        tid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Некорректный id")
    text = " ".join(context.args[1:])
    t = ticket_get(tid)
    if not t:
        return await update.message.reply_text("Нет такого тикета")
    ticket_add_message(tid, "staff", update.effective_user.id, text)
    try:
        await context.bot.send_message(chat_id=int(t["tg_id"]), text=f"Ответ по тикету #{tid}:\n{text}")
    except Exception as e:
        log.warning("failed to DM user for ticket %s: %s", tid, e)
    await update.message.reply_text("Отправлено.")

async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_staff(update):
        return await update.message.reply_text("Доступ запрещён.")
    if not context.args:
        return await update.message.reply_text("Использование: /close <id>")
    try:
        tid = int(context.args[0])
    except ValueError:
        return await update.message.reply_text("Некорректный id")
    t = ticket_get(tid)
    if not t:
        return await update.message.reply_text("Нет такого тикета")
    ticket_close(tid)
    await update.message.reply_text("Закрыто.")
    try:
        await context.bot.send_message(chat_id=int(t["tg_id"]), text=f"Ваш тикет #{tid} закрыт. Если проблема не решена — создайте новый: /new")
    except Exception:
        pass

# --------- Callbacks (inline) ---------
async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if not data.startswith("spt:"):
        return
    if not is_staff(update):
        return await q.message.reply_text("Доступ запрещён.")
    _, action, tid_s = data.split(":", 2)
    try:
        tid = int(tid_s)
    except ValueError:
        return
    if action == "reply":
        REPLY_AWAIT[update.effective_user.id] = tid
        await q.message.reply_text(f"Ответ на тикет #{tid}: отправьте сообщение.")
        return
    if action == "close":
        t = ticket_get(tid)
        if t:
            ticket_close(tid)
            await q.message.reply_text("Закрыто.")
            try:
                await context.bot.send_message(chat_id=int(t["tg_id"]), text=f"Ваш тикет #{tid} закрыт. Если проблема не решена — создайте новый: /new")
            except Exception:
                pass
        return

async def on_staff_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_staff(update) or not update.message or update.message.text is None:
        return
    uid = update.effective_user.id
    tid = REPLY_AWAIT.pop(uid, None)
    if not tid:
        return  # обычная переписка — игнорируем
    text = update.message.text
    t = ticket_get(tid)
    if not t:
        return await update.message.reply_text("Тикет не найден.")
    ticket_add_message(tid, "staff", uid, text)
    try:
        await context.bot.send_message(chat_id=int(t["tg_id"]), text=f"Ответ по тикету #{tid}:\n{text}")
    except Exception as e:
        log.warning("failed to DM user for ticket %s: %s", tid, e)
    await update.message.reply_text("Отправлено.")

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN not set")
    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # user
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("new",  cmd_new))
    app.add_handler(CommandHandler("mytickets", cmd_mytickets))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & (~filters.COMMAND), on_user_message))

    # staff
    app.add_handler(CommandHandler("tickets", cmd_tickets))
    app.add_handler(CommandHandler("view",    cmd_view))
    app.add_handler(CommandHandler("reply",   cmd_reply))
    app.add_handler(CommandHandler("close",   cmd_close))
    app.add_handler(CallbackQueryHandler(on_cb, pattern=r"^spt:"))
    app.add_handler(MessageHandler(filters.TEXT & filters.ChatType.PRIVATE, on_staff_message))

    app.run_polling()

if __name__ == "__main__":
    main()

