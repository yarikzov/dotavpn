import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    Bot, InlineKeyboardButton, InlineKeyboardMarkup,
    Update, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, PreCheckoutQueryHandler, filters,
)

load_dotenv()

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-domain.com/webapp/index.html")
CRYPTO_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "0"))

# Канал, подписку на который нужно проверять перед доступом к боту
SUB_CHANNEL_USERNAME = os.getenv("SUB_CHANNEL_USERNAME", "@NewsDotaVPN")
SUB_CHANNEL_URL = os.getenv("SUB_CHANNEL_URL", "https://t.me/NewsDotaVPN")

# Ссылка на сайт (заглушка, поменять позже)
SITE_URL = os.getenv("SITE_URL", "https://dotachka.ru")

# Фото для приветственного сообщения
START_PHOTO_URL = os.getenv(
    "START_PHOTO_URL",
    "https://i.ibb.co/GvCPdgwJ/image.png",
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ── XUI (optional) ────────────────────────────────────────────────────────────
try:
    from xui_api import XUIApi

    xui = XUIApi()
    logger.info("XUIApi loaded")
except Exception as e:
    xui = None
    logger.warning("XUIApi not available: %s", e)

# ── DB ────────────────────────────────────────────────────────────────────────
DB_PATH = "vpn_bot.db"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kb(label="🚀 Открыть DOTAVPN") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, web_app=WebAppInfo(url=WEBAPP_URL))]])


def _dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%d.%m.%Y")


async def _is_subscribed(bot: Bot, user_id: int) -> bool:
    """Проверяет, подписан ли пользователь на канал SUB_CHANNEL_USERNAME."""
    try:
        member = await bot.get_chat_member(SUB_CHANNEL_USERNAME, user_id)
        return member.status not in ("left", "kicked")
    except Exception as e:
        logger.warning("_is_subscribed check failed for %s: %s", user_id, e)
        # Если не удалось проверить (например, бот не админ канала) —
        # не блокируем пользователя.
        return True


def _sub_required_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Подписаться на канал", url=SUB_CHANNEL_URL)],
        [InlineKeyboardButton("✅ Я подписался", callback_data="check_sub")],
    ])


def _start_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🚀 Открыть маркет", web_app=WebAppInfo(url=WEBAPP_URL))],
        [InlineKeyboardButton("🌐 Наш сайт", url=SITE_URL)],
        [InlineKeyboardButton("📖 Правила и соглашение", callback_data="show_policies")],
    ])


def _get_user(user_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _ensure_user(user_id: int, username=None, first_name=None, referrer_id=None) -> bool:
    """Create user row if missing. Returns True if this is a brand-new user
    (used to know when to fire the "по вашей ссылке перешли" notification)."""
    conn = get_db()
    is_new = not conn.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,)).fetchone()
    if is_new:
        conn.execute(
            "INSERT INTO users (user_id,username,first_name,referrer_id,created_at) VALUES (?,?,?,?,?)",
            (user_id, username, first_name, referrer_id, int(time.time())),
        )
        if referrer_id and referrer_id != user_id:
            conn.execute(
                "INSERT OR IGNORE INTO referrals (referrer_id,referee_id) VALUES (?,?)",
                (referrer_id, user_id),
            )
        conn.commit()
    else:
        conn.execute(
            "UPDATE users SET username=?, first_name=? WHERE user_id=?",
            (username, first_name, user_id),
        )
        conn.commit()
    conn.close()
    return is_new


def _get_vless_link(u: dict) -> Optional[str]:
    """Get VLESS link: try XUI first, then env-var fallback."""
    if not u.get("uuid") or not u.get("client_email"):
        return None
    # 1) Real XUI
    if xui:
        try:
            link = xui.get_vless_link(u["client_email"], u["uuid"])
            if link:
                return link
        except Exception as e:
            logger.warning("xui.get_vless_link error: %s", e)
    # 2) Env-var fallback
    import urllib.parse as _up
    host = os.getenv("XUI_HOST", os.getenv("XUI_URL", "").split("://")[-1].split(":")[0])
    port = os.getenv("XUI_PORT", "443")
    pbk = os.getenv("XUI_PBK", "")
    sni = os.getenv("XUI_SNI", "")
    sid = os.getenv("XUI_SID", "")
    if host and pbk:
        params = _up.urlencode({
            "type": "tcp", "security": "reality",
            "pbk": pbk, "fp": "chrome",
            "sni": sni or host, "sid": sid,
            "spx": "%2F", "flow": "xtls-rprx-vision",
        })
        return f"vless://{u['uuid']}@{host}:{port}?{params}#{_up.quote(u['client_email'])}"
    return None


def _provision_xui(user_id: int, expiry_ms: int) -> bool:
    """Create or update XUI client. Returns True on success."""
    u = _get_user(user_id)
    if not u:
        return False
    conn = get_db()
    try:
        if xui:
            if u.get("uuid") and u.get("client_email"):
                ok = xui.update_client_expiry(u["client_email"], u["uuid"], expiry_ms, 0)
            else:
                new_uuid = str(uuid.uuid4())
                email = f"tg_{user_id}"
                ok = xui.add_client(new_uuid, email, expiry_ms, 0)
                if ok:
                    conn.execute(
                        "UPDATE users SET uuid=?, client_email=? WHERE user_id=?",
                        (new_uuid, email, user_id),
                    )
                    conn.commit()
            return ok
    except Exception as e:
        logger.error("_provision_xui error: %s", e)
    finally:
        conn.close()
    return False


