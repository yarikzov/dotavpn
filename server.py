"""
server.py — Flask WebApp + REST API
ИСПРАВЛЕНО: app.run() перенесён в конец, все роуты теперь регистрируются
"""

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import sqlite3
import time
import os
import json
import math
import uuid as _uuid
import hashlib
import hmac as _hmac
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

try:
    from xui_api import XUIApi
    xui = XUIApi()
    XUI_OK = True
except Exception:
    xui = None
    XUI_OK = False

app = Flask(__name__, static_folder="webapp")
CORS(app)

DB_PATH       = "vpn_bot.db"
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
CRYPTO_TOKEN  = os.getenv("CRYPTO_PAY_TOKEN", "")
BOT_USERNAME  = os.getenv("BOT_USERNAME", "VpnDotaBot")
STARS_RATE    = 0.019  # 1 Star ≈ $0.019 (официальный курс Telegram)


# ── Курс USD→RUB (ЦБ РФ) ────────────────────────────────────────────────────

# ── DB ────────────────────────────────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            first_name  TEXT,
            uuid        TEXT,
            client_email TEXT,
            sub_end     INTEGER DEFAULT 0,
            balance     REAL    DEFAULT 0,
            referrer_id INTEGER,
            created_at  INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS tariffs (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT,
            days      INTEGER,
            price_usd REAL,
            gb_limit  INTEGER DEFAULT 0,
            active    INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS payments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id   TEXT UNIQUE,
            user_id      INTEGER,
            tariff_id    INTEGER,
            amount       REAL,
            currency     TEXT DEFAULT 'USDT',
            status       TEXT DEFAULT 'pending',
            provider_ref TEXT,
            created_at   INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referee_id  INTEGER UNIQUE,
            reward      REAL DEFAULT 0,
            created_at  INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS promocodes (
            code       TEXT PRIMARY KEY,
            days       INTEGER NOT NULL,
            max_uses   INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            active     INTEGER DEFAULT 1,
            created_at INTEGER DEFAULT (strftime('%s','now'))
        );
        CREATE TABLE IF NOT EXISTS promo_redemptions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT,
            user_id     INTEGER,
            redeemed_at INTEGER DEFAULT (strftime('%s','now')),
            UNIQUE(code, user_id)
        );
    """)
    # Миграция для БД, созданных до появления оплаты в рублях
    try:
        conn.execute("ALTER TABLE payments ADD COLUMN provider_ref TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже существует

    # Миграция для БД, созданных до промокодов-скидок
    for stmt in (
        "ALTER TABLE promocodes ADD COLUMN type TEXT DEFAULT 'days'",
        "ALTER TABLE promocodes ADD COLUMN discount_pct INTEGER DEFAULT 0",
        "ALTER TABLE payments ADD COLUMN promo_code TEXT",
    ):
        try:
            conn.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass  # колонка уже существует

    # Seed tariffs
    if conn.execute("SELECT COUNT(*) FROM tariffs").fetchone()[0] == 0:
        conn.executemany(
            "INSERT INTO tariffs (name, days, price_usd, gb_limit) VALUES (?,?,?,?)",
            [
                ("🚀 Личный · 1 мес",        30,  1.99, 0),
                ("👨‍👩‍👧 Семейный · 1 мес",      30,  3.99, 0),
                ("💎 Максимальный · 1 мес",  30,  4.99, 0),
                ("🎮 Игровой · 1 мес",        30,  3.99, 0),
                ("🚀 Личный · 1 год",        365, 14.99, 0),
                ("👨‍👩‍👧 Семейный · 1 год",      365, 35.99, 0),
                ("💎 Максимальный · 1 год",  365, 55.00, 0),
                ("🎮 Игровой · 1 год",       365, 45.00, 0),
            ],
        )
    conn.commit()
    conn.close()


init_db()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _verify_init_data(init_data: str):
    """Verify Telegram WebApp initData signature."""
    if not BOT_TOKEN or not init_data:
        return None
    try:
        from urllib.parse import parse_qs, unquote
        parsed   = parse_qs(init_data, keep_blank_values=True)
        hash_val = parsed.pop("hash", [None])[0]
        if not hash_val:
            return None
        check_str = "\n".join(f"{k}={v[0]}" for k, v in sorted(parsed.items()))
        secret    = _hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected  = _hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(expected, hash_val):
            return None
        user_raw = parsed.get("user", [None])[0]
        return json.loads(unquote(user_raw)) if user_raw else {}
    except Exception:
        return None


def _get_uid():
    """Get user_id from request: initData header → ?user_id dev fallback."""
    init_data = (
        request.headers.get("X-Telegram-Init-Data")
        or request.args.get("initData")
    )
    if init_data:
        tg = _verify_init_data(init_data)
        if tg and tg.get("id"):
            return int(tg["id"])
    # Dev / WEBAPP_DEV_MODE
    if os.getenv("WEBAPP_DEV_MODE", "1") == "1":
        uid = request.args.get("user_id", type=int)
        if uid:
            return uid
    return None


# ── Static ────────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/webapp/")
@app.route("/webapp/index.html")
def index():
    return send_from_directory("webapp", "index.html")


@app.route("/health")
def health():
    return jsonify({"ok": True, "ts": int(time.time()), "xui": XUI_OK})


# ── Tariffs ───────────────────────────────────────────────────────────────────

@app.route("/api/tariffs")
def api_tariffs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM tariffs WHERE active=1 ORDER BY price_usd").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ── Profile ───────────────────────────────────────────────────────────────────

@app.route("/api/profile")
def api_profile():
    user_id = _get_uid()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    if not user:
        # Auto-create on first WebApp open
        conn.execute(
            "INSERT INTO users (user_id, created_at) VALUES (?,?)",
            (user_id, int(time.time()))
        )
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

    now_ms    = int(time.time() * 1000)
    sub_end   = user["sub_end"] or 0
    active    = sub_end > now_ms
    days_left = max(0, int((sub_end - now_ms) / 86_400_000)) if active else 0

    # Traffic stats from XUI
    traf_up = traf_down = 0.0
    if xui and user["client_email"]:
        try:
            st = xui.get_client_stats(user["client_email"])
            if st:
                traf_up   = round(st.get("up",   0) / 1024**3, 2)
                traf_down = round(st.get("down", 0) / 1024**3, 2)
        except Exception:
            pass

    # Referral stats
    ref_row = conn.execute(
        "SELECT COUNT(*) AS cnt, COALESCE(SUM(reward),0) AS earned "
        "FROM referrals WHERE referrer_id=?", (user_id,)
    ).fetchone()

    # Referral list (кто пришёл по ссылке — для отображения в WebApp)
    ref_list_rows = conn.execute(
        "SELECT u.username, u.first_name, u.sub_end "
        "FROM referrals r JOIN users u ON u.user_id = r.referee_id "
        "WHERE r.referrer_id=? ORDER BY r.created_at DESC",
        (user_id,)
    ).fetchall()
    referrals = [
        {
            "name":   r["first_name"] or (("@" + r["username"]) if r["username"] else "Пользователь"),
            "active": bool(r["sub_end"] and r["sub_end"] > now_ms),
        }
        for r in ref_list_rows
    ]

    # Payment history
    pays = conn.execute(
        "SELECT p.*, t.name AS tariff_name FROM payments p "
        "LEFT JOIN tariffs t ON p.tariff_id=t.id "
        "WHERE p.user_id=? ORDER BY p.created_at DESC LIMIT 20",
        (user_id,)
    ).fetchall()
    conn.close()

    return jsonify({
        "user_id":      user_id,
        "username":     user["username"] or str(user_id),
        "uuid":         user["uuid"] or "",
        "client_email": user["client_email"] or "",
        "sub_active":   active,
        "sub_end":      sub_end,
        "days_left":    days_left,
        "balance":      round(user["balance"] or 0, 4),
        "ref_count":    ref_row["cnt"],
        "ref_earned":   round(ref_row["earned"], 4),
        "referrals":    referrals,
        "traffic_up":   traf_up,
        "traffic_down": traf_down,
        "created_at":   user["created_at"] or 0,
        "payments":     [dict(p) for p in pays],
    })


# ── VLESS Config ──────────────────────────────────────────────────────────────

@app.route("/api/get_config")
def api_get_config():
    user_id = _get_uid()
    if not user_id:
        return jsonify({"error": "unauthorized"}), 401

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()

    if not user:
        return jsonify({"error": "user not found"}), 404

    now_ms  = int(time.time() * 1000)
    sub_end = user["sub_end"] or 0
    if sub_end <= now_ms:
        return jsonify({"error": "no_sub", "message": "Нет активной подписки"}), 403

    days_left = max(0, int((sub_end - now_ms) / 86_400_000))

    # Try real XUI first
    vless = None
    if xui and user["uuid"] and user["client_email"]:
        try:
            vless = xui.get_vless_link(user["client_email"], user["uuid"])
        except Exception:
            pass

    # Fallback: build from env vars
    if not vless and user["uuid"] and user["client_email"]:
        import urllib.parse as _up
        host = os.getenv("XUI_HOST", os.getenv("XUI_URL", "").split("://")[-1].split(":")[0])
        port = os.getenv("XUI_PORT", "443")
        pbk  = os.getenv("XUI_PBK",  "")
        sni  = os.getenv("XUI_SNI",  "")
        sid  = os.getenv("XUI_SID",  "")
        if host and pbk:
            params = _up.urlencode({
                "type": "tcp", "security": "reality",
                "pbk": pbk, "fp": "chrome",
                "sni": sni or host, "sid": sid,
                "spx": "%2F", "flow": "xtls-rprx-vision",
            })
            vless = f"vless://{user['uuid']}@{host}:{port}?{params}#{_up.quote(user['client_email'])}"

    if not vless:
        return jsonify({
            "error":   "config_unavailable",
            "message": "Конфиг недоступен. Запросите через бота: /config",
        }), 503

    return jsonify({
        "success":  True,
        "vless":    vless,
        "sub_end":  sub_end,
        "days_left": days_left,
    })


# ── Промокоды ────────────────────────────────────────────────────────────────

def _validate_discount_promo(code: str, user_id: int):
    """Проверяет промокод-скидку. Возвращает (ok, discount_pct, error)."""
    conn = get_db()
    p = conn.execute("SELECT * FROM promocodes WHERE code=?", (code,)).fetchone()
    if not p or not p["active"] or p["type"] != "discount":
        conn.close()
        return False, 0, "Промокод не найден или недействителен"
    if p["max_uses"] and p["used_count"] >= p["max_uses"]:
        conn.close()
        return False, 0, "У промокода закончились активации"
    used = conn.execute(
        "SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?", (code, user_id)
    ).fetchone()
    conn.close()
    if used:
        return False, 0, "Вы уже использовали этот промокод"
    return True, p["discount_pct"], None


@app.route("/api/check_promo", methods=["POST"])
def api_check_promo():
    """Проверка промокода без создания счёта — для превью скидки на экране оплаты."""
    data      = request.get_json(silent=True) or {}
    user_id   = data.get("user_id")
    tariff_id = data.get("tariff_id")
    code      = (data.get("code") or "").strip().upper()

    if not user_id or not tariff_id or not code:
        return jsonify({"error": "missing params"}), 400

    conn   = get_db()
    tariff = conn.execute("SELECT * FROM tariffs WHERE id=? AND active=1", (tariff_id,)).fetchone()
    conn.close()
    if not tariff:
        return jsonify({"error": "tariff not found"}), 404

    ok, pct, err = _validate_discount_promo(code, user_id)
    if not ok:
        return jsonify({"success": False, "error": err}), 400

    new_price = round(float(tariff["price_usd"]) * (1 - pct / 100), 2)
    return jsonify({"success": True, "discount_pct": pct, "new_price_usd": new_price})


@app.route("/api/redeem_promo", methods=["POST"])
def api_redeem_promo():
    """Активация промокода на бонусные дни прямо из WebApp (кнопка в профиле)."""
    data    = request.get_json(silent=True) or {}
    user_id = data.get("user_id")
    code    = (data.get("code") or "").strip().upper()
    if not user_id or not code:
        return jsonify({"error": "missing params"}), 400

    conn = get_db()
    p = conn.execute("SELECT * FROM promocodes WHERE code=?", (code,)).fetchone()
    if not p or not p["active"]:
        conn.close()
        return jsonify({"error": "Промокод не найден или отключён"}), 400
    if p["type"] != "days":
        conn.close()
        return jsonify({"error": "Этот промокод даёт скидку — примените его на экране оплаты"}), 400
    if p["max_uses"] and p["used_count"] >= p["max_uses"]:
        conn.close()
        return jsonify({"error": "У промокода закончились активации"}), 400
    used = conn.execute(
        "SELECT 1 FROM promo_redemptions WHERE code=? AND user_id=?", (code, user_id)
    ).fetchone()
    if used:
        conn.close()
        return jsonify({"error": "Вы уже активировали этот промокод"}), 400

    now_ms = int(time.time() * 1000)
    u = conn.execute("SELECT sub_end, uuid, client_email FROM users WHERE user_id=?", (user_id,)).fetchone()
    base    = (u["sub_end"] if u and u["sub_end"] and u["sub_end"] > now_ms else now_ms)
    new_end = base + p["days"] * 86_400_000

    conn.execute("UPDATE users SET sub_end=? WHERE user_id=?", (new_end, user_id))
    conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code=?", (code,))
    conn.execute("INSERT INTO promo_redemptions (code,user_id) VALUES (?,?)", (code, user_id))
    conn.commit()

    if xui and u:
        try:
            if u["uuid"] and u["client_email"]:
                xui.update_client_expiry(u["client_email"], u["uuid"], new_end, 0)
            else:
                new_uuid = str(_uuid.uuid4())
                email    = f"tg_{user_id}"
                if xui.add_client(new_uuid, email, new_end, 0):
                    conn.execute("UPDATE users SET uuid=?, client_email=? WHERE user_id=?",
                                 (new_uuid, email, user_id))
                    conn.commit()
        except Exception:
            pass

    conn.close()
    return jsonify({"success": True, "days": p["days"], "sub_end": new_end})


# ── Create Invoice ────────────────────────────────────────────────────────────

@app.route("/api/create_invoice", methods=["POST"])
def api_create_invoice():
    data      = request.get_json(silent=True) or {}
    user_id   = data.get("user_id")
    tariff_id = data.get("tariff_id")
    currency  = data.get("currency", "USDT").upper().strip()

    if not user_id or not tariff_id:
        return jsonify({"error": "missing params"}), 400

    # Validate currency
    SUPPORTED = {"USDT", "TON", "BTC", "ETH", "LTC", "BNB", "TRX", "USDC", "STARS", "XTR"}
    if currency not in SUPPORTED:
        return jsonify({"error": f"unsupported currency: {currency}"}), 400

    conn   = get_db()
    tariff = conn.execute("SELECT * FROM tariffs WHERE id=? AND active=1", (tariff_id,)).fetchone()
    conn.close()

    if not tariff:
        return jsonify({"error": "tariff not found"}), 404

    import requests as _req
    amount = float(tariff["price_usd"])

    # ── Промокод на скидку (необязательный) ──────────────────────────────────
    promo_code   = (data.get("promo") or "").strip().upper()
    discount_pct = 0
    if promo_code:
        ok, discount_pct, err = _validate_discount_promo(promo_code, user_id)
        if not ok:
            return jsonify({"error": err}), 400
        amount = round(amount * (1 - discount_pct / 100), 2)

    # ── Telegram Stars (XTR) — отдельная ветка, не через CryptoPay ──────────
    if currency in ("STARS", "XTR"):
        if not BOT_TOKEN:
            return jsonify({"error": "BOT_TOKEN не задан на сервере"}), 500

        stars_count = max(1, math.ceil(amount / STARS_RATE))
        inv_id = f"stars_{user_id}_{tariff_id}_{int(time.time() * 1000)}"

        payload = json.dumps({
            "invoice_id": inv_id,
            "user_id":    user_id,
            "tariff_id":  tariff_id,
            "type":       "vpn_subscription",
        })

        try:
            r = _req.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink",
                json={
                    "title":       "VPN подписка",
                    "description": tariff["name"][:255],
                    "payload":     payload,
                    "currency":    "XTR",
                    "prices":      [{"label": tariff["name"], "amount": stars_count}],
                },
                timeout=15,
            )
            resp = r.json()
            if not resp.get("ok"):
                return jsonify({"error": f"Telegram: {resp.get('description', 'error')}"}), 502
            link = resp["result"]
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO payments (invoice_id,user_id,tariff_id,amount,currency,status,promo_code) "
            "VALUES (?,?,?,?,?,?,?)",
            (inv_id, user_id, tariff_id, amount, "XTR", "pending", promo_code or None),
        )
        conn.commit()
        conn.close()

        return jsonify({
            "success":      True,
            "invoice_id":   inv_id,
            "pay_url":      link,
            "amount":       stars_count,
            "currency":     "XTR",
            "discount_pct": discount_pct,
        })

    url     = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}
    payload = {
        "currency_type": "crypto",
        "asset":         currency,
        "amount":        str(round(amount, 2)),
        "description":   f"VpnDota · {tariff['name']}",
        "payload":       json.dumps({"user_id": user_id, "tariff_id": tariff_id}),
        "paid_btn_name": "openBot",
        "paid_btn_url":  f"https://t.me/{os.getenv('BOT_USERNAME','VpnDotaBot')}",
        "allow_comments":  False,
        "allow_anonymous": True,
    }

    try:
        r    = _req.post(url, headers=headers, json=payload, timeout=15)
        resp = r.json()
        if not resp.get("ok"):
            err = resp.get("error", {})
            msg = err.get("name", str(err)) if isinstance(err, dict) else str(err)
            return jsonify({"error": f"CryptoPay: {msg}"}), 502
        inv = resp["result"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    conn = get_db()
    conn.execute(
        "INSERT OR IGNORE INTO payments (invoice_id,user_id,tariff_id,amount,currency,promo_code) VALUES (?,?,?,?,?,?)",
        (str(inv["invoice_id"]), user_id, tariff_id, amount, currency, promo_code or None),
    )
    conn.commit()
    conn.close()

    return jsonify({
        "success":      True,
        "invoice_id":   inv["invoice_id"],
        "pay_url":      inv["pay_url"],
        "amount":       amount,
        "currency":     currency,
        "discount_pct": discount_pct,
    })


# ── Check Payment ─────────────────────────────────────────────────────────────

@app.route("/api/check_payment", methods=["POST"])
def api_check_payment():
    data       = request.get_json(silent=True) or {}
    invoice_id = str(data.get("invoice_id", ""))
    user_id    = data.get("user_id")

    if not invoice_id:
        return jsonify({"error": "no invoice_id"}), 400

    # ── Telegram Stars invoices: платёж подтверждается ботом (successful_payment)
    # и активируется напрямую в БД — сюда CryptoPay не запрашиваем.
    if invoice_id.startswith("stars_"):
        conn = get_db()
        pay = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
        conn.close()
        if not pay:
            return jsonify({"success": True, "paid": False, "status": "unknown"})
        if pay["status"] == "paid":
            conn = get_db()
            u = conn.execute("SELECT sub_end FROM users WHERE user_id=?", (pay["user_id"],)).fetchone()
            conn.close()
            return jsonify({"success": True, "paid": True, "sub_end": u["sub_end"] if u else 0})
        return jsonify({"success": True, "paid": False, "status": pay["status"]})

    import requests as _req
    url     = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_TOKEN}

    try:
        r    = _req.get(url, headers=headers, params={"invoice_ids": invoice_id}, timeout=10)
        resp = r.json()
        if not resp.get("ok") or not resp["result"]["items"]:
            return jsonify({"success": True, "paid": False, "status": "unknown"})
        item   = resp["result"]["items"][0]
        status = item["status"]
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if status != "paid":
        return jsonify({"success": True, "paid": False, "status": status})

    # --- Activate subscription ---
    conn = get_db()
    pay  = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()

    if not pay:
        conn.close()
        return jsonify({"success": True, "paid": True})

    if pay["status"] == "paid":
        conn.close()
        return jsonify({"success": True, "paid": True, "already": True})

    tariff = conn.execute("SELECT * FROM tariffs WHERE id=?", (pay["tariff_id"],)).fetchone()
    if not tariff:
        conn.close()
        return jsonify({"error": "tariff not found"}), 404

    uid     = pay["user_id"]
    now_ms  = int(time.time() * 1000)
    u       = conn.execute("SELECT sub_end, uuid, client_email FROM users WHERE user_id=?", (uid,)).fetchone()
    base    = (u["sub_end"] if u and u["sub_end"] > now_ms else now_ms)
    new_end = base + tariff["days"] * 86_400_000

    # XUI provisioning
    if xui and u:
        try:
            if u["uuid"] and u["client_email"]:
                xui.update_client_expiry(u["client_email"], u["uuid"], new_end, tariff["gb_limit"] or 0)
            else:
                new_uuid = str(_uuid.uuid4())
                email    = f"tg_{uid}"
                if xui.add_client(new_uuid, email, new_end, tariff["gb_limit"] or 0):
                    conn.execute(
                        "UPDATE users SET uuid=?, client_email=? WHERE user_id=?",
                        (new_uuid, email, uid),
                    )
        except Exception as e:
            app.logger.error("XUI error on payment activation: %s", e)

    conn.execute("UPDATE users SET sub_end=? WHERE user_id=?",         (new_end, uid))
    conn.execute("UPDATE payments SET status='paid' WHERE invoice_id=?", (invoice_id,))

    # Referral reward 15%
    ref = conn.execute("SELECT referrer_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if ref and ref["referrer_id"]:
        reward = round(float(pay["amount"]) * 0.15, 4)
        conn.execute("UPDATE referrals SET reward=reward+? WHERE referrer_id=? AND referee_id=?",
                     (reward, ref["referrer_id"], uid))
        conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?",
                     (reward, ref["referrer_id"]))

    conn.commit()
    conn.close()

    return jsonify({"success": True, "paid": True, "sub_end": new_end})


# ── Admin stats ───────────────────────────────────────────────────────────────

@app.route("/api/admin/stats")
def api_admin_stats():
    token = os.getenv("ADMIN_API_TOKEN", "")
    if token and request.args.get("token") != token:
        return jsonify({"error": "forbidden"}), 403
    conn = get_db()
    now_ms = int(time.time() * 1000)
    stats  = {
        "total_users":     conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        "active_subs":     conn.execute("SELECT COUNT(*) FROM users WHERE sub_end>?", (now_ms,)).fetchone()[0],
        "total_income":    conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='paid'").fetchone()[0],
        "total_referrals": conn.execute("SELECT COUNT(*) FROM referrals").fetchone()[0],
    }
    conn.close()
    return jsonify(stats)


# ── CryptoPay Webhook ─────────────────────────────────────────────────────────

@app.route("/api/webhook/cryptopay", methods=["POST"])
def api_cryptopay_webhook():
    """CryptoPay calls this on invoice_paid event."""
    raw_body = request.get_data(as_text=True)

    # Verify signature
    if CRYPTO_TOKEN:
        secret   = hashlib.sha256(CRYPTO_TOKEN.encode()).digest()
        sig      = request.headers.get("crypto-pay-api-signature", "")
        expected = _hmac.new(secret, raw_body.encode(), hashlib.sha256).hexdigest()
        if sig != expected:
            return jsonify({"error": "bad signature"}), 403

    try:
        event = json.loads(raw_body)
    except Exception:
        return jsonify({"error": "bad json"}), 400

    if event.get("update_type") != "invoice_paid":
        return jsonify({"ok": True})

    invoice = event.get("payload", {})
    if invoice.get("status") != "paid":
        return jsonify({"ok": True})

    invoice_id = str(invoice.get("invoice_id", ""))

    # Re-use check_payment logic
    try:
        meta    = json.loads(invoice.get("payload", "{}"))
        user_id = int(meta.get("user_id", 0))
    except Exception:
        return jsonify({"error": "bad payload"}), 400

    # Delegate to the same activation logic
    with app.test_request_context():
        import flask
        fake_req = flask.Request.from_values(
            method="POST",
            content_type="application/json",
            data=json.dumps({"invoice_id": invoice_id, "user_id": user_id}),
        )
        # Directly call DB activation to avoid re-checking CryptoPay
        _activate_invoice(invoice_id, user_id)

    return jsonify({"ok": True})


def _activate_invoice(invoice_id: str, user_id: int):
    """Pure DB activation — called from webhook and check_payment."""
    conn   = get_db()
    pay    = conn.execute("SELECT * FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
    if not pay or pay["status"] == "paid":
        conn.close()
        return

    tariff = conn.execute("SELECT * FROM tariffs WHERE id=?", (pay["tariff_id"],)).fetchone()
    if not tariff:
        conn.close()
        return

    now_ms  = int(time.time() * 1000)
    uid     = pay["user_id"]
    u       = conn.execute("SELECT sub_end, uuid, client_email FROM users WHERE user_id=?", (uid,)).fetchone()
    base    = (u["sub_end"] if u and u["sub_end"] > now_ms else now_ms)
    new_end = base + tariff["days"] * 86_400_000

    if xui and u:
        try:
            if u["uuid"] and u["client_email"]:
                xui.update_client_expiry(u["client_email"], u["uuid"], new_end, tariff["gb_limit"] or 0)
            else:
                new_uuid = str(_uuid.uuid4())
                email    = f"tg_{uid}"
                if xui.add_client(new_uuid, email, new_end, tariff["gb_limit"] or 0):
                    conn.execute("UPDATE users SET uuid=?, client_email=? WHERE user_id=?",
                                 (new_uuid, email, uid))
        except Exception:
            pass

    conn.execute("UPDATE users SET sub_end=? WHERE user_id=?",          (new_end, uid))
    conn.execute("UPDATE payments SET status='paid' WHERE invoice_id=?", (invoice_id,))

    if pay["promo_code"]:
        conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code=?", (pay["promo_code"],))
        conn.execute("INSERT OR IGNORE INTO promo_redemptions (code,user_id) VALUES (?,?)",
                     (pay["promo_code"], uid))

    ref = conn.execute("SELECT referrer_id FROM users WHERE user_id=?", (uid,)).fetchone()
    if ref and ref["referrer_id"]:
        reward = round(float(pay["amount"]) * 0.15, 4)
        conn.execute("UPDATE referrals SET reward=reward+? WHERE referrer_id=? AND referee_id=?",
                     (reward, ref["referrer_id"], uid))
        conn.execute("UPDATE users SET balance=balance+? WHERE user_id=?",
                     (reward, ref["referrer_id"]))

    conn.commit()
    conn.close()


# ── Run ───────────────────────────────────────────────────────────────────────

def run_server(host="0.0.0.0", port=8080):
    app.logger.info("WebApp server starting on %s:%s", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    run_server()