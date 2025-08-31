#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
VPN User Bot — подписки/промокоды/выдача конфигов, с нулевым тест-режимом оплаты

Команды пользователя:
  /start, /help
  /buy                — покупка подписки (если задан PAYMENT_PROVIDER_TOKEN)
  /plans              — показать планы
  /redeem <CODE>      — активировать промокод
  /status             — когда купил и сколько осталось
  /myvpn              — скачать .conf или QR

Команды админа (если ваш chat_id в ADMIN_IDS):
  /addpromo <CODE> <DAYS> [MAX_USES] [NOTE...]
  /delpromo <CODE>
  /promoinfo <CODE>

Зависимости: python-telegram-bot==20.7
"""

import asyncio
import html
import logging
import os
import re
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple

from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, LabeledPrice, InputFile
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters, PreCheckoutQueryHandler
)

# -------------------- Конфиг --------------------
VPN_SCRIPT = Path("/root/vpn_setup.sh")               # если у тебя /root/vpn_conf.sh — сделай симлинк
CLIENTS_DIR = Path("/etc/wireguard/clients")
BASH = os.environ.get("BASH", "/bin/bash")

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

# Telegram Payments
PAYMENT_PROVIDER_TOKEN = os.environ.get("PAYMENT_PROVIDER_TOKEN", "").strip()
CURRENCY = os.environ.get("CURRENCY", "RUB").strip()  # RUB/USD/EUR...

# Тест-режим оплаты: 1 = отправляем инвойсы на 0; 0 = реальные цены
PAY_TEST_ZERO = os.environ.get("PAY_TEST_ZERO", "1").strip() in ("1", "true", "True", "yes", "on")

# Админы для /addpromo и т.п.
_ADMIN_ENV = os.environ.get("ADMIN_IDS", "").strip()
ADMIN_IDS = {int(x) for x in _ADMIN_ENV.split(",") if x.strip().isdigit()} if _ADMIN_ENV else set()

# БД
DB_DIR = Path("/var/lib/vpn-user-bot"); DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "db.sqlite3"

# Планы (minor units: копейки/центы)
PLANS = {
    "month":  dict(title="VPN на 30 дней", payload="plan_month",   price=19900,  duration_days=30),
    "quarter":dict(title="VPN на 90 дней", payload="plan_quarter", price=49900,  duration_days=90),
    "year":   dict(title="VPN на 365 дней",payload="plan_year",    price=149900, duration_days=365),
}

ONLINE_REVOKE_INTERVAL = 600  # авто-ревок просроченных каждые 10 минут

# -------------------- Логирование --------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")
log = logging.getLogger("vpn-user-bot")

# -------------------- Утилиты --------------------
def is_admin(update: Update) -> bool:
    if not ADMIN_IDS:
        return False
    uid = update.effective_user.id if update.effective_user else None
    return uid in ADMIN_IDS

async def run_cmd(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    log.debug("RUN: %s", " ".join(shlex.quote(a) for a in args))
    return await asyncio.to_thread(
        subprocess.run, args,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        timeout=timeout, check=False
    )

async def run_script(script_args: List[str], timeout: int = 240) -> subprocess.CompletedProcess:
    return await run_cmd([BASH, str(VPN_SCRIPT), *script_args], timeout=timeout)

def client_conf_path(username: str) -> Path:
    return CLIENTS_DIR / username / f"{username}.conf"

def client_qr_path(username: str) -> Path:
    return CLIENTS_DIR / username / "qr.png"

def client_dir(username: str) -> Path:
    return CLIENTS_DIR / username

def ensure_vpn_username(tg_id: int) -> str:
    return f"u{tg_id}"

def now_ts() -> int:
    return int(time.time())

def human_left(seconds: int) -> str:
    seconds = max(0, seconds)
    d, r = divmod(seconds, 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}д")
    if h: parts.append(f"{h}ч")
    if m and not d: parts.append(f"{m}м")
    return " ".join(parts) or "меньше минуты"

# -------------------- БД --------------------
def db() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    return con

def db_init():
    con = db()
    with con:
        con.executescript("""