def _activate_payment(invoice_id: str) -> Optional[dict]:
    """
    Activate subscription for a paid invoice.
    Returns dict with user_id, tariff, new_end on success; None if already done or error.
    """
    conn = get_db()
    try:
        pay = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not pay or pay["status"] == "paid":
            return None

        tariff = conn.execute("SELECT * FROM tariffs WHERE id=?", (pay["tariff_id"],)).fetchone()
        if not tariff:
            return None

        uid = pay["user_id"]
        now_ms = int(time.time() * 1000)
        u = conn.execute("SELECT sub_end FROM users WHERE user_id=?", (uid,)).fetchone()
        base = (u["sub_end"] if u and u["sub_end"] > now_ms else now_ms)
        new_end = base + tariff["days"] * 86_400_000

        conn.execute("UPDATE users SET sub_end=? WHERE user_id=?", (new_end, uid))
        conn.execute("UPDATE payments SET status='paid' WHERE invoice_id=?", (invoice_id,))

        if pay["promo_code"]:
            conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code=?", (pay["promo_code"],))
            conn.execute("INSERT OR IGNORE INTO promo_redemptions (code,user_id) VALUES (?,?)",
                         (pay["promo_code"], uid))

        # Referral 15%
        ref = conn.execute("SELECT referrer_id FROM users WHERE user_id=?", (uid,)).fetchone()
        reward_info = None
        if ref and ref["referrer_id"]:
            reward = round(float(pay["amount"]) * 0.15, 4)
            conn.execute(
                "UPDATE referrals SET reward=reward+? WHERE referrer_id=? AND referee_id=?",
                (reward, ref["referrer_id"], uid),
            )
            conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?",
                         (reward, ref["referrer_id"]))
            reward_info = {"referrer_id": ref["referrer_id"], "reward": reward}

        conn.commit()
        return {
            "user_id": uid,
            "tariff_name": tariff["name"],
            "tariff_days": tariff["days"],
            "new_end": new_end,
            "reward": reward_info,
            "amount": pay["amount"],
            "currency": pay["currency"],
        }
    finally:
        conn.close()


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # ── Реферальную ссылку разбираем и регистрируем ДО проверки подписки ──
    # (новый пользователь почти никогда ещё не подписан на канал — если
    # делать это после return'а из проверки подписки, ref_id теряется
    # навсегда: при повторном заходе через "Я подписался" в context.args
    # уже ничего нет, это отдельный callback, а не команда /start)
    args = ctx.args or []
    ref_id = None

    if args and args[0].startswith("ref_"):
        try:
            ref_id = int(args[0][4:])
            if ref_id == user.id:
                ref_id = None
        except ValueError:
            pass

    # Выполняем работу с БД синхронно (SQLite работает быстро)
    is_new_user = _ensure_user(user.id, user.username, user.first_name, ref_id)

    # ── Реферальная система: показываем переход сразу, не дожидаясь оплаты ──
    referral_greeting_extra = ""
    if is_new_user and ref_id:
        referral_greeting_extra = (
            "\n\n🔗 <i>Вы перешли по реферальной ссылке.</i> "
            "Оформите подписку — и ваш друг получит бонус!"
        )
        try:
            uname = f"@{user.username}" if user.username else (user.first_name or f"ID {user.id}")
            await ctx.bot.send_message(
                ref_id,
                f"👋 <b>По вашей ссылке перешёл новый пользователь!</b>\n\n"
                f"👤 {uname}\n\n"
                "Как только он оплатит подписку, вам будет начислено 15% от суммы на баланс.",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.warning("Referral join notify to %s failed: %s", ref_id, e)

    if not await _is_subscribed(ctx.bot, user.id):
        await update.message.reply_photo(
            photo=START_PHOTO_URL,
            caption=(
                "⚡️ <b>Добро пожаловать в VpnDotaBot!</b> ⚡️\n\n"
                "Чтобы пользоваться ботом, подпишитесь на наш канал с новостями 👇"
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=_sub_required_kb(),
        )
        return

    # Выдаем триал без блокировки основного потока бота (УСКОРЕНИЕ ОТКЛИКА)
    if TRIAL_DAYS > 0:
        u = _get_user(user.id)
        if u and not u.get("sub_end"):
            exp_ms = int((time.time() + TRIAL_DAYS * 86400) * 1000)

            # Запускаем создание клиента в XUI в фоне
            asyncio.create_task(asyncio.to_thread(_provision_xui, user.id, exp_ms))

            conn = get_db()
            conn.execute("UPDATE users SET sub_end=? WHERE user_id=?", (exp_ms, user.id))
            conn.commit()
            conn.close()

    # Крутое стартовое сообщение
    greeting_text = (
        "⚡️ <b>Добро пожаловать в VpnDotaBot!</b> ⚡️\n\n"
        "🛡 <b>Надежно. Быстро. Безопасно.</b>\n"
        "└ Обход блокировок, защита трафика и минимальный пинг."
        f"{referral_greeting_extra}\n\n"
    )

    await update.message.reply_photo(
        photo=START_PHOTO_URL,
        caption=greeting_text,
        reply_markup=_start_kb(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = _get_user(update.effective_user.id)
    sub_end = u.get("sub_end", 0) if u else 0
    now_ms = int(time.time() * 1000)
    status = f"✅ До <b>{_dt(sub_end)}</b>" if sub_end > now_ms else "🔴 Не активна"
    await update.message.reply_html(
        f"<b>VpnDota</b> · {status}",
        reply_markup=_kb("🛡 Открыть панель"),
    )


async def cmd_config(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_config(update.effective_chat.id, update.effective_user.id, ctx.bot)


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await _send_stats(update.effective_chat.id, update.effective_user.id, ctx.bot)


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = _get_user(uid)
    if not u:
        await update.message.reply_text("❌ Нажмите /start")
        return
    now_ms = int(time.time() * 1000)
    sub_end = u.get("sub_end", 0)
    active = sub_end > now_ms
    days = max(0, int((sub_end - now_ms) / 86_400_000)) if active else 0
    conn = get_db()
    ref = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(reward),0) AS earned FROM referrals WHERE referrer_id=?",
        (uid,)
    ).fetchone()
    conn.close()
    sub_str = f"✅ до {_dt(sub_end)} ({days} дн.)" if active else "🔴 не активна"
    await update.message.reply_html(
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👤 {update.effective_user.first_name}\n\n"
        f"🔐 Подписка: {sub_str}\n"
        f"💰 Баланс: <b>${u.get('balance', 0):.2f}</b>\n"
        f"🔗 Рефералов: <b>{ref['cnt']}</b>  Заработано: <b>${ref['earned']:.2f}</b>",
    )


# ── Admin: клавиатуры ─────────────────────────────────────────────────────────

def _adm_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Статистика сети", callback_data="adm_stats"),
            InlineKeyboardButton("💰 Тарифы", callback_data="adm_tariffs"),
        ],
        [
            InlineKeyboardButton("👤 Пользователь", callback_data="adm_user"),
            InlineKeyboardButton("🎟 Промокоды", callback_data="adm_promo"),
        ],
        [
            InlineKeyboardButton("📜 Логи бота", callback_data="adm_logs"),
            InlineKeyboardButton("📢 Рассылка", callback_data="adm_broadcast"),
        ],
    ])


def _adm_back_kb(extra: list = None) -> InlineKeyboardMarkup:
    rows = extra or []
    rows.append([InlineKeyboardButton("⬅️ В меню", callback_data="adm_back")])
    return InlineKeyboardMarkup(rows)


def _adm_tariff_card_kb(t: dict) -> InlineKeyboardMarkup:
    tid = t["id"]
    status = "✅ Включён" if t["active"] else "🚫 Выключен"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✏️ Цена", callback_data=f"adm_tprc_{tid}"),
            InlineKeyboardButton("📅 Дни", callback_data=f"adm_tdys_{tid}"),
            InlineKeyboardButton("📦 GB", callback_data=f"adm_tgb_{tid}"),
        ],
        [InlineKeyboardButton(status, callback_data=f"adm_ttgl_{tid}")],
        [
            InlineKeyboardButton("⬅️ К тарифам", callback_data="adm_tariffs"),
            InlineKeyboardButton("🏠 В меню", callback_data="adm_back"),
        ],
    ])


def _tariff_card_text(t: dict) -> str:
    gb = "безлимит" if not t["gb_limit"] else f"{t['gb_limit']} GB"
    status = "✅ включён" if t["active"] else "🚫 выключен"
    return (
        f"💰 <b>{t['name']}</b>  <code>#{t['id']}</code>\n\n"
        f"Цена: <b>${t['price_usd']}</b>\n"
        f"Срок: <b>{t['days']} дн.</b>\n"
        f"Лимит: <b>{gb}</b>\n"
        f"Статус: {status}"
    )


def _adm_user_card_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ 7 дней", callback_data=f"adm_u7_{uid}"),
            InlineKeyboardButton("➕ 30 дней", callback_data=f"adm_u30_{uid}"),
        ],
        [InlineKeyboardButton("✏️ Своё число дней", callback_data=f"adm_ux_{uid}")],
        [InlineKeyboardButton("❌ Забрать подписку", callback_data=f"adm_urv_{uid}")],
        [
            InlineKeyboardButton("🔎 Другой пользователь", callback_data="adm_user"),
            InlineKeyboardButton("🏠 В меню", callback_data="adm_back"),
        ],
    ])


def _user_card_text(u: dict) -> str:
    now_ms = int(time.time() * 1000)
    sub_end = u.get("sub_end", 0) or 0
    sub_str = f"✅ до {_dt(sub_end)}" if sub_end > now_ms else "🔴 не активна"
    uname = f"@{u['username']}" if u.get("username") else "—"
    return (
        f"👤 <b>{u.get('first_name') or '—'}</b> ({uname})\n"
        f"🆔 <code>{u['user_id']}</code>\n\n"
        f"🔐 Подписка: {sub_str}\n"
        f"💰 Баланс: <b>${u.get('balance', 0):.2f}</b>"
    )


def _adm_promo_kb(rows) -> InlineKeyboardMarkup:
    kb = []
    for p in rows:
        used  = f"{p['used_count']}/{p['max_uses'] or '∞'}"
        icon  = "✅" if p["active"] else "🚫"
        value = f"-{int(p['discount_pct'])}%" if p["type"] == "discount" else f"+{int(p['days'])}д"
        kb.append([InlineKeyboardButton(
            f"{icon} {p['code']} · {value} · {used}",
            callback_data=f"adm_ptgl_{p['code']}",
        )])
    kb.append([InlineKeyboardButton("🎁 Создать: дни", callback_data="adm_pnew_days"),
               InlineKeyboardButton("💸 Создать: скидка", callback_data="adm_pnew_pct")])
    kb.append([InlineKeyboardButton("🏠 В меню", callback_data="adm_back")])
    return InlineKeyboardMarkup(kb)