CREATE TABLE IF NOT EXISTS users (
  tg_id       INTEGER PRIMARY KEY,
  tg_username TEXT,
  vpn_user    TEXT UNIQUE,
  created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS subscriptions (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  tg_id       INTEGER NOT NULL,
  start_ts    INTEGER NOT NULL,
  end_ts      INTEGER NOT NULL,
  status      TEXT NOT NULL,            -- active | expired | canceled
  plan        TEXT,                     -- plan key or 'promo'
  promo_code  TEXT,
  tx_id       TEXT,
  created_at  INTEGER NOT NULL,
  FOREIGN KEY (tg_id) REFERENCES users (tg_id)
);

CREATE TABLE IF NOT EXISTS promo_codes (
  code        TEXT PRIMARY KEY,
  duration_days INTEGER NOT NULL,
  max_uses    INTEGER NOT NULL DEFAULT 1,
  used_count  INTEGER NOT NULL DEFAULT 0,
  note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_subs_user ON subscriptions (tg_id, end_ts, status);
""")
    con.close()

def db_get_user(tg_id: int) -> Optional[sqlite3.Row]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    row = cur.fetchone(); con.close()
    return row

def db_upsert_user(tg_id: int, tg_username: Optional[str]) -> sqlite3.Row:
    vpn_user = ensure_vpn_username(tg_id)
    con = db()
    with con:
        con.execute("""
INSERT INTO users (tg_id, tg_username, vpn_user, created_at)
VALUES (?, ?, ?, ?)
ON CONFLICT(tg_id) DO UPDATE SET tg_username=excluded.tg_username
""", (tg_id, tg_username or "", vpn_user, now_ts()))
    con.close()
    return db_get_user(tg_id)

def db_get_active_sub(tg_id: int) -> Optional[sqlite3.Row]:
    ts = now_ts()
    con = db(); cur = con.cursor()
    cur.execute("""
SELECT * FROM subscriptions
WHERE tg_id=? AND status='active' AND end_ts>?
ORDER BY end_ts DESC LIMIT 1
""", (tg_id, ts))
    row = cur.fetchone(); con.close()
    return row

def db_create_or_extend_sub(tg_id: int, duration_days: int, plan: str, promo_code: Optional[str], tx_id: Optional[str]) -> sqlite3.Row:
    now = now_ts()
    cur_active = db_get_active_sub(tg_id)
    start_ts = now
    end_ts = now + duration_days * 86400
    if cur_active:
        start_ts = cur_active["start_ts"]
        end_ts = int(cur_active["end_ts"]) + duration_days * 86400
    con = db()
    with con:
        con.execute("UPDATE subscriptions SET status='expired' WHERE tg_id=? AND status='active' AND end_ts<=?", (tg_id, now))
        con.execute("""
INSERT INTO subscriptions (tg_id, start_ts, end_ts, status, plan, promo_code, tx_id, created_at)
VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
""", (tg_id, start_ts, end_ts, plan, promo_code, tx_id or "", now))
    con.close()
    return db_get_active_sub(tg_id)

def db_consume_promo(code: str) -> Optional[sqlite3.Row]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM promo_codes WHERE code=?", (code,))
    row = cur.fetchone()
    if not row:
        con.close(); return None
    if row["used_count"] >= row["max_uses"]:
        con.close(); return None
    with con:
        cur2 = con.execute("UPDATE promo_codes SET used_count = used_count + 1 WHERE code=? AND used_count<max_uses", (code,))
        if cur2.rowcount == 0:
            con.close(); return None
    con.close()
    return row

def db_add_promo(code: str, days: int, max_uses: int, note: str) -> bool:
    con = db()
    try:
        with con:
            con.execute("INSERT INTO promo_codes (code, duration_days, max_uses, used_count, note) VALUES (?, ?, ?, 0, ?)",
                        (code, days, max_uses, note))
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        con.close()

def db_del_promo(code: str) -> bool:
    con = db()
    with con:
        cur = con.execute("DELETE FROM promo_codes WHERE code=?", (code,))
        ok = cur.rowcount > 0
    con.close()
    return ok

def db_get_promo(code: str) -> Optional[sqlite3.Row]:
    con = db(); cur = con.cursor()
    cur.execute("SELECT * FROM promo_codes WHERE code=?", (code,))
    row = cur.fetchone(); con.close()
    return row

def db_all_expired_to_revoke() -> List[Tuple[int, str]]:
    now = now_ts()
    con = db(); cur = con.cursor()
    cur.execute("""
SELECT u.tg_id, u.vpn_user
FROM subscriptions s
JOIN users u ON u.tg_id = s.tg_id
WHERE s.status='active' AND s.end_ts<=?
GROUP BY u.tg_id, u.vpn_user
""", (now,))
    rows = cur.fetchall(); con.close()
    return [(r["tg_id"], r["vpn_user"]) for r in rows]

def db_mark_user_expired(tg_id: int):
    con = db()
    with con:
        con.execute("UPDATE subscriptions SET status='expired' WHERE tg_id=? AND status='active'", (tg_id,))
    con.close()

# -------------------- Бизнес-логика --------------------
async def ensure_client_files(vpn_user: str) -> bool:
    conf = client_conf_path(vpn_user)
    if conf.exists():
        return True
    proc = await run_script(["add", vpn_user])
    return proc.returncode == 0 and conf.exists()

async def set_expires_file(vpn_user: str, end_ts: int):
    d = client_dir(vpn_user)
    d.mkdir(parents=True, exist_ok=True)
    (d / "expires_at").write_text(str(int(end_ts)))

async def revoke_user(vpn_user: str):
    await run_script(["revoke", vpn_user])

async def grant_or_extend_access(tg_id: int, duration_days: int, plan_key: str, promo_code: Optional[str], tx_id: Optional[str]) -> Tuple[bool, str]:
    user = db_upsert_user(tg_id, None)
    vpn_user = user["vpn_user"]
    active = db_create_or_extend_sub(tg_id, duration_days, plan_key, promo_code, tx_id)
    ok = await ensure_client_files(vpn_user)
    if not ok:
        return False, "Не удалось создать файлы VPN-клиента."
    await set_expires_file(vpn_user, int(active["end_ts"]))
    return True, f"Доступ активен до {datetime.fromtimestamp(active['end_ts']).strftime('%Y-%m-%d %H:%M:%S')}"

# -------------------- Команды пользователя --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db_upsert_user(update.effective_user.id, update.effective_user.username)
    txt = (
        "Привет! Это VPN-бот.\n\n"
        "• /buy — купить подписку\n"
        "• /redeem <код> — активировать промокод\n"
        "• /status — статус подписки\n"
        "• /myvpn — скачать .conf или QR\n"
        "• /help — помощь\n"
    )
    await update.message.reply_text(txt)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)

async def cmd_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    test_tag = " [ТЕСТ 0]" if PAY_TEST_ZERO else ""
    lines = ["Доступные планы:"]
    for k, p in PLANS.items():
        price = (0 if PAY_TEST_ZERO else p["price"]) / 100.0
        lines.append(f"• {p['title']}: {price:.2f} {CURRENCY}{test_tag} — {p['duration_days']} дней  (/buy)")
    await update.message.reply_text("\n".join(lines))

async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not PAYMENT_PROVIDER_TOKEN:
        return await update.message.reply_text("Онлайн-оплата не настроена. Используйте промокод: /redeem <код>.")
    tag = " (ТЕСТ 0 ₽)" if PAY_TEST_ZERO else ""
    kb = [
        [InlineKeyboardButton(f"{p['title']}{tag}", callback_data=f"buy:{key}")]
        for key, p in PLANS.items()
    ]
    await update.message.reply_text("Выберите план:", reply_markup=InlineKeyboardMarkup(kb))

async def try_send_invoice(qmsg, plan_key: str):
    """Шлём инвойс. В тест-режиме — 0. Если платформа не принимает 0, делаем тестовую выдачу без оплаты."""
    plan = PLANS.get(plan_key)
    if not plan:
        await qmsg.reply_text("Неизвестный план.")
        return
    amount = 0 if PAY_TEST_ZERO else plan["price"]
    prices = [LabeledPrice(label=plan["title"], amount=amount)]
    try:
        await qmsg.reply_invoice(
            title=plan["title"],
            description=f"Подписка на {plan['duration_days']} дней" + (" (тест, 0 ₽)" if PAY_TEST_ZERO else ""),
            payload=plan["payload"],
            provider_token=PAYMENT_PROVIDER_TOKEN,
            currency=CURRENCY,
            prices=prices,
            is_flexible=False,
        )
    except BadRequest as e:
        # Если провайдер/ТГ не принимает 0 — тестовый бэкап: сразу выдаём доступ
        if PAY_TEST_ZERO:
            ok, msg = await grant_or_extend_access(qmsg.chat_id, plan["duration_days"], plan_key, None, tx_id="TEST-ZERO")
            if ok:
                await qmsg.reply_text("✅ Тестовый режим: платёж не требовался. " + msg)
                await send_vpn_buttons_direct(qmsg, plan_key)
            else:
                await qmsg.reply_text("❌ " + msg)
        else:
            await qmsg.reply_text(f"❌ Ошибка выставления счёта: {e.message}")

async def cb_buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not PAYMENT_PROVIDER_TOKEN:
        return await update.callback_query.answer("Оплата отключена", show_alert=True)
    q = update.callback_query
    await q.answer()
    _, plan_key = (q.data or "").split(":", 1)
    await try_send_invoice(q.message, plan_key)

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sp = update.message.successful_payment
    payload = sp.invoice_payload
    tx_id = sp.provider_payment_charge_id or sp.telegram_payment_charge_id
    plan_key = None
    for k, p in PLANS.items():
        if p["payload"] == payload:
            plan_key = k
            break
    if not plan_key:
        return await update.message.reply_text("Не удалось определить план. Напишите в поддержку.")
    duration = PLANS[plan_key]["duration_days"]
    ok, msg = await grant_or_extend_access(update.effective_user.id, duration, plan_key, None, tx_id)
    if ok:
        await update.message.reply_text("✅ Оплата принята. " + msg)
        await send_vpn_buttons(update, context)
    else:
        await update.message.reply_text("❌ " + msg)

async def cmd_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if not args:
        return await update.message.reply_text("Использование: /redeem <ПРОМОКОД>")
    code = args[0].strip()
    row = db_consume_promo(code)
    if not row:
        return await update.message.reply_text("❌ Промокод недействителен или исчерпан.")
    days = int(row["duration_days"])
    ok, msg = await grant_or_extend_access(update.effective_user.id, days, "promo", code, None)
    if ok:
        await update.message.reply_text(f"✅ Промокод применён на {days} дней. " + msg)
        await send_vpn_buttons(update, context)
    else:
        await update.message.reply_text("❌ " + msg)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = db_upsert_user(update.effective_user.id, update.effective_user.username)
    sub = db_get_active_sub(user["tg_id"])
    if not sub:
        return await update.message.reply_text("Подписка не активна. /buy или /redeem <код>")
    left = int(sub["end_ts"]) - now_ts()
    dt_start = datetime.fromtimestamp(int(sub["start_ts"])).strftime("%Y-%m-%d %H:%M:%S")
    dt_end = datetime.fromtimestamp(int(sub["end_ts"])).strftime("%Y-%m-%d %H:%M:%S")
    plan = sub["plan"] or "—"
    promo = sub["promo_code"] or "—"
    await update.message.reply_text(
        f"Статус: активна\n"
        f"План: {plan}\n"
        f"Промокод: {promo}\n"
        f"Начало: {dt_start}\n"
        f"Окончание: {dt_end}\n"
        f"Осталось: {human_left(left)}"
    )

async def send_vpn_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Скачать .conf", callback_data="get:conf"),
         InlineKeyboardButton("🧾 Скачать QR",    callback_data="get:qr")],
    ])
    await update.message.reply_text("Готово! Можете скачать конфиг:", reply_markup=kb)

async def send_vpn_buttons_direct(msg, plan_key: str) -> None:
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Скачать .conf", callback_data="get:conf"),
         InlineKeyboardButton("🧾 Скачать QR",    callback_data="get:qr")],
    ])
    await msg.reply_text(f"План: {PLANS[plan_key]['title']}. Можете скачать конфиг:", reply_markup=kb)

async def cmd_myvpn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = db_upsert_user(update.effective_user.id, update.effective_user.username)
    sub = db_get_active_sub(user["tg_id"])
    if not sub:
        return await update.message.reply_text("Подписка не активна. /buy или /redeem <код>")
    vpn_user = user["vpn_user"]
    ok = await ensure_client_files(vpn_user)
    if not ok:
        return await update.message.reply_text("Не удалось подготовить файлы VPN. Напишите в поддержку.")
    await set_expires_file(vpn_user, int(sub["end_ts"]))
    await send_vpn_buttons(update, context)

# -------------------- Callback: выдача файлов --------------------
async def on_cb_get(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    user = db_get_user(q.from_user.id) or db_upsert_user(q.from_user.id, q.from_user.username)
    sub = db_get_active_sub(user["tg_id"])
    if not sub:
        return await q.message.reply_text("Подписка не активна. /buy или /redeem <код>")

    vpn_user = user["vpn_user"]
    ok = await ensure_client_files(vpn_user)
    if not ok:
        return await q.message.reply_text("Не удалось подготовить файлы VPN. Напишите в поддержку.")
    await set_expires_file(vpn_user, int(sub["end_ts"]))

    if data == "get:conf":
        path = client_conf_path(vpn_user)
        with path.open("rb") as f:
            await q.message.reply_document(InputFile(f, filename=path.name), caption=f"{vpn_user}.conf")
    elif data == "get:qr":
        qr = client_qr_path(vpn_user)
        if not qr.exists():
            await run_script(["add", vpn_user])
        if qr.exists():
            with qr.open("rb") as f:
                await q.message.reply_photo(InputFile(f, filename=qr.name), caption=f"{vpn_user} — QR")
        else:
            await q.message.reply_text("QR не найден. Попробуйте снова.")

# -------------------- Админ: промокоды --------------------
async def cmd_addpromo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return await update.message.reply_text("Доступ запрещён.")
    if len(context.args) < 2:
        return await update.message.reply_text("Использование: /addpromo <CODE> <DAYS> [MAX_USES=1] [NOTE...]")
    code = context.args[0].strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", code or ""):
        return await update.message.reply_text("Некорректный код (3-64 символа A-Za-z0-9_-).")
    try:
        days = int(context.args[1])
        max_uses = int(context.args[2]) if len(context.args) >= 3 else 1
    except ValueError:
        return await update.message.reply_text("DAYS/MAX_USES должны быть числами.")
    note = " ".join(context.args[3:]) if len(context.args) >= 4 else ""
    ok = db_add_promo(code, days, max_uses, note)
    await update.message.reply_text("✅ Добавлен." if ok else "❌ Такой код уже существует.")

async def cmd_delpromo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return await update.message.reply_text("Доступ запрещён.")
    if not context.args:
        return await update.message.reply_text("Использование: /delpromo <CODE>")
    ok = db_del_promo(context.args[0].strip())
    await update.message.reply_text("🗑 Удалён." if ok else "❌ Код не найден.")

async def cmd_promoinfo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update):
        return await update.message.reply_text("Доступ запрещён.")
    if not context.args:
        return await update.message.reply_text("Использование: /promoinfo <CODE>")
    row = db_get_promo(context.args[0].strip())
    if not row:
        return await update.message.reply_text("Код не найден.")
    await update.message.reply_text(
        f"Код: {row['code']}\n"
        f"Дней: {row['duration_days']}\n"
        f"Использовано: {row['used_count']} / {row['max_uses']}\n"
        f"Заметка: {row['note'] or '—'}"
    )

# -------------------- Планировщик: авто-ревок --------------------
async def job_expiry_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    victims = db_all_expired_to_revoke()
    if not victims:
        return
    for tg_id, vpn_user in victims:
        try:
            await revoke_user(vpn_user)
            db_mark_user_expired(tg_id)
            log.info("Revoked expired user: %s (tg %s)", vpn_user, tg_id)
        except Exception as e:
            log.exception("Failed revoke for %s: %s", vpn_user, e)

# -------------------- main --------------------
def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN not set")
    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # user-facing
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("plans",   cmd_plans))
    app.add_handler(CommandHandler("buy",     cmd_buy))
    app.add_handler(CommandHandler("redeem",  cmd_redeem))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("myvpn",   cmd_myvpn))

    # payments
    app.add_handler(CallbackQueryHandler(cb_buy, pattern=r"^buy:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # file callbacks
    app.add_handler(CallbackQueryHandler(on_cb_get, pattern=r"^get:(conf|qr)$"))

    # admin promo
    app.add_handler(CommandHandler("addpromo",   cmd_addpromo))
    app.add_handler(CommandHandler("delpromo",   cmd_delpromo))
    app.add_handler(CommandHandler("promoinfo",  cmd_promoinfo))

    # expiry job
    app.job_queue.run_repeating(job_expiry_check, interval=ONLINE_REVOKE_INTERVAL, first=30)

    app.run_polling()

if __name__ == "__main__":
    main()