def _find_user(query: str) -> Optional[dict]:
    conn = get_db()
    query = query.strip().lstrip("@")
    if query.isdigit():
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (int(query),)).fetchone()
    else:
        row = conn.execute("SELECT * FROM users WHERE username LIKE ? LIMIT 1", (query,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _grant_days(user_id: int, days: int) -> int:
    """Прибавляет (или отнимает, если days < 0) дни к подписке. Возвращает новый sub_end (мс)."""
    now_ms = int(time.time() * 1000)
    conn = get_db()
    u = conn.execute("SELECT sub_end FROM users WHERE user_id=?", (user_id,)).fetchone()
    base = (u["sub_end"] if u and u["sub_end"] and u["sub_end"] > now_ms else now_ms)
    new_end = max(0, base + days * 86_400_000)
    conn.execute("UPDATE users SET sub_end=? WHERE user_id=?", (new_end, user_id))
    conn.commit()
    conn.close()
    return new_end


def _revoke_sub(user_id: int):
    u = _get_user(user_id)
    conn = get_db()
    conn.execute("UPDATE users SET sub_end=0 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    if xui and u and u.get("client_email"):
        try:
            xui.delete_client(u["client_email"])
        except Exception as e:
            logger.warning("_revoke_sub: xui.delete_client failed for %s: %s", user_id, e)


# ── Admin: команды и колбэки ────────────────────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # Невидимая защита: если юзер не админ, бот просто игнорирует команду
    if update.effective_user.id != ADMIN_ID:
        return
    ctx.user_data["admin_state"] = None
    admin_text = (
        "👑 <b>Секретная Панель Администратора</b>\n\n"
        "Системы функционируют нормально. Выберите раздел для управления сервером и ботом:"
    )
    await update.message.reply_text(admin_text, reply_markup=_adm_main_kb(), parse_mode=ParseMode.HTML)


async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if update.effective_user.id != ADMIN_ID:
        return

    data = q.data

    # ── Главное меню / статистика / логи / рассылка (как раньше) ──────────────
    if data == "adm_back":
        ctx.user_data["admin_state"] = None
        await q.edit_message_text(
            "👑 <b>Секретная Панель Администратора</b>\n\nВыберите раздел:",
            reply_markup=_adm_main_kb(), parse_mode=ParseMode.HTML,
        )
        return

    if data == "adm_stats":
        conn = get_db()
        now_ms = int(time.time() * 1000)
        s = {
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
            "active": conn.execute("SELECT COUNT(*) FROM users WHERE sub_end>?", (now_ms,)).fetchone()[0],
            "income": conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'").fetchone()[0],
        }
        conn.close()
        await q.edit_message_text(
            f"📊 <b>Статистика</b>\n\n"
            f"Пользователей: {s['users']}\nАктивных: {s['active']}\nДоход: ${s['income']:.2f}",
            parse_mode=ParseMode.HTML, reply_markup=_adm_back_kb(),
        )
        return

    if data == "adm_logs":
        try:
            tail = "".join(open("bot.log", encoding="utf-8").readlines()[-30:])
            await q.edit_message_text(f"<pre>{tail[-3800:]}</pre>", parse_mode=ParseMode.HTML,
                                       reply_markup=_adm_back_kb())
        except FileNotFoundError:
            await q.edit_message_text("Лог не найден.", reply_markup=_adm_back_kb())
        return

    if data == "adm_broadcast":
        ctx.user_data["admin_state"] = "broadcast"
        await q.edit_message_text("📢 Введите текст рассылки:", reply_markup=_adm_back_kb())
        return

    # ── Тарифы ──────────────────────────────────────────────────────────────
    if data == "adm_tariffs":
        conn = get_db()
        rows = conn.execute("SELECT * FROM tariffs ORDER BY price_usd").fetchall()
        conn.close()
        kb = [[InlineKeyboardButton(
            f"{'✅' if t['active'] else '🚫'} {t['name']} — {t['days']}д — ${t['price_usd']}",
            callback_data=f"adm_tar_{t['id']}",
        )] for t in rows]
        await q.edit_message_text("💰 <b>Тарифы</b>\n\nВыберите тариф для редактирования:",
                                   parse_mode=ParseMode.HTML, reply_markup=_adm_back_kb(kb))
        return

    if data.startswith("adm_tar_"):
        tid = int(data.rsplit("_", 1)[1])
        conn = get_db()
        t = conn.execute("SELECT * FROM tariffs WHERE id=?", (tid,)).fetchone()
        conn.close()
        if not t:
            await q.edit_message_text("Тариф не найден.", reply_markup=_adm_back_kb())
            return
        await q.edit_message_text(_tariff_card_text(dict(t)), parse_mode=ParseMode.HTML,
                                   reply_markup=_adm_tariff_card_kb(dict(t)))
        return

    if data.startswith("adm_tprc_") or data.startswith("adm_tdys_") or data.startswith("adm_tgb_"):
        # определяем поле и id по префиксу
        if data.startswith("adm_tprc_"):
            field, tid = "price", int(data.split("_")[2])
            prompt = "Введите новую цену в USD (например 2.99):"
        elif data.startswith("adm_tdys_"):
            field, tid = "days", int(data.split("_")[2])
            prompt = "Введите новое количество дней:"
        else:
            field, tid = "gb", int(data.split("_")[2])
            prompt = "Введите лимит трафика в GB (0 = безлимит):"
        ctx.user_data["admin_state"] = f"tariff:{field}:{tid}"
        await q.edit_message_text(prompt, reply_markup=_adm_back_kb())
        return

    if data.startswith("adm_ttgl_"):
        tid = int(data.rsplit("_", 1)[1])
        conn = get_db()
        conn.execute("UPDATE tariffs SET active = 1 - active WHERE id=?", (tid,))
        conn.commit()
        t = conn.execute("SELECT * FROM tariffs WHERE id=?", (tid,)).fetchone()
        conn.close()
        await q.edit_message_text(_tariff_card_text(dict(t)), parse_mode=ParseMode.HTML,
                                   reply_markup=_adm_tariff_card_kb(dict(t)))
        return

    # ── Пользователь ────────────────────────────────────────────────────────
    if data == "adm_user":
        ctx.user_data["admin_state"] = "user:lookup"
        await q.edit_message_text("🔎 Отправьте ID или @username пользователя:", reply_markup=_adm_back_kb())
        return

    if data.startswith("adm_u7_") or data.startswith("adm_u30_"):
        uid = int(data.rsplit("_", 1)[1])
        days = 7 if data.startswith("adm_u7_") else 30
        new_end = _grant_days(uid, days)
        asyncio.create_task(asyncio.to_thread(_provision_xui, uid, new_end))
        try:
            await ctx.bot.send_message(
                uid, f"🎁 Администратор начислил вам <b>{days}</b> дней подписки!\n"
                     f"📅 Действует до <b>{_dt(new_end)}</b>", parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        u = _get_user(uid)
        await q.edit_message_text(f"✅ Начислено {days} дн.\n\n" + _user_card_text(u),
                                   parse_mode=ParseMode.HTML, reply_markup=_adm_user_card_kb(uid))
        return

    if data.startswith("adm_ux_"):
        uid = int(data.rsplit("_", 1)[1])
        ctx.user_data["admin_state"] = f"user:days:{uid}"
        await q.edit_message_text(
            "Введите количество дней (можно отрицательное число, чтобы отнять):",
            reply_markup=_adm_back_kb(),
        )
        return

    if data.startswith("adm_urv_"):
        uid = int(data.rsplit("_", 1)[1])
        _revoke_sub(uid)
        try:
            await ctx.bot.send_message(uid, "⛔ Ваша подписка была отменена администратором.")
        except Exception:
            pass
        u = _get_user(uid)
        await q.edit_message_text("✅ Подписка отозвана.\n\n" + _user_card_text(u),
                                   parse_mode=ParseMode.HTML, reply_markup=_adm_user_card_kb(uid))
        return

    # ── Промокоды ───────────────────────────────────────────────────────────
    if data == "adm_promo":
        conn = get_db()
        rows = conn.execute("SELECT * FROM promocodes ORDER BY created_at DESC").fetchall()
        conn.close()
        text = "🎟 <b>Промокоды</b>\n\n" + ("Пока не создано ни одного." if not rows else
                                             "Нажмите на код, чтобы включить/выключить его.")
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=_adm_promo_kb(rows))
        return

    if data == "adm_pnew_days":
        ctx.user_data["admin_state"] = "promo:code"
        ctx.user_data["promo_draft"] = {"type": "days"}
        await q.edit_message_text(
            "Введите код промокода (латиница/цифры, без пробелов, например SUMMER30):",
            reply_markup=_adm_back_kb(),
        )
        return

    if data == "adm_pnew_pct":
        ctx.user_data["admin_state"] = "promo:code"
        ctx.user_data["promo_draft"] = {"type": "discount"}
        await q.edit_message_text(
            "Введите код промокода (латиница/цифры, без пробелов, например SALE20):",
            reply_markup=_adm_back_kb(),
        )
        return

    if data.startswith("adm_ptgl_"):
        code = data[len("adm_ptgl_"):]
        conn = get_db()
        conn.execute("UPDATE promocodes SET active = 1 - active WHERE code=?", (code,))
        conn.commit()
        rows = conn.execute("SELECT * FROM promocodes ORDER BY created_at DESC").fetchall()
        conn.close()
        await q.edit_message_text("🎟 <b>Промокоды</b>\n\nНажмите на код, чтобы включить/выключить его.",
                                   parse_mode=ParseMode.HTML, reply_markup=_adm_promo_kb(rows))
        return


async def handle_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    state = ctx.user_data.get("admin_state")
    if not state:
        return
    text = (update.message.text or "").strip()

    # ── Рассылка ────────────────────────────────────────────────────────────
    if state == "broadcast":
        ctx.user_data["admin_state"] = None
        conn = get_db()
        ids = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
        conn.close()
        sent = failed = 0
        for uid in ids:
            try:
                await ctx.bot.send_message(uid, text)
                sent += 1
                await asyncio.sleep(0.04)
            except Exception:
                failed += 1
        await update.message.reply_text(f"✅ Отправлено: {sent}, ошибок: {failed}",
                                         reply_markup=_adm_back_kb())
        return

    # ── Редактирование тарифа ──────────────────────────────────────────────
    if state.startswith("tariff:"):
        _, field, tid_s = state.split(":")
        tid = int(tid_s)
        try:
            value = float(text.replace(",", ".")) if field == "price" else int(text)
        except ValueError:
            await update.message.reply_text("❌ Введите число. Попробуйте ещё раз:")
            return
        col = {"price": "price_usd", "days": "days", "gb": "gb_limit"}[field]
        if field == "days" and value <= 0:
            await update.message.reply_text("❌ Количество дней должно быть больше нуля. Попробуйте ещё раз:")
            return
        if field == "price" and value <= 0:
            await update.message.reply_text("❌ Цена должна быть больше нуля. Попробуйте ещё раз:")
            return
        conn = get_db()
        conn.execute(f"UPDATE tariffs SET {col}=? WHERE id=?", (value, tid))
        conn.commit()
        t = conn.execute("SELECT * FROM tariffs WHERE id=?", (tid,)).fetchone()
        conn.close()
        ctx.user_data["admin_state"] = None
        await update.message.reply_text("✅ Обновлено.\n\n" + _tariff_card_text(dict(t)),
                                         parse_mode=ParseMode.HTML, reply_markup=_adm_tariff_card_kb(dict(t)))
        return

    # ── Поиск пользователя ─────────────────────────────────────────────────
    if state == "user:lookup":
        u = _find_user(text)
        if not u:
            await update.message.reply_text("❌ Пользователь не найден. Отправьте ID или @username ещё раз:")
            return
        ctx.user_data["admin_state"] = None
        await update.message.reply_text(_user_card_text(u), parse_mode=ParseMode.HTML,
                                         reply_markup=_adm_user_card_kb(u["user_id"]))
        return

    if state.startswith("user:days:"):
        uid = int(state.split(":")[2])
        try:
            days = int(text)
        except ValueError:
            await update.message.reply_text("❌ Введите целое число (можно отрицательное). Попробуйте ещё раз:")
            return
        new_end = _grant_days(uid, days)
        ctx.user_data["admin_state"] = None
        if days > 0:
            asyncio.create_task(asyncio.to_thread(_provision_xui, uid, new_end))
            try:
                await ctx.bot.send_message(
                    uid, f"🎁 Администратор начислил вам <b>{days}</b> дней подписки!\n"
                         f"📅 Действует до <b>{_dt(new_end)}</b>", parse_mode=ParseMode.HTML,
                )
            except Exception:
                pass
        u = _get_user(uid)
        await update.message.reply_text("✅ Готово.\n\n" + _user_card_text(u), parse_mode=ParseMode.HTML,
                                         reply_markup=_adm_user_card_kb(uid))
        return

    # ── Создание промокода (мини-мастер) ────────────────────────────────────
    if state == "promo:code":
        code = text.upper().replace(" ", "")
        if not code.isalnum():
            await update.message.reply_text("❌ Только буквы/цифры, без пробелов и символов. Попробуйте ещё раз:")
            return
        conn = get_db()
        exists = conn.execute("SELECT 1 FROM promocodes WHERE code=?", (code,)).fetchone()
        conn.close()
        if exists:
            await update.message.reply_text("❌ Такой код уже существует. Введите другой:")
            return
        ctx.user_data["promo_draft"]["code"] = code
        if ctx.user_data["promo_draft"]["type"] == "discount":
            ctx.user_data["admin_state"] = "promo:pct"
            await update.message.reply_text("Какую скидку давать в процентах? (например 20)")
        else:
            ctx.user_data["admin_state"] = "promo:days"
            await update.message.reply_text("Сколько дней подписки начислять за активацию?")
        return

    if state == "promo:days":
        try:
            days = int(text)
            assert days > 0
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Введите целое число дней больше нуля:")
            return
        ctx.user_data["promo_draft"]["days"] = days
        ctx.user_data["admin_state"] = "promo:uses"
        await update.message.reply_text("Сколько раз можно использовать код? (0 = без лимита)")
        return

    if state == "promo:pct":
        try:
            pct = int(text)
            assert 0 < pct <= 100
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Введите процент скидки от 1 до 100:")
            return
        ctx.user_data["promo_draft"]["discount_pct"] = pct
        ctx.user_data["admin_state"] = "promo:uses"
        await update.message.reply_text("Сколько раз можно использовать код? (0 = без лимита)")
        return

    if state == "promo:uses":
        try:
            uses = int(text)
            assert uses >= 0
        except (ValueError, AssertionError):
            await update.message.reply_text("❌ Введите целое число ≥ 0:")
            return
        draft = ctx.user_data.pop("promo_draft", {})
        ctx.user_data["admin_state"] = None
        conn = get_db()
        conn.execute(
            "INSERT INTO promocodes (code,type,days,discount_pct,max_uses,active) VALUES (?,?,?,?,?,1)",
            (draft["code"], draft["type"], draft.get("days", 0), draft.get("discount_pct", 0), uses),
        )
        conn.commit()
        rows = conn.execute("SELECT * FROM promocodes ORDER BY created_at DESC").fetchall()
        conn.close()
        if draft["type"] == "discount":
            summary = f"скидка {draft['discount_pct']}%"
        else:
            summary = f"+{draft['days']} дн."
        await update.message.reply_text(
            f"✅ Промокод <code>{draft['code']}</code> создан: {summary}, "
            f"лимит {uses or '∞'}.", parse_mode=ParseMode.HTML, reply_markup=_adm_promo_kb(rows),
        )
        return


# ── Промокоды: активация пользователем ──────────────────────────────────────

def _redeem_days_promo(code: str, uid: int, username, first_name) -> str:
    """Общая логика активации бонусных дней по коду — используется и в /promo,
    и при обычной отправке кода текстом в чат. Возвращает готовый текст ответа."""
    conn = get_db()
    p = conn.execute("SELECT * FROM promocodes WHERE code=?", (code,)).fetchone()
    if not p or not p["active"]:
        conn.close()
        return "❌ Промокод не найден или отключён."
    if p["type"] != "days":
        conn.close()
        return "💸 Этот промокод даёт скидку на оплату — примените его на экране оплаты в панели."
    if p["max_uses"] and p["used_count"] >= p["max_uses"]:
        conn.close()
        return "❌ У этого промокода закончились активации."
    already = conn.execute(
        "SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?", (code, uid)
    ).fetchone()
    if already:
        conn.close()
        return "❌ Вы уже активировали этот промокод."

    conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code=?", (code,))
    conn.execute("INSERT INTO promo_redemptions (code,user_id) VALUES (?,?)", (code, uid))
    conn.commit()
    days = p["days"]
    conn.close()

    _ensure_user(uid, username, first_name)
    new_end = _grant_days(uid, days)
    asyncio.create_task(asyncio.to_thread(_provision_xui, uid, new_end))

    return (
        f"🎉 Промокод активирован! Начислено <b>{days}</b> дней.\n"
        f"📅 Подписка действует до <b>{_dt(new_end)}</b>"
    )


async def cmd_promo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    args = ctx.args or []
    if not args:
        await update.message.reply_text("Использование: <code>/promo КОД</code>", parse_mode=ParseMode.HTML)
        return
    user = update.effective_user
    code = args[0].strip().upper()
    reply = _redeem_days_promo(code, user.id, user.username, user.first_name)
    await update.message.reply_html(reply)


async def handle_plain_promo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Позволяет активировать промокод, просто прислав его текстом в чат —
    без команды /promo. Если текст не похож ни на один существующий код,
    молчим (не спамим ошибкой на каждое обычное сообщение)."""
    text = (update.message.text or "").strip().upper().replace(" ", "")
    if not text or not text.isalnum() or len(text) > 32:
        return

    conn = get_db()
    exists = conn.execute("SELECT 1 FROM promocodes WHERE code=?", (text,)).fetchone()
    conn.close()
    if not exists:
        return  # не похоже на промокод — просто игнорируем, это не ошибка

    user = update.effective_user
    reply = _redeem_days_promo(text, user.id, user.username, user.first_name)
    await update.message.reply_html(reply)


# ── WebApp data ───────────────────────────────────────────────────────────────

async def webapp_data_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = update.effective_chat.id
    bot = ctx.bot

    try:
        data = json.loads(update.effective_message.web_app_data.data)
    except Exception:
        return

    action = data.get("action", "")
    logger.info("WebApp %s from %s", action, user.id)

    _ensure_user(user.id, user.username, user.first_name)

    if action == "get_config":
        await _send_config(chat_id, user.id, bot)
    elif action == "get_stats":
        await _send_stats(chat_id, user.id, bot)
    elif action == "buy":
        await bot.send_message(chat_id, "💳 Используйте панель для оплаты.", reply_markup=_kb("💳 Открыть панель"))
    elif action == "referral_withdraw":
        await _process_withdrawal(chat_id, user.id, float(data.get("amount", 0)), data.get("wallet", ""), bot)


async def cb_check_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("⏳ Проверяем...")
    invoice_id = q.data.replace("check_", "")
    uid = update.effective_user.id

    import aiohttp
    status = None
    if CRYPTO_TOKEN:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                        "https://pay.crypt.bot/api/getInvoices",
                        headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN},
                        params={"invoice_ids": invoice_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    d = await resp.json()
                    if d.get("ok") and d["result"]["items"]:
                        status = d["result"]["items"][0]["status"]
        except Exception as e:
            logger.error("check invoice: %s", e)

    if status == "paid":
        result = _activate_payment(invoice_id)
        if result:
            _provision_xui(result["user_id"], result["new_end"])
            if result.get("reward"):
                rw = result["reward"]
                try:
                    await ctx.bot.send_message(
                        rw["referrer_id"],
                        f"🎉 Реферал оплатил подписку!\n💵 Начислено <b>${rw['reward']:.2f}</b>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

        await q.edit_message_text(
            f"✅ <b>Оплата подтверждена!</b>\n\n"
            f"📅 До <b>{_dt(result['new_end'])}</b>" if result else "✅ Подписка активирована.",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb("🔐 Получить конфиг"),
        )
    elif status == "expired":
        await q.edit_message_text("❌ Счёт истёк. Создайте новый в панели.")
    else:
        await q.answer("⏳ Оплата ещё не поступила. Попробуйте позже.", show_alert=True)




async def _send_config(chat_id: int, uid: int, bot: Bot):
    import io
    try:
        import qrcode
        has_qr = True
    except ImportError:
        has_qr = False

    u = _get_user(uid)
    if not u or not u.get("uuid"):
        await bot.send_message(
            chat_id,
            "❌ <b>Нет активной подписки.</b>\nКупите тариф в панели.",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb("💳 Купить"),
        )
        return

    now_ms = int(time.time() * 1000)
    if u.get("sub_end", 0) < now_ms:
        await bot.send_message(
            chat_id, "⛔ <b>Подписка истекла.</b> Продлите в панели.",
            parse_mode=ParseMode.HTML, reply_markup=_kb("🔄 Продлить"),
        )
        return

    link = _get_vless_link(u)
    if not link:
        await bot.send_message(
            chat_id,
            "⚠️ Конфиг временно недоступен.\n"
            "Проверьте настройки 3X-UI или переменные XUI_HOST / XUI_PBK в .env",
        )
        return

    expiry = _dt(u["sub_end"]) if u.get("sub_end") else "∞"
    caption = (
        f"🔐 <b>Ваш VLESS конфиг</b>\n\n"
        f"📅 Действует до: <b>{expiry}</b>\n\n"
        f"<code>{link}</code>\n\n"
        "━━━━━━━━━━━━━━━━━\n"
        "📱 <b>Как подключиться:</b>\n"
        "• Android → Happ / NekoBox\n"
        "• iOS → Streisand / FoXray\n"
        "• PC → Hiddify / Happ\n\n"
        "Скопируй ссылку и импортируй в приложение."
    )

    if has_qr:
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="white", back_color="black")
        buf = io.BytesIO()
        img.save(buf, "PNG")
        buf.seek(0)
        await bot.send_photo(chat_id, buf, caption=caption, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(chat_id, caption, parse_mode=ParseMode.HTML)


async def _send_stats(chat_id: int, uid: int, bot: Bot):
    u = _get_user(uid)
    if not u or not u.get("client_email"):
        await bot.send_message(chat_id, "❌ Нет активной подписки.")
        return

    tup = tdw = 0.0
    if xui:
        try:
            st = xui.get_client_stats(u["client_email"])
            if st:
                tup = st.get("up", 0) / 1024 ** 3
                tdw = st.get("down", 0) / 1024 ** 3
        except Exception:
            pass

    expiry = _dt(u["sub_end"]) if u.get("sub_end") else "∞"
    await bot.send_message(
        chat_id,
        f"📊 <b>Статистика трафика</b>\n\n"
        f"⬆️ Отправлено: <b>{tup:.2f} GB</b>\n"
        f"⬇️ Получено:   <b>{tdw:.2f} GB</b>\n"
        f"📦 Итого:      <b>{tup + tdw:.2f} GB</b>\n\n"
        f"📅 Подписка до: <b>{expiry}</b>",
        parse_mode=ParseMode.HTML,
    )


async def _process_withdrawal(chat_id: int, uid: int, amount: float, wallet: str, bot: Bot):
    u = _get_user(uid)
    if not u:
        await bot.send_message(chat_id, "❌ Профиль не найден.")
        return
    if amount < 5:
        await bot.send_message(chat_id, "❌ Минимум $5.00 USDT")
        return
    if amount > (u.get("balance") or 0):
        await bot.send_message(chat_id, f"❌ На балансе ${u.get('balance', 0):.2f}")
        return
    if not wallet:
        await bot.send_message(chat_id, "❌ Укажите кошелёк.")
        return
    if ADMIN_ID:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💸 <b>Вывод</b>\n\n{uid} @{u.get('username', '—')}\n"
                f"${amount:.2f} USDT\n<code>{wallet}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    await bot.send_message(chat_id, "✅ Заявка принята! Обработка до 24 ч.")


# ── Callback для проверки подписки на канал ──────────────────────────────────────

async def cb_check_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = update.effective_user

    if not await _is_subscribed(ctx.bot, user.id):
        await q.answer("❌ Вы всё ещё не подписаны на канал.", show_alert=True)
        return

    await q.answer("✅ Подписка подтверждена!")

    is_new_user = _ensure_user(user.id, user.username, user.first_name)

    if TRIAL_DAYS > 0:
        u = _get_user(user.id)
        if u and not u.get("sub_end"):
            exp_ms = int((time.time() + TRIAL_DAYS * 86400) * 1000)
            asyncio.create_task(asyncio.to_thread(_provision_xui, user.id, exp_ms))
            conn = get_db()
            conn.execute("UPDATE users SET sub_end=? WHERE user_id=?", (exp_ms, user.id))
            conn.commit()
            conn.close()

    greeting_text = (
        "⚡️ <b>Добро пожаловать в VpnDotaBot!</b> ⚡️\n\n"
        "🛡 <b>Надежно. Быстро. Безопасно.</b>\n"
        "└ Обход блокировок, защита трафика и минимальный пинг.\n\n"
    )

    await q.edit_message_caption(caption=greeting_text, reply_markup=_start_kb(), parse_mode=ParseMode.HTML)


# ── Callback для кнопки "Политика и соглашение" ──────────────────────────────────

async def cb_show_policies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📄 Политика конфиденциальности",
                              url="telegra.ph/Polzovatelskoe-soglashenie-VpnDotaBot-07-17")],
        [InlineKeyboardButton("📑 Договор оферты", url="https://telegra.ph/DOGOVOR-OFERTY-03-19-5")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_to_start")]
    ])
    await q.edit_message_caption(
        caption=(
            "📚 <b>Пользовательское соглашение</b>\n\n"
            "Пожалуйста, внимательно ознакомьтесь с документами ниже:"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


async def cb_back_to_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    greeting_text = (
        "⚡️ <b>Добро пожаловать в VpnDota!</b> ⚡️\n\n"
        "🔮 <i>Ваш портал в свободный интернет без границ.</i>\n\n"
        "🛡 <b>Надежно. Быстро. Безопасно.</b>\n"
        "└ Обход блокировок, защита трафика и минимальный пинг.\n\n"
        "👇 <i>Нажмите кнопку ниже, чтобы открыть панель управления:</i>"
    )
    await q.edit_message_caption(caption=greeting_text, reply_markup=_start_kb(), parse_mode=ParseMode.HTML)


# ── Telegram Stars Payments ───────────────────────────────────────────────────

async def pre_checkout_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Approve all Stars pre-checkout queries."""
    query = update.pre_checkout_query
    try:
        payload = json.loads(query.invoice_payload)
    except Exception:
        await query.answer(ok=False, error_message="Неверный payload")
        return

    # Validate required fields
    if not all(k in payload for k in ("user_id", "tariff_id", "type")):
        await query.answer(ok=False, error_message="Неверные данные платежа")
        return

    if payload.get("type") != "vpn_subscription":
        await query.answer(ok=False, error_message="Неизвестный тип платежа")
        return

    tariff = None
    conn = get_db()
    try:
        tariff = conn.execute("SELECT * FROM tariffs WHERE id=?", (payload["tariff_id"],)).fetchone()
    finally:
        conn.close()

    if not tariff:
        await query.answer(ok=False, error_message="Тариф не найден")
        return

    await query.answer(ok=True)


async def successful_payment_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle confirmed Stars payment — activate subscription.

    Инвойс (и запись в payments) уже создан сервером в /api/create_invoice
    со статусом 'pending' и invoice_id, зашитым в payload. Здесь мы просто
    находим эту запись по invoice_id из payload и активируем её через тот
    же _activate_payment(), что использует крипто-оплата — это гарантирует
    одинаковую логику начисления рефералки и отсутствие дублей.
    """
    msg = update.effective_message
    payment = msg.successful_payment
    user = update.effective_user
    stars_amount = payment.total_amount  # в Stars

    try:
        payload = json.loads(payment.invoice_payload)
    except Exception:
        logger.error("Stars: invalid payload from user %s", user.id)
        return

    invoice_id = payload.get("invoice_id")
    user_id = int(payload.get("user_id", user.id))
    tariff_id = payload.get("tariff_id")

    if not invoice_id:
        logger.error("Stars: payload without invoice_id from user %s", user.id)
        return

    logger.info("Stars payment: user=%s tariff=%s stars=%s invoice=%s",
                user_id, tariff_id, stars_amount, invoice_id)

    # На случай, если по какой-то причине записи ещё нет (например, сервер
    # был недоступен в момент создания счёта) — создаём её "на лету".
    conn = get_db()
    try:
        existing = conn.execute("SELECT 1 FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
        if not existing:
            amount_usd = round(stars_amount * 0.019, 4)
            conn.execute(
                "INSERT OR IGNORE INTO payments (invoice_id, user_id, tariff_id, amount, currency, status, created_at) "
                "VALUES (?,?,?,?,'XTR','pending',?)",
                (invoice_id, user_id, tariff_id, amount_usd, int(time.time() * 1000)),
            )
            conn.commit()
    finally:
        conn.close()

    result = _activate_payment(invoice_id)
    if not result:
        logger.warning("Stars: invoice %s already activated or not found", invoice_id)
        return

    # Provision XUI in background
    asyncio.create_task(asyncio.to_thread(_provision_xui, result["user_id"], result["new_end"]))

    # Notify referrer
    if result.get("reward"):
        rw = result["reward"]
        try:
            await ctx.bot.send_message(
                rw["referrer_id"],
                f"🎉 Реферал оплатил ⭐ Stars!\n💵 Начислено <b>${rw['reward']:.2f}</b>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass

    # Notify user
    exp_str = datetime.fromtimestamp(result["new_end"] / 1000).strftime("%d.%m.%Y")
    try:
        await ctx.bot.send_message(
            result["user_id"],
            f"✅ <b>Оплата Stars прошла!</b>\n\n"
            f"⭐ Списано: <b>{stars_amount} Stars</b>\n"
            f"📦 Тариф: <b>{result['tariff_name']}</b>\n"
            f"📅 Подписка до: <b>{exp_str}</b>\n\n"
            "Нажмите кнопку для получения конфига:",
            parse_mode=ParseMode.HTML,
            reply_markup=_kb("🔐 Получить конфиг"),
        )
    except Exception as e:
        logger.error("Stars: notify user %s failed: %s", result["user_id"], e)

    logger.info("Stars: activated user=%s until=%s", result["user_id"], exp_str)


# ── Background jobs ───────────────────────────────────────────────────────────

async def _job_poll_payments(bot: Bot):
    """Poll pending payments every 15 seconds."""
    import aiohttp
    while True:
        try:
            conn = get_db()
            pending = conn.execute(
                "SELECT * FROM payments WHERE status='pending' ORDER BY created_at"
            ).fetchall()
            conn.close()

            for pay in pending:
                inv_id = pay["invoice_id"]
                try:
                    async with aiohttp.ClientSession() as s:
                        async with s.get(
                                "https://pay.crypt.bot/api/getInvoices",
                                headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN},
                                params={"invoice_ids": inv_id},
                                timeout=aiohttp.ClientTimeout(total=8),
                        ) as resp:
                            d = await resp.json()
                            if not d.get("ok") or not d["result"]["items"]:
                                continue
                            status = d["result"]["items"][0]["status"]
                    if status == "paid":
                        result = _activate_payment(inv_id)
                        if result:
                            _provision_xui(result["user_id"], result["new_end"])
                            if result.get("reward"):
                                rw = result["reward"]
                                try:
                                    await bot.send_message(
                                        rw["referrer_id"],
                                        f"🎉 Реферал оплатил!\n💵 <b>${rw['reward']:.2f}</b>",
                                        parse_mode=ParseMode.HTML,
                                    )
                                except Exception:
                                    pass
                            try:
                                await bot.send_message(
                                    result["user_id"],
                                    f"✅ <b>Подписка активирована!</b>\n\n"
                                    f"📦 {result['tariff_name']}\n"
                                    f"📅 До <b>{_dt(result['new_end'])}</b>\n\n"
                                    "Нажмите кнопку для получения конфига:",
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=_kb("🔐 Получить конфиг"),
                                )
                            except Exception:
                                pass
                        logger.info("Poller activated invoice %s", inv_id)
                except Exception as e:
                    logger.error("Poll invoice %s: %s", inv_id, e)
        except Exception as e:
            logger.error("Payment poller error: %s", e)

        await asyncio.sleep(15)


async def _job_expire_subs():
    while True:
        try:
            now_ms = int(time.time() * 1000)
            conn = get_db()
            expired = conn.execute(
                "SELECT * FROM users WHERE sub_end > 0 AND sub_end < ?", (now_ms,)
            ).fetchall()
            conn.close()
            for u in expired:
                if xui and u["client_email"]:
                    try:
                        xui.delete_client(u["client_email"])
                    except Exception:
                        pass
                conn2 = get_db()
                conn2.execute("UPDATE users SET sub_end=0 WHERE user_id=?", (u["user_id"],))
                conn2.commit()
                conn2.close()
                logger.info("Deactivated user %s", u["user_id"])
        except Exception as e:
            logger.error("Expire job: %s", e)
        await asyncio.sleep(300)



def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    from server import run_server
    threading.Thread(
        target=run_server,
        kwargs={"host": "0.0.0.0", "port": int(os.getenv("WEBAPP_PORT", "8080"))},
        daemon=True, name="flask",
    ).start()
    logger.info("Flask started on port %s", os.getenv("WEBAPP_PORT", "8080"))

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("config", cmd_config))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("promo", cmd_promo))

    app.add_handler(CallbackQueryHandler(cb_admin, pattern="^adm_"))
    app.add_handler(CallbackQueryHandler(cb_check_sub, pattern="^check_sub$"))
    app.add_handler(CallbackQueryHandler(cb_check_payment, pattern="^check_"))
    app.add_handler(CallbackQueryHandler(cb_show_policies, pattern="^show_policies$"))
    app.add_handler(CallbackQueryHandler(cb_back_to_start, pattern="^back_to_start$"))

    # Telegram Stars
    app.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, webapp_data_handler))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.User(ADMIN_ID) & ~filters.COMMAND,
        handle_admin_text,
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.User(ADMIN_ID) & ~filters.COMMAND,
        handle_plain_promo,
    ))

    # Start async background jobs inside the bot's event loop
    async def _post_init(application: Application):
        asyncio.create_task(_job_expire_subs())
        asyncio.create_task(_job_poll_payments(application.bot))
        logger.info("Background jobs started")

    app.post_init = _post_init

    logger.info("Bot started. WebApp: %s", WEBAPP_URL)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()