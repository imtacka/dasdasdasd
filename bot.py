import html
import hashlib
import hmac
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


API_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
API_URL = f"https://api.telegram.org/bot{API_TOKEN}/" if API_TOKEN else None
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"]) if os.environ.get("ADMIN_CHAT_ID") else None
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
MONO_TOKEN = os.environ.get("MONO_TOKEN")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
MINI_APP_URL = os.environ.get("MINI_APP_URL", "").rstrip("/")
MINI_APP_DEV_MODE = os.environ.get("MINI_APP_DEV_MODE", "0") == "1"
MINI_APP_ONLY = os.environ.get("MINI_APP_ONLY", "1") != "0"
MONO_WEBHOOK_SECRET = os.environ.get("MONO_WEBHOOK_SECRET", "mono-test-webhook")
MONO_PAYMENT_TYPE = os.environ.get("MONO_PAYMENT_TYPE", "hold")
WEBHOOK_PORT = int(os.environ.get("PORT", "8000"))
CRYPTO_TON_ADDRESS = os.environ.get("CRYPTO_TON_ADDRESS", "")
CRYPTO_USDT_ADDRESS = os.environ.get("CRYPTO_USDT_ADDRESS", "")
CRYPTO_USDT_NETWORK = os.environ.get("CRYPTO_USDT_NETWORK", "TRC20")
DATA_DIR = Path(__file__).resolve().parent / "data"
WEBAPP_DIR = Path(__file__).resolve().parent / "webapp"
DEALS_FILE = DATA_DIR / "deals.json"
BALANCES_FILE = DATA_DIR / "balances.json"
WITHDRAWALS_FILE = DATA_DIR / "withdrawals.json"
USERS_FILE = DATA_DIR / "users.json"
SERVICE_FEE_PERCENT = 2
DEAL_ACCEPT_TIMEOUT_SECONDS = int(os.environ.get("DEAL_ACCEPT_TIMEOUT_SECONDS", "600"))
MAX_NEW_DEALS_PER_HOUR = int(os.environ.get("MAX_NEW_DEALS_PER_HOUR", os.environ.get("MAX_NEW_DEALS_PER_10_MIN", "3")))
MIN_DEAL_AMOUNT_KOP = int(os.environ.get("MIN_DEAL_AMOUNT_KOP", "1000"))
MAX_DEAL_AMOUNT_KOP = int(os.environ.get("MAX_DEAL_AMOUNT_KOP", "1000000"))
MAX_PROOFS_PER_DEAL = int(os.environ.get("MAX_PROOFS_PER_DEAL", "10"))


sessions = {}
interface_messages = {}
BOT_USERNAME = ""


def load_deals():
    if not DEALS_FILE.exists():
        return {}
    with DEALS_FILE.open("r", encoding="utf-8") as file:
        deals = json.load(file)
    for deal in deals.values():
        migrate_deal(deal)
    return deals


def save_deals(deals):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DEALS_FILE.open("w", encoding="utf-8") as file:
        json.dump(deals, file, ensure_ascii=False, indent=2)


def load_json_file(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_json_file(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def load_balances():
    return load_json_file(BALANCES_FILE, {})


def save_balances(balances):
    save_json_file(BALANCES_FILE, balances)


def load_withdrawals():
    return load_json_file(WITHDRAWALS_FILE, {})


def save_withdrawals(withdrawals):
    save_json_file(WITHDRAWALS_FILE, withdrawals)


def load_users():
    return load_json_file(USERS_FILE, {})


def save_users(users):
    save_json_file(USERS_FILE, users)


def migrate_deal(deal):
    buyer = deal.get("buyer", "")
    seller = deal.get("seller", "")
    creator_chat_id = deal.get("creator_chat_id")
    deal.setdefault("buyer_chat_id", creator_chat_id)
    deal.setdefault("seller_chat_id", None)
    deal.setdefault("buyer_key", normalize_username(buyer) if str(buyer).startswith("@") else str(deal.get("buyer_chat_id") or buyer))
    deal.setdefault("seller_key", normalize_username(seller))
    deal.setdefault("confirmations", {"buyer_terms": False, "seller_terms": False})
    deal.setdefault("proofs", [])
    deal.setdefault("deal_type", "digital")
    deal.setdefault("handoff", None)
    deal.setdefault("seller_shipped", deal.get("status") in ("delivery", "inspection", "released"))
    deal.setdefault("buyer_received", deal.get("status") == "released")
    deal.setdefault("seller_payout_details", None)
    deal.setdefault("payout", deal.get("payout") or {})
    deal.setdefault("chat", [])
    deal.setdefault("admin_reviews", {})
    deal.setdefault("events", [])
    deal.setdefault("admin_notes", [])
    deal.setdefault("ratings", {})
    if deal.get("status") == "draft":
        deal["status"] = "awaiting_acceptance"
    return deal


def deal_type_label(deal):
    return {
        "digital": "цифровой товар",
        "service": "услуга",
    }.get(deal.get("deal_type"), "цифровой товар")


def seller_handoff_label(deal):
    if deal.get("deal_type") == "service":
        return "результат услуги"
    return "данные цифрового товара"


def seller_handoff_button(deal):
    if deal.get("deal_type") == "service":
        return "🧩 Передать результат"
    return "🔐 Передать цифровой товар"


def buyer_receive_button(deal):
    if deal.get("deal_type") == "service":
        return "✅ Принять результат"
    return "✅ Принять товар"


def seller_payout_details_for_deal(deal):
    users = load_users()
    details = users.get(deal.get("seller_key"), {}).get("payout_details")
    if details:
        return details
    seller_chat_id = deal.get("seller_chat_id")
    if seller_chat_id:
        for record in users.values():
            if record.get("chat_id") == seller_chat_id and record.get("payout_details"):
                return record.get("payout_details")
    return details or deal.get("seller_payout_details")


def api(method, payload=None):
    if not API_URL:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN environment variable first.")
    payload = payload or {}
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(API_URL + method, data=data)
    with urllib.request.urlopen(request, timeout=35) as response:
        return json.loads(response.read().decode("utf-8"))


def safe_api(method, payload=None):
    try:
        return api(method, payload)
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": str(error)}
    except Exception as error:
        return {"ok": False, "description": str(error)}


def mono_api(method, path, payload=None, query=None):
    if not MONO_TOKEN:
        raise RuntimeError("Set MONO_TOKEN first.")
    url = "https://api.monobank.ua" + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None
    headers = {"X-Token": MONO_TOKEN}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=35) as response:
        raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def safe_mono_api(method, path, payload=None, query=None):
    try:
        return {"ok": True, "result": mono_api(method, path, payload, query)}
    except urllib.error.HTTPError as error:
        try:
            body = error.read().decode("utf-8")
            details = json.loads(body) if body else {}
        except Exception:
            details = {"message": str(error)}
        return {"ok": False, "description": details}
    except Exception as error:
        return {"ok": False, "description": str(error)}


def esc(value):
    return html.escape(str(value), quote=False)


def normalize_username(value):
    return (value or "").strip().lower().lstrip("@")


def user_name(user):
    username = user.get("username")
    return "@" + username if username else str(user.get("id"))


def user_key(user):
    username = user.get("username")
    return normalize_username(username) if username else str(user.get("id"))


def user_record(user):
    users = load_users()
    key = user_key(user)
    record = users.setdefault(key, {
        "name": user_name(user),
        "chat_id": user.get("id"),
        "blocked": False,
        "blocked_reason": "",
        "created_deals": [],
    })
    record["name"] = user_name(user)
    record["chat_id"] = user.get("id")
    users[key] = record
    save_users(users)
    return record


def is_user_blocked(user):
    return bool(user_record(user).get("blocked"))


def can_create_deal(user):
    record = user_record(user)
    now = int(time.time())
    recent = [ts for ts in record.get("created_deals", []) if now - ts <= 3600]
    record["created_deals"] = recent
    users = load_users()
    users[user_key(user)] = record
    save_users(users)
    return len(recent) < MAX_NEW_DEALS_PER_HOUR


def mark_deal_created(user):
    users = load_users()
    key = user_key(user)
    record = users.setdefault(key, {"name": user_name(user), "blocked": False, "created_deals": []})
    record.setdefault("created_deals", []).append(int(time.time()))
    record["name"] = user_name(user)
    record["chat_id"] = user.get("id")
    users[key] = record
    save_users(users)


def add_event(deal, actor, title, details=""):
    deal.setdefault("events", []).append({
        "at": int(time.time()),
        "actor": actor,
        "title": title,
        "details": details,
    })


def event_time(ts):
    return time.strftime("%H:%M", time.localtime(int(ts)))


def risk_report(deal):
    score = 10
    reasons = []
    amount = deal_amount_kop(deal)
    if amount >= 500000:
        score += 25
        reasons.append("крупная сумма")
    if not deal.get("proofs"):
        score += 20
        reasons.append("нет доказательств")
    if deal.get("status") == "dispute":
        score += 35
        reasons.append("открыт спор")
    if not deal.get("seller_chat_id"):
        score += 15
        reasons.append("продавец еще не подключился")
    users = load_users()
    if users.get(deal.get("buyer_key"), {}).get("blocked") or users.get(deal.get("seller_key"), {}).get("blocked"):
        score += 50
        reasons.append("одна из сторон в блокировке")
    score = min(100, score)
    level = "низкий"
    if score >= 70:
        level = "высокий"
    elif score >= 40:
        level = "средний"
    return score, level, reasons or ["критичных флагов нет"]


def keyboard(rows):
    return json.dumps({"inline_keyboard": rows}, ensure_ascii=False)


def send_message(chat_id, text, reply_markup=None):
    if MINI_APP_ONLY:
        reply_markup = None
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    if MINI_APP_ONLY:
        return safe_api("sendMessage", payload)
    return api("sendMessage", payload)


def send_plain_notice(chat_id, text):
    if not API_TOKEN or not chat_id:
        return {"ok": False, "description": "Telegram token or chat id is not configured."}
    return safe_api("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    })


def send_photo(chat_id, file_id, caption=None, reply_markup=None):
    payload = {"chat_id": chat_id, "photo": file_id, "parse_mode": "HTML"}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendPhoto", payload)


def send_document(chat_id, file_id, caption=None, reply_markup=None):
    payload = {"chat_id": chat_id, "document": file_id, "parse_mode": "HTML"}
    if caption:
        payload["caption"] = caption
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return api("sendDocument", payload)


def delete_message(chat_id, message_id):
    if message_id:
        safe_api("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


def send_clean_message(chat_id, text, reply_markup=None):
    delete_message(chat_id, interface_messages.get(chat_id))
    result = send_message(chat_id, text, reply_markup)
    message_id = result.get("result", {}).get("message_id")
    if message_id:
        interface_messages[chat_id] = message_id
    return result


def send_notice(chat_id, text, reply_markup=None):
    return send_message(chat_id, text, reply_markup)


def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = safe_api("editMessageText", payload)
    if result.get("ok"):
        interface_messages[chat_id] = message_id
        return result
    return send_clean_message(chat_id, text, reply_markup)


def show_screen(chat_id, text, reply_markup=None, message_id=None):
    if message_id:
        return edit_message(chat_id, message_id, text, reply_markup)
    return send_clean_message(chat_id, text, reply_markup)


def answer_callback(callback_id, text=""):
    return safe_api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})


def mini_app_url():
    if MINI_APP_URL:
        return MINI_APP_URL
    if PUBLIC_BASE_URL:
        return f"{PUBLIC_BASE_URL}/app/"
    return ""


def mini_app_button(text="Открыть Garant App"):
    url = mini_app_url()
    if not url:
        return None
    return {"text": text, "web_app": {"url": url}}


def main_menu(chat_id=None):
    rows = [
        [
            {"text": "🧾 Новая сделка", "callback_data": "new_deal"},
            {"text": "📁 Мои сделки", "callback_data": "my_deals"},
        ],
        [
            {"text": "👤 Мой профиль", "callback_data": "profile"},
            {"text": "🤖 Консультант", "callback_data": "ai_agent"},
        ],
        [
            {"text": "⚙️ Как работает", "callback_data": "how_it_works"},
        ],
    ]
    app_button = mini_app_button()
    if app_button:
        rows.insert(0, [app_button])
    if ADMIN_CHAT_ID and chat_id == ADMIN_CHAT_ID:
        rows.append([
            {"text": "🕵️ Активные сделки", "callback_data": "admin_active_deals"},
            {"text": "💸 Выплаты", "callback_data": "admin_payouts"},
        ])
        rows.append([{"text": "🧹 Очистка сделок", "callback_data": "cleanup_menu"}])
    return keyboard(rows)


def back_menu():
    return keyboard([[{"text": "⬅️ В меню", "callback_data": "menu"}]])


def cancel_menu():
    return keyboard([[{"text": "✖️ Отмена", "callback_data": "cancel"}]])


def deal_type_menu():
    return keyboard([
        [
            {"text": "🔐 Цифровой товар", "callback_data": "deal_type:digital"},
            {"text": "🧩 Услуга", "callback_data": "deal_type:service"},
        ],
        [{"text": "✖️ Отмена", "callback_data": "cancel"}],
    ])


def crypto_payment_keyboard(deal_id):
    rows = []
    if CRYPTO_TON_ADDRESS:
        rows.append([{"text": "TON", "callback_data": f"crypto_create_ton:{deal_id}"}])
    if CRYPTO_USDT_ADDRESS:
        rows.append([{"text": f"USDT {CRYPTO_USDT_NETWORK}", "callback_data": f"crypto_create_usdt:{deal_id}"}])
    rows.append([{"text": "⬅️ К сделке", "callback_data": f"deal:{deal_id}"}])
    return keyboard(rows)


def deal_nav_keyboard(deal_id, role):
    rows = json.loads(deal_keyboard(deal_id, role))["inline_keyboard"]
    rows.append([{"text": "⬅️ К списку сделок", "callback_data": "my_deals"}])
    return keyboard(rows)


def profile_keyboard(chat_id, user):
    rows = [[{"text": "⬅️ В меню", "callback_data": "menu"}]]
    record = user_record(user)
    rows.insert(0, [{"text": "💸 Вывести деньги", "callback_data": "profile_withdraw"}])
    if not record.get("payout_details"):
        rows.insert(0, [{"text": "🏦 Указать реквизиты", "callback_data": "profile_payout_details"}])
    else:
        rows.insert(0, [{"text": "🏦 Изменить реквизиты", "callback_data": "profile_payout_details"}])
    return keyboard(rows)


def status_label(status):
    labels = {
        "awaiting_acceptance": "🤝 ждет согласия сторон",
        "waiting_payment": "💳 ждет оплату",
        "paid": "✅ оплачено",
        "paid_hold": "🔒 оплата в холде",
        "payment_failed": "❌ оплата не прошла",
        "shipping_review": "🕵️ передача на проверке админа",
        "delivery": "🔎 покупатель проверяет результат",
        "receive_review": "🕵️ принятие на проверке админа",
        "inspection": "🔍 покупатель проверяет",
        "dispute": "⚖️ спор",
        "released": "💸 сделка завершена",
        "refunded": "↩️ деньги возвращены",
    }
    return labels.get(status, status)


def deal_role(deal, user, chat_id):
    key = user_key(user)
    if chat_id == deal.get("buyer_chat_id") or key == deal.get("buyer_key"):
        return "buyer"
    if chat_id == deal.get("seller_chat_id") or key == deal.get("seller_key"):
        return "seller"
    return "guest"


def role_title(role):
    return {"buyer": "покупатель", "seller": "продавец"}.get(role, "участник")


def terms_confirmed(deal):
    confirmations = deal.get("confirmations") or {}
    return bool(confirmations.get("buyer_terms") and confirmations.get("seller_terms"))


def deal_accept_deadline(deal):
    return int(deal.get("created_at", int(time.time()))) + DEAL_ACCEPT_TIMEOUT_SECONDS


def deal_accept_remaining(deal):
    return max(0, deal_accept_deadline(deal) - int(time.time()))


def should_auto_delete_deal(deal):
    payment = deal.get("payment") or {}
    if payment.get("invoiceId") or payment.get("status") in ("success", "hold"):
        return False
    if payment.get("provider") == "crypto" and payment.get("status") in ("pending_admin", "proof_sent"):
        return False
    if deal.get("status") not in ("awaiting_acceptance", "waiting_payment"):
        return False
    if terms_confirmed(deal):
        return False
    return deal_accept_remaining(deal) <= 0


def delete_deal_everywhere(deal_id):
    deals = load_deals()
    deal = deals.pop(deal_id, None)
    if not deal:
        return None
    save_deals(deals)
    for chat_id in (deal.get("buyer_chat_id"), deal.get("seller_chat_id"), ADMIN_CHAT_ID):
        if chat_id:
            delete_message(chat_id, interface_messages.get(chat_id))
            interface_messages.pop(chat_id, None)
    return deal


def run_auto_delete_expired_deals():
    deals = load_deals()
    expired_ids = [deal_id for deal_id, deal in deals.items() if should_auto_delete_deal(deal)]
    if not expired_ids:
        return 0
    for deal_id in expired_ids:
        deals.pop(deal_id, None)
    save_deals(deals)
    return len(expired_ids)


def run_deal_reminders():
    now = int(time.time())
    deals = load_deals()
    changed = False
    for deal in deals.values():
        reminders = deal.setdefault("reminders", {})
        if deal.get("status") in ("awaiting_acceptance", "waiting_payment") and not terms_confirmed(deal):
            if now - reminders.get("accept", 0) >= 300:
                for chat_id in (deal.get("buyer_chat_id"), deal.get("seller_chat_id")):
                    if chat_id:
                        send_notice(chat_id, f"⏳ Сделка #{deal['id']} ждет подтверждения условий. Без согласия она будет удалена по таймеру.")
                reminders["accept"] = now
                changed = True
        if deal.get("status") in ("shipping_review", "receive_review") and ADMIN_CHAT_ID:
            if now - reminders.get("admin_review", 0) >= 600:
                send_notice(ADMIN_CHAT_ID, f"🕵️ Сделка #{deal['id']} ждет админской проверки.", admin_deal_review_keyboard(deal["id"]))
                reminders["admin_review"] = now
                changed = True
    if changed:
        save_deals(deals)


def invite_link(deal_id):
    if BOT_USERNAME:
        return f"https://t.me/{BOT_USERNAME}?start=join_{deal_id}"
    return f"/join {deal_id}"


def deal_keyboard(deal_id, role):
    deals = load_deals()
    deal = deals.get(deal_id, {})
    payment = deal.get("payment") or {}
    status = deal.get("status")
    payment_status = payment.get("status")
    confirmed = terms_confirmed(deal)
    can_create_payment = (
        role == "buyer"
        and confirmed
        and status in ("waiting_payment", "payment_failed")
        and (not payment.get("provider") or payment_status in ("failure", "expired", "reversed", "rejected"))
    )
    rows = [[{"text": "📌 Карточка сделки", "callback_data": f"deal:{deal_id}"}]]
    rows.append([{"text": "🛡️ Паспорт сделки", "callback_data": f"passport:{deal_id}"}])
    rows.append([{"text": "💬 Чат сделки", "callback_data": f"chat:{deal_id}"}])

    if role in ("buyer", "seller") and not confirmed:
        label = "✅ Согласен с условиями"
        rows.append([{"text": label, "callback_data": f"accept:{deal_id}"}])

    if can_create_payment:
        rows.append([
            {"text": "💳 Оплата Mono", "callback_data": f"mono_create:{deal_id}"},
            {"text": "🪙 Оплата Crypto", "callback_data": f"crypto_menu:{deal_id}"},
        ])

    payment_done = payment_status in ("success", "hold")
    if role in ("buyer", "seller") and payment.get("invoiceId") and not payment_done and status not in ("released", "refunded", "dispute"):
        rows.append([{"text": "🔄 Проверить оплату", "callback_data": f"mono_status:{deal_id}"}])

    if role == "buyer" and not payment_done:
        if payment.get("pageUrl"):
            rows.append([{"text": "🌐 Открыть оплату", "url": payment["pageUrl"]}])
        if payment.get("provider") == "crypto":
            rows.append([{"text": "🧾 Инструкция crypto", "callback_data": f"crypto_card:{deal_id}"}])
            rows.append([{"text": "🔎 Отправить TX/скрин", "callback_data": f"crypto_proof:{deal_id}"}])

    if role == "seller":
        if payment_status in ("success", "hold") and not deal.get("seller_shipped") and status not in ("shipping_review", "released", "refunded", "dispute"):
            rows.append([{"text": seller_handoff_button(deal), "callback_data": f"ship:{deal_id}"}])

    receive_allowed = status != "receive_review" or (deal.get("admin_reviews") or {}).get("receive_approved")
    if role == "buyer" and deal.get("seller_shipped") and payment_status in ("success", "hold") and not deal.get("buyer_received") and receive_allowed and status not in ("released", "refunded", "dispute"):
        label = "🏁 Завершить сделку" if (deal.get("admin_reviews") or {}).get("receive_approved") else buyer_receive_button(deal)
        rows.append([{"text": label, "callback_data": f"receive:{deal_id}"}])

    if role in ("buyer", "seller") and status not in ("released", "refunded"):
        rows.append([
            {"text": "📎 Доказательство", "callback_data": f"proof:{deal_id}"},
            {"text": "⚠️ Открыть спор", "callback_data": f"dispute:{deal_id}"},
        ])
    if role in ("buyer", "seller") and status == "released":
        rows.append([
            {"text": "⭐ Оценить 5", "callback_data": f"rate_5:{deal_id}"},
            {"text": "⭐ Оценить 4", "callback_data": f"rate_4:{deal_id}"},
        ])
    return keyboard(rows)


def admin_payout_keyboard(deal_id):
    return keyboard([
        [{"text": "🧾 Прикрепить квитанцию", "callback_data": f"admin_receipt:{deal_id}"}],
        [
            {"text": "✅ Выплачено вручную", "callback_data": f"admin_paid:{deal_id}"},
            {"text": "⚠️ Открыть спор", "callback_data": f"admin_dispute:{deal_id}"},
        ],
        [{"text": "📌 Карточка сделки", "callback_data": f"admin_deal:{deal_id}"}],
        [{"text": "⬅️ К выплатам", "callback_data": "admin_payouts"}],
    ])


def admin_withdrawal_keyboard(withdrawal_id):
    return keyboard([
        [{"text": "🧾 Прикрепить квитанцию", "callback_data": f"withdrawal_receipt:{withdrawal_id}"}],
        [
            {"text": "✅ Выплачено вручную", "callback_data": f"withdrawal_paid:{withdrawal_id}"},
            {"text": "⚠️ Отклонить", "callback_data": f"withdrawal_reject:{withdrawal_id}"},
        ],
        [{"text": "⬅️ К выплатам", "callback_data": "admin_payouts"}],
    ])


def admin_deal_review_keyboard(deal_id):
    deal = load_deals().get(deal_id, {})
    payment = deal.get("payment") or {}
    rows = [
        [{"text": "🛡️ Паспорт сделки", "callback_data": f"admin_passport:{deal_id}"}],
        [
            {"text": "✅ Разрешить передачу", "callback_data": f"admin_approve_ship:{deal_id}"},
            {"text": "✅ Разрешить принятие", "callback_data": f"admin_approve_receive:{deal_id}"},
        ],
        [
            {"text": "📝 Заметка", "callback_data": f"admin_note:{deal_id}"},
            {"text": "🚫 Блок покупателя", "callback_data": f"admin_block_buyer:{deal_id}"},
        ],
        [
            {"text": "🚫 Блок продавца", "callback_data": f"admin_block_seller:{deal_id}"},
        ],
        [
            {"text": "⚠️ Открыть спор", "callback_data": f"admin_review_dispute:{deal_id}"},
            {"text": "🗑️ Удалить", "callback_data": f"delete_deal_preview:{deal_id}"},
        ],
        [
            {"text": "↩️ Решить: возврат", "callback_data": f"admin_resolve_refund:{deal_id}"},
            {"text": "💰 Решить: продавцу", "callback_data": f"admin_resolve_seller:{deal_id}"},
        ],
        [
            {"text": "⬅️ Активные сделки", "callback_data": "admin_active_deals"},
        ],
    ]
    if payment.get("provider") == "crypto" and payment.get("status") in ("pending_admin", "proof_sent"):
        rows.insert(1, [
            {"text": "🪙 Crypto оплачен", "callback_data": f"admin_crypto_approve:{deal_id}"},
            {"text": "❌ Crypto отклонить", "callback_data": f"admin_crypto_reject:{deal_id}"},
        ])
    return keyboard(rows)


def dispute_reason_keyboard(deal_id):
    return keyboard([
        [{"text": "📦 Товар/результат не получен", "callback_data": f"dispute_reason:not_received:{deal_id}"}],
        [{"text": "❌ Не соответствует условиям", "callback_data": f"dispute_reason:not_as_described:{deal_id}"}],
        [{"text": "🚨 Подозрение на мошенничество", "callback_data": f"dispute_reason:fraud:{deal_id}"}],
        [{"text": "✍️ Другая причина", "callback_data": f"dispute_reason:other:{deal_id}"}],
        [{"text": "⬅️ К сделке", "callback_data": f"deal:{deal_id}"}],
    ])


def dispute_reason_title(reason):
    return {
        "not_received": "товар/результат не получен",
        "not_as_described": "не соответствует условиям",
        "fraud": "подозрение на мошенничество",
        "other": "другая причина",
    }.get(reason, reason)


def setup_bot_commands():
    commands = [
        {"command": "start", "description": "Главное меню"},
    ]
    api("setMyCommands", {"commands": json.dumps(commands, ensure_ascii=False)})


def get_bot_identity():
    result = api("getMe")
    return result.get("result", {})


def render_menu():
    return (
        "🛡️ <b>Garant Bot</b>\n\n"
        "Я веду сделку по этапам: согласие сторон, оплата, передача, проверка, завершение или спор.\n\n"
        "Выбери действие ниже 👇"
    )


def trust_level(score):
    if score >= 90:
        return "🟢 высокий"
    if score >= 70:
        return "🟡 нормальный"
    if score >= 45:
        return "🟠 требует внимания"
    return "🔴 низкий"


def render_profile(chat_id, user):
    key = user_key(user)
    record = user_record(user)
    deals = list(load_deals().values())
    related = [
        deal for deal in deals
        if chat_id in (deal.get("buyer_chat_id"), deal.get("seller_chat_id"))
        or key in (deal.get("buyer_key"), deal.get("seller_key"))
    ]
    as_buyer = [deal for deal in related if chat_id == deal.get("buyer_chat_id") or key == deal.get("buyer_key")]
    as_seller = [deal for deal in related if chat_id == deal.get("seller_chat_id") or key == deal.get("seller_key")]
    completed = [deal for deal in related if deal.get("status") == "released"]
    active = [deal for deal in related if deal.get("status") not in ("released", "refunded")]
    disputes = [deal for deal in related if deal.get("status") == "dispute"]

    balances = load_balances()
    balance = get_balance_record(balances, key)
    dispute_penalty = len(disputes) * 12
    completion_bonus = min(25, len(completed) * 3)
    proof_bonus = min(15, sum(1 for deal in related if deal.get("proofs")) * 2)
    score = max(0, min(100, 65 + completion_bonus + proof_bonus - dispute_penalty))

    tips = []
    if not record.get("payout_details"):
        tips.append("• Продавцу стоит заранее указать реквизиты для вывода.")
    if disputes:
        tips.append("• Есть спорные сделки: держи доказательства и переписку в карточке.")
    if not related:
        tips.append("• Начни с небольшой сделки, чтобы набрать историю доверия.")
    if not tips:
        tips.append("• Профиль выглядит аккуратно: продолжай добавлять доказательства по сделкам.")

    return (
        "👤 <b>Мой профиль</b>\n\n"
        f"Пользователь: <b>{esc(user_name(user))}</b>\n"
        f"Telegram ID: <code>{chat_id}</code>\n\n"
        f"🤝 <b>Доверие:</b> {trust_level(score)} · {score}/100\n"
        f"📁 <b>Всего сделок:</b> {len(related)}\n"
        f"🛒 <b>Как покупатель:</b> {len(as_buyer)}\n"
        f"🏪 <b>Как продавец:</b> {len(as_seller)}\n"
        f"✅ <b>Завершено:</b> {len(completed)}\n"
        f"🕒 <b>Активно:</b> {len(active)}\n"
        f"⚖️ <b>Споры:</b> {len(disputes)}\n\n"
        f"💰 <b>Баланс продавца:</b> {format_uah(balance.get('available', 0))}\n"
        f"⏳ <b>В заявках на вывод:</b> {format_uah(balance.get('pending', 0))}\n"
        f"📈 <b>Всего зачислено:</b> {format_uah(balance.get('total_credited', 0))}\n"
        f"🏷️ <b>Комиссия сервиса:</b> {SERVICE_FEE_PERCENT}%\n\n"
        f"🏦 <b>Реквизиты:</b> {'указаны' if record.get('payout_details') else 'не указаны'}\n\n"
        "<b>Полезно:</b>\n"
        + "\n".join(tips)
    )


def render_timeline(deal):
    confirmations = deal.get("confirmations") or {}
    payment = deal.get("payment") or {}
    handoff = seller_handoff_label(deal)
    lines = [
        "✅ Покупатель согласен" if confirmations.get("buyer_terms") else "▫️ Покупатель еще не согласился",
        "✅ Продавец согласен" if confirmations.get("seller_terms") else "▫️ Продавец еще не согласился",
        "✅ Оплата подтверждена" if payment.get("status") in ("success", "hold") else "▫️ Оплата еще не подтверждена",
        f"✅ Продавец передал {handoff}" if deal.get("seller_shipped") else f"▫️ Продавец еще не передал {handoff}",
        "✅ Покупатель принял результат" if deal.get("buyer_received") else "▫️ Покупатель еще не принял результат",
    ]
    return "\n".join(lines)


def next_step_text(deal):
    confirmations = deal.get("confirmations") or {}
    payment = deal.get("payment") or {}
    reviews = deal.get("admin_reviews") or {}
    status = deal.get("status")
    if status == "dispute":
        return "⚖️ Ждем решения админа по спору."
    if status == "released":
        return "✅ Сделка завершена. Продавец может вывести баланс в профиле."
    if not confirmations.get("buyer_terms"):
        return "🛒 Покупатель должен подтвердить условия сделки."
    if not confirmations.get("seller_terms"):
        return "🏪 Продавец должен подтвердить условия сделки."
    if payment.get("provider") == "crypto" and payment.get("status") in ("pending_admin", "proof_sent"):
        return "🪙 Покупатель отправляет crypto TX/скрин, админ проверяет оплату."
    if not payment.get("invoiceId") and payment.get("provider") != "crypto":
        return "💳 Покупатель должен выбрать способ оплаты и оплатить сделку."
    if payment.get("status") not in ("success", "hold"):
        return "🔄 Нужно проверить оплату после платежа."
    if not deal.get("seller_shipped") and not reviews.get("ship_requested"):
        return f"🔐 Продавец должен передать {seller_handoff_label(deal)} через бота."
    if reviews.get("ship_requested") and not reviews.get("ship_approved"):
        return f"🕵️ Админ проверяет передачу: {seller_handoff_label(deal)}."
    if deal.get("seller_shipped") and not reviews.get("receive_requested") and not deal.get("buyer_received"):
        return "🔎 Покупатель проверяет переданное и отправляет принятие на проверку."
    if reviews.get("receive_requested") and not reviews.get("receive_approved"):
        return "🕵️ Админ проверяет подтверждение покупателя."
    if reviews.get("receive_approved") and not deal.get("buyer_received"):
        return "✅ Покупатель может завершить сделку."
    return "📌 Следуй доступной кнопке в карточке сделки."


def render_payment(deal):
    payment = deal.get("payment") or {}
    if not payment:
        return "не создана"
    if payment.get("provider") == "crypto":
        asset = crypto_asset_label(payment.get("asset", "crypto"))
        status = payment.get("status", "pending_admin")
        tx = payment.get("tx_hash")
        tx_text = f" · TX: {esc(tx)}" if tx else ""
        return f"crypto · {esc(asset)} · {esc(status)}{tx_text}"
    amount = payment.get("amount")
    amount_text = f"{amount / 100:.2f} грн" if isinstance(amount, int) else "сумма неизвестна"
    return f"{esc(payment.get('provider', 'monobank'))} · {esc(payment.get('status', 'created'))} · {amount_text}"


def payout_status_label(deal):
    if deal.get("balance_credit"):
        credit = deal["balance_credit"]
        return f"зачислено на баланс {format_uah(credit.get('net', 0))}"
    payout = deal.get("payout") or {}
    labels = {
        "pending_admin": "⏳ ждет ручной выплаты",
        "paid_manual": "✅ выплачено вручную",
    }
    return labels.get(payout.get("status"), "не создана")


def render_deal(deal):
    proofs = deal.get("proofs") or []
    proof_lines = []
    for item in proofs[-6:]:
        if isinstance(item, dict):
            label = {"photo": "фото", "document": "документ", "text": "текст"}.get(item.get("type"), "файл")
            caption = item.get("caption") or item.get("text") or "без подписи"
            proof_lines.append(f"• {esc(item.get('from', 'участник'))}: {label} · {esc(caption)}")
        else:
            proof_lines.append(f"• {esc(item)}")
    proof_text = "\n".join(proof_lines) or "пока пусто"
    join_text = ""
    if not deal.get("seller_chat_id"):
        join_text = (
            "\n\n🔗 <b>Ссылка для продавца:</b>\n"
            f"<code>{esc(invite_link(deal['id']))}</code>"
        )
    timer_text = ""
    if deal.get("status") in ("awaiting_acceptance", "waiting_payment") and not terms_confirmed(deal):
        remaining = deal_accept_remaining(deal)
        timer_text = f"\n⏳ <b>На согласие осталось:</b> {remaining // 60} мин {remaining % 60} сек\n"
    return (
        f"🧾 <b>Сделка #{esc(deal['id'])}</b>\n\n"
        f"🧩 <b>Тип:</b> {esc(deal_type_label(deal))}\n"
        f"📦 <b>Предмет:</b> {esc(deal['item'])}\n"
        f"💰 <b>Сумма:</b> {esc(deal['amount'])}\n"
        f"📍 <b>Статус:</b> {status_label(deal['status'])}\n"
        f"{timer_text}"
        f"➡️ <b>Следующий шаг:</b> {next_step_text(deal)}\n"
        f"💳 <b>Оплата:</b> {render_payment(deal)}\n\n"
        f"🏦 <b>Реквизиты продавца:</b> {'указаны в профиле' if seller_payout_details_for_deal(deal) else 'не указаны'}\n"
        f"💸 <b>Выплата:</b> {payout_status_label(deal)}\n\n"
        f"👤 <b>Покупатель:</b> {esc(deal['buyer'])}\n"
        f"🏪 <b>Продавец:</b> {esc(deal['seller'])}\n"
        f"⏱️ <b>Проверка:</b> {esc(deal['inspection_time'])}\n\n"
        f"✅ <b>Условия успеха:</b>\n{esc(deal['success_terms'])}\n\n"
        f"🧭 <b>Этапы:</b>\n{render_timeline(deal)}\n\n"
        f"📎 <b>Доказательства:</b>\n{proof_text}"
        f"{join_text}"
    )


def render_step(step, data):
    deal_type = data.get("deal_type")
    item_title = "📦 <b>Шаг 2 из 6</b>\nЧто продается?"
    item_example = "Например: <code>аккаунт Fortnite</code>, <code>ключ Steam</code>, <code>игровая валюта</code>"
    terms_example = "Например: <code>данные подходят для входа, почта передана, аккаунт без блокировок</code>"
    if deal_type == "service":
        item_title = "🧩 <b>Шаг 2 из 6</b>\nКакая услуга выполняется?"
        item_example = "Например: <code>дизайн баннера</code>, <code>прокачка аккаунта</code>, <code>настройка Discord</code>"
        terms_example = "Например: <code>баннер передан в PNG и PSD</code> или <code>аккаунт прокачан до 50 уровня</code>"

    titles = {
        "deal_type": "🧩 <b>Шаг 1 из 6</b>\nВыбери тип сделки: цифровой товар или услуга.",
        "item": item_title,
        "amount": "💰 <b>Шаг 3 из 6</b>\nКакая сумма сделки в гривне?",
        "success_terms": "✅ <b>Шаг 4 из 6</b>\nЧто считается успешным завершением сделки?",
        "inspection_time": "⏱️ <b>Шаг 5 из 6</b>\nСколько времени у покупателя на проверку?",
        "counterparty": "👥 <b>Шаг 6 из 6</b>\nУкажи username продавца.",
    }
    examples = {
        "deal_type": "Например: <code>цифровой товар</code> или <code>услуга</code>",
        "item": item_example,
        "amount": "Например: <code>520 грн</code>",
        "success_terms": terms_example,
        "inspection_time": "Например: <code>24 часа после передачи</code>",
        "counterparty": "Например: <code>@seller</code>",
    }
    filled = []
    if data.get("deal_type"):
        filled.append(f"🧩 {esc(deal_type_label(data))}")
    if data.get("item"):
        filled.append(f"📦 {esc(data['item'])}")
    if data.get("amount"):
        filled.append(f"💰 {esc(data['amount'])}")
    if data.get("success_terms"):
        filled.append("✅ условия записаны")
    if data.get("inspection_time"):
        filled.append(f"⏱️ {esc(data['inspection_time'])}")
    summary = "\n\n<b>Уже записано:</b>\n" + "\n".join(filled) if filled else ""
    return f"{titles[step]}\n\n{examples[step]}{summary}"


def notify_parties(deal, text=None):
    if MINI_APP_ONLY:
        return
    deals = load_deals()
    fresh = deals.get(deal["id"], deal)
    for role, chat_id in (("buyer", fresh.get("buyer_chat_id")), ("seller", fresh.get("seller_chat_id"))):
        if chat_id:
            send_notice(chat_id, text or render_deal(fresh), deal_keyboard(fresh["id"], role))


def refresh_parties_deal(deal):
    if MINI_APP_ONLY:
        return
    deals = load_deals()
    fresh = deals.get(deal["id"], deal)
    for role, chat_id in (("buyer", fresh.get("buyer_chat_id")), ("seller", fresh.get("seller_chat_id"))):
        if chat_id:
            send_clean_message(chat_id, render_deal(fresh), deal_nav_keyboard(fresh["id"], role))


def other_party_chat_id(deal, role):
    if role == "buyer":
        return deal.get("seller_chat_id")
    if role == "seller":
        return deal.get("buyer_chat_id")
    return None


def start_deal_chat(chat_id, deal_id, role, message_id=None):
    sessions[chat_id] = {"flow": "deal_chat", "deal_id": deal_id, "role": role}
    text = (
        "💬 <b>Чат сделки включен</b>\n\n"
        "Теперь все обычные сообщения будут отправляться второй стороне по этой сделке.\n"
        "Чтобы выйти из чата, отправь <code>/stopchat</code>."
    )
    show_screen(chat_id, text, None, message_id)


def forward_deal_chat(chat_id, user, text):
    session = sessions.get(chat_id) or {}
    deal_id = session.get("deal_id")
    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal:
        sessions.pop(chat_id, None)
        send_notice(chat_id, "❌ Сделка для чата не найдена.")
        return
    role = deal_role(deal, user, chat_id)
    target_chat_id = other_party_chat_id(deal, role)
    if not target_chat_id:
        send_notice(chat_id, "👥 Вторая сторона еще не подключилась к сделке.")
        return
    label = "Покупатель" if role == "buyer" else "Продавец"
    deal.setdefault("chat", []).append({
        "from": role,
        "name": user_name(user),
        "text": text,
        "at": int(time.time()),
    })
    save_deals(deals)
    send_notice(target_chat_id, f"💬 <b>{label} по сделке #{esc(deal_id)}:</b>\n{esc(text)}")
    send_notice(chat_id, "✅ Сообщение отправлено.")


def proof_from_message(message, user):
    caption = message.get("caption") or message.get("text") or ""
    if message.get("photo"):
        file_id = message["photo"][-1]["file_id"]
        return {"type": "photo", "file_id": file_id, "caption": caption, "from": user_name(user), "at": int(time.time())}
    if message.get("document"):
        file_id = message["document"]["file_id"]
        name = message["document"].get("file_name", "document")
        return {"type": "document", "file_id": file_id, "caption": caption or name, "from": user_name(user), "at": int(time.time())}
    if caption:
        return {"type": "text", "text": caption, "from": user_name(user), "at": int(time.time())}
    return None


def send_proof_media(chat_id, proof, deal_id, prefix="📎 Доказательство"):
    caption = f"{prefix} по сделке #{esc(deal_id)}\nОт: {esc(proof.get('from'))}\n{esc(proof.get('caption') or proof.get('text') or '')}"
    if proof.get("type") == "photo":
        return send_photo(chat_id, proof["file_id"], caption)
    if proof.get("type") == "document":
        return send_document(chat_id, proof["file_id"], caption)
    return send_notice(chat_id, caption)


def broadcast_proof(deal, proof):
    for chat_id in (deal.get("buyer_chat_id"), deal.get("seller_chat_id"), ADMIN_CHAT_ID):
        if chat_id:
            send_proof_media(chat_id, proof, deal["id"])


def render_admin_payout(deal):
    details = deal.get("seller_payout_details") or "не указаны"
    payout = deal.get("payout") or {}
    receipt = payout.get("receipt") or "квитанция еще не прикреплена"
    return (
        "💸 <b>Нужна ручная выплата продавцу</b>\n\n"
        f"🧾 <b>Сделка:</b> #{esc(deal['id'])}\n"
        f"📦 <b>Предмет:</b> {esc(deal['item'])}\n"
        f"💰 <b>Сумма:</b> {esc(deal['amount'])}\n"
        f"🏪 <b>Продавец:</b> {esc(deal['seller'])}\n\n"
        f"🏦 <b>Реквизиты:</b>\n<code>{esc(details)}</code>\n\n"
        f"🧾 <b>Квитанция:</b>\n<code>{esc(receipt)}</code>\n\n"
        "После ручного перевода нажми кнопку ниже."
    )


def render_admin_withdrawal(withdrawal):
    receipt = withdrawal.get("receipt") or "квитанция еще не прикреплена"
    return (
        "💸 <b>Заявка на вывод баланса</b>\n\n"
        f"ID: <code>{esc(withdrawal['id'])}</code>\n"
        f"🏪 <b>Продавец:</b> {esc(withdrawal.get('seller'))}\n"
        f"💰 <b>Сумма:</b> {format_uah(withdrawal.get('amount', 0))}\n\n"
        f"🏦 <b>Реквизиты:</b>\n<code>{esc(withdrawal.get('details') or 'не указаны')}</code>\n\n"
        f"🧾 <b>Квитанция:</b>\n<code>{esc(receipt)}</code>"
    )


def render_admin_deal_review(deal):
    reviews = deal.get("admin_reviews") or {}
    chat = deal.get("chat") or []
    chat_lines = []
    for item in chat[-6:]:
        who = "Покупатель" if item.get("from") == "buyer" else "Продавец"
        chat_lines.append(f"• {who}: {esc(item.get('text', ''))}")
    chat_text = "\n".join(chat_lines) or "сообщений пока нет"
    return (
        "🕵️ <b>Проверка активной сделки</b>\n\n"
        + render_deal(deal)
        + "\n\n<b>Проверки админа:</b>\n"
        f"🔐 Передача: {'разрешена' if reviews.get('ship_approved') else 'не разрешена'}\n"
        f"✅ Принятие: {'разрешено' if reviews.get('receive_approved') else 'не разрешено'}\n\n"
        f"<b>Чат сторон:</b>\n{chat_text}"
    )


def render_deal_passport(deal):
    risk_score, risk_level, reasons = risk_report(deal)
    events = deal.get("events") or []
    notes = deal.get("admin_notes") or []
    ratings = deal.get("ratings") or {}
    dispute = deal.get("dispute") or {}
    event_lines = [
        f"• {event_time(item['at'])} · {esc(item.get('actor', 'system'))}: {esc(item.get('title', ''))}"
        for item in events[-12:]
    ] or ["• событий пока нет"]
    note_lines = [
        f"• {event_time(item['at'])} · {esc(item.get('text', ''))}"
        for item in notes[-6:]
    ] or ["• заметок нет"]
    return (
        "🛡️ <b>Паспорт сделки</b>\n\n"
        + render_deal(deal)
        + "\n\n<b>Риск:</b>\n"
        f"{risk_level} · {risk_score}/100\n"
        + "\n".join([f"• {esc(reason)}" for reason in reasons])
        + "\n\n<b>Журнал действий:</b>\n"
        + "\n".join(event_lines)
        + "\n\n<b>Заметки админа:</b>\n"
        + "\n".join(note_lines)
        + "\n\n<b>Спор:</b>\n"
        + (esc(dispute.get("reason_text")) if dispute else "не открыт")
        + "\n\n<b>Оценки:</b>\n"
        + f"Покупатель: {ratings.get('buyer', {}).get('rating', 'нет')}\n"
        + f"Продавец: {ratings.get('seller', {}).get('rating', 'нет')}"
    )


def list_active_deals(chat_id, message_id=None):
    deals = load_deals()
    active_statuses = {
        "awaiting_acceptance", "waiting_payment", "paid", "paid_hold",
        "shipping_review", "delivery", "receive_review", "inspection", "dispute",
    }
    active = [deal for deal in deals.values() if deal.get("status") in active_statuses]
    active = sorted(active, key=lambda deal: deal.get("created_at", 0), reverse=True)
    if not active:
        show_screen(chat_id, "✅ <b>Активных сделок нет</b>", back_menu(), message_id)
        return
    rows = [
        [{"text": compact_deal_title(deal), "callback_data": f"admin_review:{deal['id']}"}]
        for deal in active[:20]
    ]
    rows.append([{"text": "⬅️ В меню", "callback_data": "menu"}])
    show_screen(chat_id, "🕵️ <b>Активные сделки</b>\n\nВыбери сделку для проверки доказательств:", keyboard(rows), message_id)


def cleanup_candidates(kind):
    now = int(time.time())
    deals = load_deals()
    result = {}
    for deal_id, deal in deals.items():
        status = deal.get("status")
        payment = deal.get("payment") or {}
        has_payment = bool(payment.get("invoiceId")) or payment.get("status") in ("success", "hold")
        confirmations = deal.get("confirmations") or {}
        age_hours = (now - int(deal.get("created_at", now))) / 3600
        if status in ("released", "dispute", "paid", "paid_hold", "shipping_review", "delivery", "receive_review"):
            continue
        if has_payment:
            continue
        if kind == "unpaid" and not has_payment:
            result[deal_id] = deal
        elif kind == "unconfirmed" and not confirmations.get("buyer_terms") and not confirmations.get("seller_terms"):
            result[deal_id] = deal
        elif kind == "old24" and age_hours >= 24:
            result[deal_id] = deal
    return result


def cleanup_title(kind):
    return {
        "unpaid": "неоплаченные сделки без invoice",
        "unconfirmed": "сделки без подтверждения сторон",
        "old24": "безопасные сделки старше 24 часов",
    }.get(kind, "сделки")


def cleanup_keyboard():
    return keyboard([
        [{"text": "🗑️ Удалить конкретную", "callback_data": "cleanup_all_page:1"}],
        [{"text": "🧾 Неоплаченные", "callback_data": "cleanup_preview:unpaid"}],
        [{"text": "🤝 Без подтверждений", "callback_data": "cleanup_preview:unconfirmed"}],
        [{"text": "⏱️ Старше 24 часов", "callback_data": "cleanup_preview:old24"}],
        [{"text": "⬅️ В меню", "callback_data": "menu"}],
    ])


def show_cleanup_menu(chat_id, message_id=None):
    text = (
        "🧹 <b>Очистка сделок</b>\n\n"
        "Удаляются только безопасные кандидаты: без оплаты и без активного критичного статуса.\n"
        "Оплаченные, спорные, завершенные и сделки в проверке не трогаются."
    )
    show_screen(chat_id, text, cleanup_keyboard(), message_id)


def show_cleanup_preview(chat_id, kind, message_id=None):
    candidates = cleanup_candidates(kind)
    sample = list(candidates.values())[:5]
    preview = "\n".join([f"• #{deal['id']} · {esc(deal.get('item', 'без названия'))}" for deal in sample])
    if len(candidates) > 5:
        preview += f"\n• ...и еще {len(candidates) - 5}"
    if not preview:
        preview = "Кандидатов нет."
    rows = [
        [{"text": f"✅ Удалить {len(candidates)}", "callback_data": f"cleanup_confirm:{kind}"}],
        [{"text": "⬅️ Назад", "callback_data": "cleanup_menu"}],
    ]
    text = (
        f"🧹 <b>Очистка: {cleanup_title(kind)}</b>\n\n"
        f"Будет удалено: <b>{len(candidates)}</b>\n\n"
        f"{preview}"
    )
    show_screen(chat_id, text, keyboard(rows), message_id)


def run_cleanup(kind):
    candidates = cleanup_candidates(kind)
    if not candidates:
        return 0
    deals = load_deals()
    for deal_id in candidates:
        deals.pop(deal_id, None)
    save_deals(deals)
    return len(candidates)


def show_all_deals_for_delete(chat_id, message_id=None, page=1):
    deals = sorted(load_deals().values(), key=lambda deal: deal.get("created_at", 0), reverse=True)
    if not deals:
        show_screen(chat_id, "📭 Сделок нет.", cleanup_keyboard(), message_id)
        return
    per_page = 4
    total_pages = max(1, (len(deals) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    visible = deals[(page - 1) * per_page:page * per_page]
    rows = [[{"text": compact_deal_title(deal), "callback_data": f"delete_deal_preview:{deal['id']}"}] for deal in visible]
    if total_pages > 1:
        rows.append([
            {"text": f"{'• ' if index == page else ''}{index}{' •' if index == page else ''}", "callback_data": f"cleanup_all_page:{index}"}
            for index in range(1, total_pages + 1)
        ])
    rows.append([{"text": "⬅️ Назад", "callback_data": "cleanup_menu"}])
    show_screen(chat_id, f"🗑️ <b>Удалить конкретную сделку</b>\n\nСтраница {page} из {total_pages}", keyboard(rows), message_id)


def show_delete_deal_preview(chat_id, deal_id, message_id=None):
    deal = load_deals().get(deal_id)
    if not deal:
        show_screen(chat_id, "❌ Сделка уже удалена или не найдена.", cleanup_keyboard(), message_id)
        return
    rows = [
        [{"text": "🗑️ Да, удалить сделку", "callback_data": f"delete_deal_confirm:{deal_id}"}],
        [{"text": "⬅️ Назад", "callback_data": "cleanup_all_page:1"}],
    ]
    text = (
        "🗑️ <b>Подтверждение удаления</b>\n\n"
        "Эта сделка будет удалена из базы и перестанет отображаться у участников.\n\n"
        + render_deal(deal)
    )
    show_screen(chat_id, text, keyboard(rows), message_id)


def render_seller_withdrawal_success(withdrawal):
    receipt = withdrawal.get("receipt")
    receipt_text = f"\n\n🧾 <b>Квитанция:</b>\n<code>{esc(receipt)}</code>" if receipt else ""
    return (
        "✅ <b>Вывод средств выполнен</b>\n\n"
        f"Сумма: <b>{format_uah(withdrawal.get('amount', 0))}</b>\n"
        "Админ отметил выплату как отправленную."
        f"{receipt_text}"
    )


def render_seller_payout_success(deal):
    payout = deal.get("payout") or {}
    receipt = payout.get("receipt")
    receipt_text = f"\n\n🧾 <b>Квитанция:</b>\n<code>{esc(receipt)}</code>" if receipt else ""
    return (
        "✅ <b>Выплата отправлена</b>\n\n"
        f"По сделке #{esc(deal['id'])} админ отметил ручную выплату продавцу.\n"
        f"💰 <b>Сумма:</b> {esc(deal['amount'])}\n"
        f"📦 <b>Предмет:</b> {esc(deal['item'])}"
        f"{receipt_text}"
    )


def create_admin_payout_request(deal):
    deal["payout"] = {
        "status": "pending_admin",
        "created_at": int(time.time()),
        "amount": deal.get("amount"),
    }
    if ADMIN_CHAT_ID:
        send_message(ADMIN_CHAT_ID, render_admin_payout(deal), admin_payout_keyboard(deal["id"]))


def list_pending_payouts(chat_id):
    withdrawals = load_withdrawals()
    pending = [item for item in withdrawals.values() if item.get("status") == "pending_admin"]
    if not pending:
        send_notice(chat_id, "✅ <b>Ожидающих выплат нет</b>")
        return
    rows = [
        [{"text": f"#{item['id']} · {format_uah(item['amount'])} · {item.get('seller')}", "callback_data": f"withdrawal_deal:{item['id']}"}]
        for item in pending[-20:]
    ]
    rows.append([{"text": "⬅️ В меню", "callback_data": "menu"}])
    send_clean_message(chat_id, "💸 <b>Ожидающие выплаты</b>", keyboard(rows))


def parse_uah_to_kop(amount_text):
    cleaned = amount_text.replace(",", ".")
    match = re.search(r"\d+(?:\.\d{1,2})?", cleaned)
    if not match:
        return None
    return int(round(float(match.group(0)) * 100))


def format_uah(kop):
    return f"{(int(kop) / 100):.2f} грн"


def crypto_asset_label(asset):
    return {
        "ton": "TON",
        "usdt": f"USDT {CRYPTO_USDT_NETWORK}",
    }.get(asset, asset.upper())


def crypto_address(asset):
    if asset == "ton":
        return CRYPTO_TON_ADDRESS
    if asset == "usdt":
        return CRYPTO_USDT_ADDRESS
    return ""


def crypto_payment_ready(asset):
    return bool(crypto_address(asset))


def render_crypto_payment_instruction(deal):
    payment = deal.get("payment") or {}
    asset = payment.get("asset", "ton")
    address = payment.get("address") or crypto_address(asset)
    tx_hash = payment.get("tx_hash") or "не отправлен"
    return (
        f"🪙 <b>Оплата криптовалютой: {esc(crypto_asset_label(asset))}</b>\n\n"
        f"🧾 <b>Сделка:</b> #{esc(deal['id'])}\n"
        f"💰 <b>Сумма сделки:</b> {esc(deal.get('amount'))}\n\n"
        "Переведи сумму по курсу, который согласован сторонами, на адрес:\n"
        f"<code>{esc(address or 'адрес не настроен')}</code>\n\n"
        f"🔎 <b>TX / доказательство:</b> <code>{esc(tx_hash)}</code>\n\n"
        "После оплаты нажми кнопку доказательства и отправь tx hash, ссылку на транзакцию или скрин. "
        "Админ проверит перевод вручную и подтвердит оплату."
    )


def deal_amount_kop(deal):
    payment = deal.get("payment") or {}
    for key in ("finalAmount", "amount"):
        value = payment.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return parse_uah_to_kop(deal.get("amount", "")) or 0


def balance_key_for_deal(deal):
    return deal.get("seller_key") or normalize_username(deal.get("seller"))


def find_seller_deal_for_user(chat_id, user):
    key = user_key(user)
    deals = load_deals()
    return next(
        (deal for deal in deals.values() if chat_id == deal.get("seller_chat_id") or key == deal.get("seller_key")),
        None,
    )


def get_balance_record(balances, seller_key):
    return balances.setdefault(seller_key, {
        "available": 0,
        "pending": 0,
        "total_credited": 0,
        "total_fees": 0,
        "credits": [],
    })


def credit_seller_balance(deal):
    if deal.get("balance_credit"):
        return deal["balance_credit"]
    gross = deal_amount_kop(deal)
    fee = gross * SERVICE_FEE_PERCENT // 100
    net = max(0, gross - fee)
    seller_key = balance_key_for_deal(deal)
    balances = load_balances()
    record = get_balance_record(balances, seller_key)
    credit = {
        "deal_id": deal["id"],
        "seller": deal.get("seller"),
        "gross": gross,
        "fee": fee,
        "net": net,
        "created_at": int(time.time()),
    }
    record["available"] += net
    record["total_credited"] += net
    record["total_fees"] += fee
    record.setdefault("credits", []).append(credit)
    save_balances(balances)
    deal["balance_credit"] = credit
    return credit


def seller_balance_text(seller_key):
    balances = load_balances()
    record = get_balance_record(balances, seller_key)
    return (
        "💰 <b>Баланс продавца</b>\n\n"
        f"Доступно: <b>{format_uah(record.get('available', 0))}</b>\n"
        f"В заявках на вывод: <b>{format_uah(record.get('pending', 0))}</b>\n"
        f"Всего зачислено: <b>{format_uah(record.get('total_credited', 0))}</b>\n"
        f"Комиссия сервиса: <b>{SERVICE_FEE_PERCENT}%</b>"
    )


SERVICE_KNOWLEDGE = """
Сервис — Telegram-гарант сделок для цифровых товаров и услуг.
Типы сделок: цифровой товар и услуга. Физическая доставка сейчас не используется.
Создание сделки: покупатель выбирает тип, предмет, сумму, условия успеха, срок проверки и username продавца.
Лимиты: минимум 10 грн, максимум 10000 грн, максимум 10 доказательств на сделку, максимум 3 новые сделки в час.
Согласие: покупатель и продавец обязаны подтвердить условия. Если стартового согласия нет 10 минут, сделка удаляется.
Оплата: доступны Monobank и crypto. Monobank проверяется через invoice/status. Crypto TON/USDT проверяется админом вручную по tx hash, ссылке или скрину.
После оплаты продавец передает цифровой товар или результат услуги через бота. Передача сохраняется как доказательство.
Админ проверяет передачу и разрешает покупателю принимать результат.
Покупатель проверяет результат, отправляет принятие на проверку, после разрешения админа завершает сделку.
После завершения деньги зачисляются продавцу на внутренний баланс минус комиссия 2%.
Вывод: продавец указывает реквизиты в профиле и создает заявку на вывод. Админ переводит вручную и прикрепляет квитанцию.
Спор: любая сторона может открыть спор. Админ смотрит паспорт сделки, доказательства, чат и решает возвратом покупателю или зачислением продавцу.
Профиль: баланс продавца, реквизиты, доверие, количество сделок, активные сделки, споры и статистика.
Безопасность: админ может блокировать пользователей, удалять конкретные сделки, смотреть активные сделки, доказательства и чат.
"""


def fallback_ai_answer(question):
    text = question.lower()
    if any(word in text for word in ("привет", "здрав", "hello", "ку", "салам")):
        return "Привет. Я консультант сервиса гаранта. Могу объяснить, как создать сделку, оплатить через Mono или crypto, передать товар, открыть спор, вывести баланс или пройти проверку админа."
    if "комисс" in text or "процент" in text:
        return "Комиссия сервиса 2%. После завершения сделки сумма минус комиссия зачисляется продавцу на баланс."
    if "крипт" in text or "ton" in text or "usdt" in text:
        return "Crypto-оплата работает через ручную проверку админа: покупатель выбирает TON или USDT, переводит средства на указанный адрес, отправляет tx hash/ссылку/скрин, после чего админ подтверждает оплату в сделке."
    if "mono" in text or "монобанк" in text or "monobank" in text:
        return "Monobank-оплата создается после согласия обеих сторон. Покупатель оплачивает счет, затем бот проверяет статус через Monobank. После успешной оплаты продавец может передать товар или результат услуги."
    if "вывод" in text or "выплат" in text or "баланс" in text:
        return "Продавец указывает реквизиты, после завершения сделки получает деньги на баланс и создает заявку на вывод. Админ переводит вручную и прикрепляет квитанцию."
    if "спор" in text or "обман" in text or "кинул" in text or "проблем" in text:
        return "Если что-то пошло не так, участник открывает спор. Деньги остаются под контролем до решения админа."
    if "доказ" in text or "скрин" in text or "фото" in text:
        return f"В сделку можно добавить до {MAX_PROOFS_PER_DEAL} доказательств: текст, фото или документ. Передача цифрового товара или результата услуги тоже сохраняется как доказательство."
    if "товар" in text or "услуг" in text or "перед" in text:
        return "После подтвержденной оплаты продавец передает цифровой товар или результат услуги через бота: ключ, логин/пароль, файл, ссылку, архив, макет или описание результата. Админ проверяет передачу перед этапом принятия."
    if "созд" in text or "сделк" in text or "как работает" in text:
        return "Схема такая: создание сделки → согласие обеих сторон → оплата Mono или crypto → передача товара/результата через бота → проверка админом → принятие покупателем → завершение → деньги на баланс продавца минус 2%."
    return (
        "Я могу ответить по любому вопросу сервиса: создание сделки, оплата Mono/crypto, передача цифрового товара, услуги, доказательства, спор, баланс, вывод, комиссия, админ-проверка и безопасность. "
        "Опиши ситуацию обычными словами, например: «покупатель не подтверждает», «как оплатить USDT», «как продавцу вывести деньги»."
    )


def ask_ai_agent(question):
    if not OPENAI_API_KEY:
        return fallback_ai_answer(question)
    payload = {
        "model": OPENAI_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Ты умный консультант Telegram-сервиса гаранта сделок. Отвечай на русском, дружелюбно, конкретно и по делу. "
                    "Не говори, что знаешь только конкретные вопросы: помогай по любой ситуации внутри сервиса. "
                    "Если вопрос опасный или спорный, советуй открыть спор или обратиться к админу. "
                    "Не обещай автоматический crypto-hold: crypto сейчас подтверждается админом вручную.\n\n"
                    f"База знаний сервиса:\n{SERVICE_KNOWLEDGE}"
                ),
            },
            {"role": "user", "content": question},
        ],
        "temperature": 0.3,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"].strip()
    except Exception as error:
        return fallback_ai_answer(question) + f"\n\nAI временно недоступен: {esc(error)}"


def create_withdrawal_request(deal, seller_key):
    balances = load_balances()
    record = get_balance_record(balances, seller_key)
    amount = record.get("available", 0)
    if amount <= 0:
        return {"ok": False, "description": "На балансе нет доступных средств для вывода."}
    withdrawals = load_withdrawals()
    withdrawal_id = f"{int(time.time())}{len(withdrawals) + 1}"
    record["available"] -= amount
    record["pending"] += amount
    withdrawal = {
        "id": withdrawal_id,
        "seller_key": seller_key,
        "seller": deal.get("seller"),
        "seller_chat_id": deal.get("seller_chat_id"),
        "amount": amount,
        "details": deal.get("seller_payout_details"),
        "status": "pending_admin",
        "created_at": int(time.time()),
    }
    withdrawals[withdrawal_id] = withdrawal
    save_balances(balances)
    save_withdrawals(withdrawals)
    if ADMIN_CHAT_ID:
        send_message(ADMIN_CHAT_ID, render_admin_withdrawal(withdrawal), admin_withdrawal_keyboard(withdrawal_id))
    return {"ok": True, "withdrawal": withdrawal}


def create_profile_withdrawal_request(chat_id, user):
    key = user_key(user)
    record = user_record(user)
    details = record.get("payout_details")
    if not details:
        return {"ok": False, "description": "Сначала укажи реквизиты в профиле."}
    deals = load_deals()
    has_dispute = any(
        deal.get("status") == "dispute" and (deal.get("seller_key") == key or deal.get("seller_chat_id") == chat_id)
        for deal in deals.values()
    )
    if has_dispute:
        return {"ok": False, "description": "Вывод заблокирован, пока у продавца есть открытый спор."}
    balances = load_balances()
    balance = get_balance_record(balances, key)
    amount = balance.get("available", 0)
    if amount <= 0:
        return {"ok": False, "description": "На балансе нет доступных средств для вывода."}
    withdrawals = load_withdrawals()
    withdrawal_id = f"{int(time.time())}{len(withdrawals) + 1}"
    balance["available"] -= amount
    balance["pending"] += amount
    withdrawal = {
        "id": withdrawal_id,
        "seller_key": key,
        "seller": user_name(user),
        "seller_chat_id": chat_id,
        "amount": amount,
        "details": details,
        "status": "pending_admin",
        "created_at": int(time.time()),
    }
    withdrawals[withdrawal_id] = withdrawal
    save_balances(balances)
    save_withdrawals(withdrawals)
    if ADMIN_CHAT_ID:
        send_message(ADMIN_CHAT_ID, render_admin_withdrawal(withdrawal), admin_withdrawal_keyboard(withdrawal_id))
    return {"ok": True, "withdrawal": withdrawal}


def mono_webhook_url():
    if not PUBLIC_BASE_URL:
        return None
    return f"{PUBLIC_BASE_URL}/webhooks/monobank/{MONO_WEBHOOK_SECRET}"


def create_mono_invoice(deal):
    amount_kop = parse_uah_to_kop(deal["amount"])
    if not amount_kop:
        return {"ok": False, "description": "Не смог понять сумму. Укажи сумму в грн, например: 520 грн."}
    payload = {
        "amount": amount_kop,
        "ccy": 980,
        "merchantPaymInfo": {
            "reference": deal["id"],
            "destination": f"Garant deal #{deal['id']}: {deal['item'][:80]}",
        },
        "redirectUrl": PUBLIC_BASE_URL or "https://t.me",
        "validity": 3600,
        "paymentType": MONO_PAYMENT_TYPE,
    }
    webhook_url = mono_webhook_url()
    if webhook_url:
        payload["webHookUrl"] = webhook_url
    result = safe_mono_api("POST", "/api/merchant/invoice/create", payload)
    if not result["ok"]:
        return result
    invoice = result["result"]
    deal["payment"] = {
        "provider": "monobank",
        "invoiceId": invoice.get("invoiceId"),
        "pageUrl": invoice.get("pageUrl"),
        "amount": amount_kop,
        "ccy": 980,
        "paymentType": MONO_PAYMENT_TYPE,
        "status": "created",
        "created_at": int(time.time()),
    }
    deal["status"] = "waiting_payment"
    return {"ok": True, "result": invoice}


def apply_mono_payment_update(deal, payload):
    payment = deal.setdefault("payment", {"provider": "monobank"})
    status = payload.get("status") or payment.get("status")
    payment.update({
        "invoiceId": payload.get("invoiceId") or payment.get("invoiceId"),
        "status": status,
        "amount": payload.get("amount") or payment.get("amount"),
        "finalAmount": payload.get("finalAmount") or payment.get("finalAmount"),
        "ccy": payload.get("ccy") or payment.get("ccy"),
        "failureReason": payload.get("failureReason"),
        "updated_at": int(time.time()),
    })
    if status in ("success", "hold"):
        deal["status"] = "paid_hold" if payment.get("paymentType") == "hold" else "paid"
    elif status in ("failure", "expired", "reversed"):
        deal["status"] = "payment_failed"


def refresh_mono_status(deal):
    invoice_id = (deal.get("payment") or {}).get("invoiceId")
    if not invoice_id:
        return {"ok": False, "description": "У этой сделки еще нет счета Monobank."}
    result = safe_mono_api("GET", "/api/merchant/invoice/status", query={"invoiceId": invoice_id})
    if result["ok"]:
        apply_mono_payment_update(deal, result["result"])
    return result


def finalize_mono_hold(deal):
    invoice_id = (deal.get("payment") or {}).get("invoiceId")
    if not invoice_id:
        return {"ok": False, "description": "Нет invoiceId для финализации."}
    status_result = refresh_mono_status(deal)
    if not status_result["ok"]:
        return status_result
    payment = deal.get("payment") or {}
    if payment.get("status") == "success":
        return {"ok": True, "result": {"status": "success", "already_finalized": True}}
    if payment.get("status") != "hold":
        return {"ok": False, "description": f"Счет еще не в hold. Текущий статус: {payment.get('status', 'unknown')}"}
    amounts = [value for value in (payment.get("amount"), payment.get("finalAmount")) if isinstance(value, int) and value > 0]
    amount = min(amounts) if amounts else None
    payload = {"invoiceId": invoice_id}
    if amount:
        payload["amount"] = amount
    result = safe_mono_api("POST", "/api/merchant/invoice/finalize", payload)
    if result["ok"]:
        payment["status"] = "success"
        payment["finalAmount"] = amount
        payment["finalized_at"] = int(time.time())
    return result


def create_crypto_payment(deal, asset):
    address = crypto_address(asset)
    if not address:
        return {"ok": False, "description": f"Адрес для {crypto_asset_label(asset)} не настроен."}
    deal["payment"] = {
        "provider": "crypto",
        "asset": asset,
        "asset_label": crypto_asset_label(asset),
        "address": address,
        "status": "pending_admin",
        "amount_uah": deal.get("amount"),
        "created_at": int(time.time()),
    }
    deal["status"] = "waiting_payment"
    add_event(deal, "system", f"создана crypto-оплата {crypto_asset_label(asset)}")
    return {"ok": True}


def start_new_deal(chat_id, user, message_id=None):
    if is_user_blocked(user):
        show_screen(chat_id, "🚫 Твой профиль заблокирован для создания сделок. Обратись к админу.", back_menu(), message_id)
        return
    if not can_create_deal(user):
        show_screen(chat_id, f"⏳ Лимит: максимум {MAX_NEW_DEALS_PER_HOUR} сделки в час. Попробуй позже.", back_menu(), message_id)
        return
    sessions[chat_id] = {
        "flow": "new_deal",
        "step": "deal_type",
        "creator": user_name(user),
        "creator_key": user_key(user),
        "data": {},
        "prompt_message_id": message_id,
    }
    result = show_screen(chat_id, render_step("deal_type", {}), deal_type_menu(), message_id)
    sessions[chat_id]["prompt_message_id"] = result.get("result", {}).get("message_id") or message_id


def send_next_flow_prompt(chat_id):
    session = sessions[chat_id]
    delete_message(chat_id, session.get("prompt_message_id"))
    markup = deal_type_menu() if session["step"] == "deal_type" else cancel_menu()
    result = send_message(chat_id, render_step(session["step"], session["data"]), markup)
    message_id = result.get("result", {}).get("message_id")
    session["prompt_message_id"] = message_id
    if message_id:
        interface_messages[chat_id] = message_id


def continue_new_deal(chat_id, text):
    session = sessions[chat_id]
    step = session["step"]
    data = session["data"]

    if step == "deal_type":
        normalized = text.strip().lower()
        if normalized in ("1", "цифровой", "цифровой товар", "digital", "товар"):
            data["deal_type"] = "digital"
        elif normalized in ("2", "услуга", "service", "работа"):
            data["deal_type"] = "service"
        else:
            send_notice(chat_id, "🧩 Напиши <code>цифровой товар</code> или <code>услуга</code>.")
            return
        session["step"] = "item"
        send_next_flow_prompt(chat_id)
        return

    if step == "item":
        data["item"] = text
        session["step"] = "amount"
        send_next_flow_prompt(chat_id)
        return
    if step == "amount":
        amount_kop = parse_uah_to_kop(text)
        if amount_kop is None:
            send_notice(chat_id, "💰 Не понял сумму. Напиши, например: <code>250 грн</code>.")
            return
        if amount_kop < MIN_DEAL_AMOUNT_KOP:
            send_notice(chat_id, f"💰 Минимальная сумма сделки: <b>{format_uah(MIN_DEAL_AMOUNT_KOP)}</b>.")
            return
        if amount_kop > MAX_DEAL_AMOUNT_KOP:
            send_notice(chat_id, f"💰 Максимальная сумма сделки: <b>{format_uah(MAX_DEAL_AMOUNT_KOP)}</b>.")
            return
        data["amount"] = text
        session["step"] = "success_terms"
        send_next_flow_prompt(chat_id)
        return
    if step == "success_terms":
        data["success_terms"] = text
        session["step"] = "inspection_time"
        send_next_flow_prompt(chat_id)
        return
    if step == "inspection_time":
        data["inspection_time"] = text
        session["step"] = "counterparty"
        send_next_flow_prompt(chat_id)
        return

    if step == "counterparty":
        deals = load_deals()
        deal_id = str(int(time.time()))
        seller_username = text.strip()
        if normalize_username(seller_username) == session["creator_key"]:
            send_notice(chat_id, "⛔ Нельзя создать сделку с самим собой. Укажи username другой стороны.")
            return
        deal = {
            "id": deal_id,
            "deal_type": data["deal_type"],
            "item": data["item"],
            "amount": data["amount"],
            "success_terms": data["success_terms"],
            "inspection_time": data["inspection_time"],
            "buyer": session["creator"],
            "buyer_key": session["creator_key"],
            "buyer_chat_id": chat_id,
            "seller": seller_username,
            "seller_key": normalize_username(seller_username),
            "seller_chat_id": None,
            "status": "awaiting_acceptance",
            "confirmations": {"buyer_terms": False, "seller_terms": False},
            "proofs": [],
            "created_at": int(time.time()),
        }
        add_event(deal, session["creator"], "создал сделку", data["item"])
        deals[deal_id] = deal
        save_deals(deals)
        mark_deal_created({"id": chat_id, "username": session["creator"].lstrip("@") if session["creator"].startswith("@") else None})
        delete_message(chat_id, session.get("prompt_message_id"))
        sessions.pop(chat_id, None)
        text = (
            "✨ <b>Сделка создана</b>\n\n"
            "Теперь обе стороны должны подтвердить условия. Отправь продавцу ссылку из карточки.\n\n"
            + render_deal(deal)
        )
        send_clean_message(chat_id, text, deal_nav_keyboard(deal_id, "buyer"))
        if ADMIN_CHAT_ID:
            send_message(ADMIN_CHAT_ID, "🆕 <b>Новая сделка создана</b>\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))


def join_deal(chat_id, user, deal_id):
    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal:
        send_notice(chat_id, "❌ Сделка не найдена.")
        return
    key = user_key(user)
    if key != deal.get("seller_key") and chat_id != deal.get("buyer_chat_id"):
        send_notice(chat_id, "⛔ Эта ссылка не для твоего аккаунта. Проверь username в Telegram.")
        return
    if key == deal.get("seller_key"):
        deal["seller_chat_id"] = chat_id
        deal["seller"] = user_name(user)
        save_deals(deals)
        refresh_parties_deal(deal)
        return
    send_notice(chat_id, render_deal(deal), deal_nav_keyboard(deal_id, "buyer"))


def compact_deal_title(deal, limit=48):
    text = f"#{deal['id']} · {deal['item']} · {status_label(deal['status'])}"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def list_deals(chat_id, user, message_id=None, page=1):
    key = user_key(user)
    deals = load_deals()
    own_deals = [
        deal for deal in deals.values()
        if chat_id in (deal.get("buyer_chat_id"), deal.get("seller_chat_id"))
        or key in (deal.get("buyer_key"), deal.get("seller_key"))
    ]
    own_deals = sorted(own_deals, key=lambda deal: deal.get("created_at", 0), reverse=True)
    if not own_deals:
        show_screen(chat_id, "📭 <b>Сделок пока нет</b>\n\nСоздай первую сделку или присоединись по ссылке.", back_menu(), message_id)
        return
    per_page = 4
    total_pages = max(1, (len(own_deals) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    visible_deals = own_deals[start:start + per_page]
    rows = [
        [{"text": compact_deal_title(deal), "callback_data": f"deal:{deal['id']}"}]
        for deal in visible_deals
    ]
    if total_pages > 1:
        rows.append([
            {
                "text": f"{'• ' if index == page else ''}{index}{' •' if index == page else ''}",
                "callback_data": f"deals_page:{index}",
            }
            for index in range(1, total_pages + 1)
        ])
    rows.append([{"text": "⬅️ В меню", "callback_data": "menu"}])
    text = f"📁 <b>Твои сделки</b>\n\nВыбери карточку:\nСтраница {page} из {total_pages}"
    show_screen(chat_id, text, keyboard(rows), message_id)


def handle_callback(callback):
    callback_id = callback["id"]
    message = callback["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    user = callback["from"]
    data = callback["data"]
    answer_callback(callback_id)

    if data == "menu":
        sessions.pop(chat_id, None)
        show_screen(chat_id, render_menu(), main_menu(chat_id), message_id)
        return
    if data == "cancel":
        sessions.pop(chat_id, None)
        show_screen(chat_id, "✖️ Действие отменено. Нажми /start, чтобы открыть главное меню.", None, message_id)
        return
    if data == "new_deal":
        start_new_deal(chat_id, user, message_id)
        return
    if data.startswith("deal_type:"):
        session = sessions.get(chat_id)
        if not session or session.get("flow") != "new_deal" or session.get("step") != "deal_type":
            show_screen(chat_id, "🧩 Сначала начни создание сделки.", back_menu(), message_id)
            return
        selected_type = data.split(":", 1)[1]
        if selected_type not in ("digital", "service"):
            show_screen(chat_id, "🧩 Неизвестный тип сделки.", deal_type_menu(), message_id)
            return
        session["data"]["deal_type"] = selected_type
        session["step"] = "item"
        result = show_screen(chat_id, render_step("item", session["data"]), cancel_menu(), message_id)
        session["prompt_message_id"] = result.get("result", {}).get("message_id") or message_id
        return
    if data == "my_deals":
        list_deals(chat_id, user, message_id)
        return
    if data == "profile":
        show_screen(chat_id, render_profile(chat_id, user), profile_keyboard(chat_id, user), message_id)
        return
    if data == "profile_payout_details":
        sessions[chat_id] = {"flow": "profile_payout_details", "prompt_message_id": message_id}
        show_screen(chat_id, "🏦 <b>Реквизиты для вывода</b>\n\nОтправь карту/IBAN и имя получателя одним сообщением.\n\nНапример:\n<code>5168 **** **** 1234, Иван Петров</code>", cancel_menu(), message_id)
        return
    if data == "profile_withdraw":
        result = create_profile_withdrawal_request(chat_id, user)
        if not result["ok"]:
            show_screen(chat_id, f"💰 <b>Вывод недоступен</b>\n\n{esc(result['description'])}", profile_keyboard(chat_id, user), message_id)
            return
        withdrawal = result["withdrawal"]
        show_screen(
            chat_id,
            "💸 <b>Заявка на вывод создана</b>\n\n"
            f"Сумма: <b>{format_uah(withdrawal['amount'])}</b>\n"
            "Админ получил заявку и выполнит перевод вручную.",
            profile_keyboard(chat_id, user),
            message_id,
        )
        return
    if data == "admin_payouts":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        list_pending_payouts(chat_id)
        return
    if data == "admin_active_deals":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        list_active_deals(chat_id, message_id)
        return
    if data == "cleanup_menu":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        show_cleanup_menu(chat_id, message_id)
        return
    if data.startswith("cleanup_all_page:"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        page = int(data.split(":", 1)[1])
        show_all_deals_for_delete(chat_id, message_id, page)
        return
    if data.startswith("delete_deal_preview:"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        deal_id = data.split(":", 1)[1]
        show_delete_deal_preview(chat_id, deal_id, message_id)
        return
    if data.startswith("delete_deal_confirm:"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        deal_id = data.split(":", 1)[1]
        deleted = delete_deal_everywhere(deal_id)
        if deleted:
            show_screen(chat_id, f"✅ Сделка #{esc(deal_id)} удалена.", cleanup_keyboard(), message_id)
        else:
            show_screen(chat_id, "❌ Сделка уже удалена или не найдена.", cleanup_keyboard(), message_id)
        return
    if data.startswith("cleanup_preview:"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        kind = data.split(":", 1)[1]
        show_cleanup_preview(chat_id, kind, message_id)
        return
    if data.startswith("cleanup_confirm:"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        kind = data.split(":", 1)[1]
        deleted = run_cleanup(kind)
        show_screen(chat_id, f"✅ Очистка завершена.\n\nУдалено сделок: <b>{deleted}</b>", cleanup_keyboard(), message_id)
        return
    if data.startswith("deals_page:"):
        page = int(data.split(":", 1)[1])
        list_deals(chat_id, user, message_id, page=page)
        return
    if data == "whoami":
        show_screen(chat_id, f"🧭 <b>Твой Telegram ID:</b> <code>{chat_id}</code>\n<b>Username:</b> {esc(user_name(user))}", back_menu(), message_id)
        return
    if data == "ai_agent":
        sessions[chat_id] = {"flow": "ai_agent"}
        show_screen(chat_id, "🤖 <b>AI-консультант включен</b>\n\nНапиши вопрос по сервису, оплате, комиссии, выводу или спору.\n\nЧтобы выйти: <code>/stopai</code>", back_menu(), message_id)
        return
    if data == "how_it_works":
        text = (
            "⚙️ <b>Как проходит сделка</b>\n\n"
            "1. Покупатель создает карточку.\n"
            "2. Продавец присоединяется по ссылке.\n"
            "3. Оба подтверждают условия.\n"
            "4. Покупатель оплачивает через Monobank.\n"
            "5. Продавец добавляет доказательства и отправляет этап на проверку.\n"
            "6. Админ разрешает следующий шаг.\n"
            "7. Покупатель подтверждает получение через проверку админа.\n"
            "8. Бот финализирует hold, либо открывается спор с причиной."
        )
        show_screen(chat_id, text, back_menu(), message_id)
        return

    if is_user_blocked(user) and chat_id != ADMIN_CHAT_ID:
        show_screen(chat_id, "🚫 Твой профиль заблокирован для действий в сделках. Обратись к админу.", back_menu(), message_id)
        return

    if data.startswith("withdrawal_"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        action, withdrawal_id = data.split(":", 1)
        withdrawals = load_withdrawals()
        withdrawal = withdrawals.get(withdrawal_id)
        if not withdrawal:
            show_screen(chat_id, "❌ Заявка на вывод не найдена.", back_menu(), message_id)
            return
        if action == "withdrawal_deal":
            show_screen(chat_id, render_admin_withdrawal(withdrawal), admin_withdrawal_keyboard(withdrawal_id), message_id)
            return
        if action == "withdrawal_receipt":
            sessions[chat_id] = {"flow": "withdrawal_receipt", "withdrawal_id": withdrawal_id, "prompt_message_id": message_id}
            show_screen(chat_id, "🧾 <b>Квитанция вывода</b>\n\nОтправь следующим сообщением ссылку, номер операции или описание выплаты.", admin_withdrawal_keyboard(withdrawal_id), message_id)
            return
        if action == "withdrawal_paid":
            withdrawal["status"] = "paid_manual"
            withdrawal["paid_at"] = int(time.time())
            withdrawal["admin_chat_id"] = chat_id
            withdrawals[withdrawal_id] = withdrawal
            balances = load_balances()
            record = get_balance_record(balances, withdrawal["seller_key"])
            record["pending"] = max(0, record.get("pending", 0) - withdrawal.get("amount", 0))
            save_balances(balances)
            save_withdrawals(withdrawals)
            seller_chat_id = withdrawal.get("seller_chat_id")
            if seller_chat_id:
                send_notice(seller_chat_id, render_seller_withdrawal_success(withdrawal))
            show_screen(chat_id, "✅ Вывод отмечен как выполненный.", admin_withdrawal_keyboard(withdrawal_id), message_id)
            return
        if action == "withdrawal_reject":
            withdrawal["status"] = "rejected"
            withdrawal["rejected_at"] = int(time.time())
            withdrawals[withdrawal_id] = withdrawal
            balances = load_balances()
            record = get_balance_record(balances, withdrawal["seller_key"])
            amount = withdrawal.get("amount", 0)
            record["pending"] = max(0, record.get("pending", 0) - amount)
            record["available"] = record.get("available", 0) + amount
            save_balances(balances)
            save_withdrawals(withdrawals)
            seller_chat_id = withdrawal.get("seller_chat_id")
            if seller_chat_id:
                send_notice(seller_chat_id, f"⚠️ <b>Заявка на вывод отклонена</b>\n\n{format_uah(amount)} вернулись на баланс.")
            show_screen(chat_id, "⚠️ Заявка отклонена, сумма возвращена на баланс продавца.", admin_withdrawal_keyboard(withdrawal_id), message_id)
            return

    if data.startswith("dispute_reason:"):
        _, reason, deal_id = data.split(":", 2)
        deals = load_deals()
        deal = deals.get(deal_id)
        if not deal:
            show_screen(chat_id, "❌ Сделка не найдена.", main_menu(chat_id), message_id)
            return
        role = deal_role(deal, user, chat_id)
        if role == "guest":
            show_screen(chat_id, "⛔ Ты не участник этой сделки.", None, message_id)
            return
        deal["status"] = "dispute"
        deal["dispute"] = {
            "reason": reason,
            "reason_text": dispute_reason_title(reason),
            "opened_by": role,
            "opened_at": int(time.time()),
        }
        add_event(deal, role_title(role), "открыл спор", dispute_reason_title(reason))
        save_deals(deals)
        refresh_parties_deal(deal)
        if ADMIN_CHAT_ID:
            send_message(ADMIN_CHAT_ID, "⚖️ <b>Открыт спор</b>\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
        return

    action, deal_id = data.split(":", 1)
    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal:
        show_screen(chat_id, "❌ Сделка не найдена.", main_menu(chat_id), message_id)
        return

    if action.startswith("admin_"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", None, message_id)
            return
        if action == "admin_deal":
            show_screen(chat_id, render_admin_payout(deal), admin_payout_keyboard(deal_id), message_id)
            return
        if action == "admin_dispute":
            deal["status"] = "dispute"
            save_deals(deals)
            refresh_parties_deal(deal)
            show_screen(chat_id, "⚠️ Спор открыт.", admin_payout_keyboard(deal_id), message_id)
            return
        if action == "admin_receipt":
            sessions[chat_id] = {"flow": "admin_receipt", "deal_id": deal_id, "prompt_message_id": message_id}
            show_screen(
                chat_id,
                "🧾 <b>Квитанция выплаты</b>\n\nОтправь следующим сообщением ссылку, номер операции или короткое описание квитанции.\n\nНапример:\n<code>Mono перевод 12:41, ID 847392</code>",
                admin_payout_keyboard(deal_id),
                message_id,
            )
            return
        if action == "admin_paid":
            deal.setdefault("payout", {})
            deal["payout"]["status"] = "paid_manual"
            deal["payout"]["paid_at"] = int(time.time())
            deal["payout"]["admin_chat_id"] = chat_id
            deal["status"] = "released"
            save_deals(deals)
            seller_chat_id = deal.get("seller_chat_id")
            if seller_chat_id:
                send_notice(seller_chat_id, render_seller_payout_success(deal), deal_nav_keyboard(deal_id, "seller"))
            buyer_chat_id = deal.get("buyer_chat_id")
            if buyer_chat_id:
                send_notice(buyer_chat_id, "✅ <b>Админ отметил выплату продавцу</b>\n\nСделка полностью закрыта.\n\n" + render_deal(deal), deal_nav_keyboard(deal_id, "buyer"))
            show_screen(chat_id, "✅ Выплата отмечена как выполненная.", admin_payout_keyboard(deal_id), message_id)
            return

    if action == "admin_review":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        show_screen(chat_id, render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_passport":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        show_screen(chat_id, render_deal_passport(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_note":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        sessions[chat_id] = {"flow": "admin_note", "deal_id": deal_id, "prompt_message_id": message_id}
        show_screen(chat_id, "📝 <b>Админская заметка</b>\n\nОтправь текст внутренней заметки по сделке.", cancel_menu(), message_id)
        return

    if action in ("admin_block_buyer", "admin_block_seller"):
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        target_key = deal.get("buyer_key") if action == "admin_block_buyer" else deal.get("seller_key")
        target_name = deal.get("buyer") if action == "admin_block_buyer" else deal.get("seller")
        users = load_users()
        record = users.setdefault(target_key, {"name": target_name})
        record["blocked"] = True
        record["blocked_reason"] = f"заблокирован админом по сделке #{deal_id}"
        users[target_key] = record
        add_event(deal, "admin", f"заблокировал пользователя {target_name}")
        save_users(users)
        save_deals(deals)
        show_screen(chat_id, render_deal_passport(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_approve_ship":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        if not deal.get("handoff"):
            show_screen(chat_id, f"🔐 Продавец еще не передал {seller_handoff_label(deal)} через бота.", admin_deal_review_keyboard(deal_id), message_id)
            return
        deal.setdefault("admin_reviews", {})["ship_approved"] = True
        deal["seller_shipped"] = True
        deal["status"] = "delivery"
        add_event(deal, "admin", "разрешил этап передачи")
        save_deals(deals)
        refresh_parties_deal(deal)
        show_screen(chat_id, render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_approve_receive":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        if not deal.get("seller_shipped"):
            show_screen(chat_id, "🔐 Сначала должна быть подтверждена передача результата.", admin_deal_review_keyboard(deal_id), message_id)
            return
        deal.setdefault("admin_reviews", {})["receive_approved"] = True
        add_event(deal, "admin", "разрешил этап принятия")
        save_deals(deals)
        refresh_parties_deal(deal)
        show_screen(chat_id, render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_crypto_approve":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        payment = deal.get("payment") or {}
        if payment.get("provider") != "crypto":
            show_screen(chat_id, "🪙 В этой сделке нет crypto-оплаты.", admin_deal_review_keyboard(deal_id), message_id)
            return
        payment["status"] = "success"
        payment["approved_at"] = int(time.time())
        payment["approved_by"] = chat_id
        deal["payment"] = payment
        deal["status"] = "paid"
        add_event(deal, "admin", f"подтвердил crypto-оплату {payment.get('asset_label') or payment.get('asset')}")
        save_deals(deals)
        refresh_parties_deal(deal)
        show_screen(chat_id, render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_crypto_reject":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        payment = deal.get("payment") or {}
        if payment.get("provider") != "crypto":
            show_screen(chat_id, "🪙 В этой сделке нет crypto-оплаты.", admin_deal_review_keyboard(deal_id), message_id)
            return
        payment["status"] = "rejected"
        payment["rejected_at"] = int(time.time())
        payment["rejected_by"] = chat_id
        deal.setdefault("payment_history", []).append(payment)
        deal["payment"] = {}
        deal["status"] = "payment_failed"
        add_event(deal, "admin", "отклонил crypto-оплату")
        save_deals(deals)
        refresh_parties_deal(deal)
        show_screen(chat_id, render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_review_dispute":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        deal["status"] = "dispute"
        add_event(deal, "admin", "открыл спор")
        save_deals(deals)
        refresh_parties_deal(deal)
        show_screen(chat_id, render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_resolve_refund":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        deal["status"] = "refunded"
        deal["dispute_resolution"] = {"result": "refund", "at": int(time.time())}
        add_event(deal, "admin", "решил спор: возврат покупателю")
        save_deals(deals)
        refresh_parties_deal(deal)
        show_screen(chat_id, render_deal_passport(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    if action == "admin_resolve_seller":
        if ADMIN_CHAT_ID != chat_id:
            show_screen(chat_id, "⛔ Это действие доступно только админу.", back_menu(), message_id)
            return
        credit = credit_seller_balance(deal)
        deal["status"] = "released"
        deal["dispute_resolution"] = {"result": "seller", "at": int(time.time())}
        add_event(deal, "admin", "решил спор: зачислить продавцу", format_uah(credit["net"]))
        save_deals(deals)
        refresh_parties_deal(deal)
        show_screen(chat_id, render_deal_passport(deal), admin_deal_review_keyboard(deal_id), message_id)
        return

    role = deal_role(deal, user, chat_id)
    if role == "guest":
        show_screen(chat_id, "⛔ Ты не участник этой сделки.", None, message_id)
        return

    if action == "deal":
        show_screen(chat_id, render_deal(deal), deal_nav_keyboard(deal_id, role), message_id)
        return

    if action == "passport":
        show_screen(chat_id, render_deal_passport(deal), deal_nav_keyboard(deal_id, role), message_id)
        return

    if action.startswith("rate_"):
        if deal.get("status") != "released":
            show_screen(chat_id, "⭐ Оценку можно оставить только после завершения сделки.", deal_nav_keyboard(deal_id, role), message_id)
            return
        rating = int(action.split("_", 1)[1])
        deal.setdefault("ratings", {})[role] = {"rating": rating, "from": user_name(user), "at": int(time.time())}
        add_event(deal, role_title(role), f"оставил оценку {rating}/5")
        save_deals(deals)
        show_screen(chat_id, "⭐ Спасибо, оценка сохранена.\n\n" + render_deal_passport(deal), deal_nav_keyboard(deal_id, role), message_id)
        return

    if action == "chat":
        start_deal_chat(chat_id, deal_id, role, message_id)
        return

    if action == "balance":
        if role != "seller":
            show_screen(chat_id, "⛔ Баланс доступен только продавцу.", deal_keyboard(deal_id, role), message_id)
            return
        show_screen(chat_id, render_profile(chat_id, user), profile_keyboard(chat_id, user), message_id)
        return

    if action == "withdraw":
        if role != "seller":
            show_screen(chat_id, "⛔ Вывод доступен только продавцу.", deal_keyboard(deal_id, role), message_id)
            return
        result = create_profile_withdrawal_request(chat_id, user)
        if not result["ok"]:
            show_screen(chat_id, f"💰 <b>Вывод недоступен</b>\n\n{esc(result['description'])}", profile_keyboard(chat_id, user), message_id)
            return
        withdrawal = result["withdrawal"]
        show_screen(
            chat_id,
            "💸 <b>Заявка на вывод создана</b>\n\n"
            f"Сумма: <b>{format_uah(withdrawal['amount'])}</b>\n"
            "Админ получил заявку и выполнит перевод вручную.",
            profile_keyboard(chat_id, user),
            message_id,
        )
        return

    if action == "accept":
        deal.setdefault("confirmations", {})
        deal["confirmations"][f"{role}_terms"] = True
        if terms_confirmed(deal):
            deal["status"] = "waiting_payment"
        add_event(deal, role_title(role), "подтвердил условия")
        save_deals(deals)
        refresh_parties_deal(deal)
        return

    if action == "mono_create":
        if role != "buyer":
            show_screen(chat_id, "⛔ Оплату создает только покупатель.", deal_keyboard(deal_id, role), message_id)
            return
        if not terms_confirmed(deal):
            show_screen(chat_id, "🤝 Сначала обе стороны должны подтвердить условия сделки.", deal_keyboard(deal_id, role), message_id)
            return
        if not MONO_TOKEN:
            show_screen(chat_id, "💳 Monobank не настроен. Добавь <code>MONO_TOKEN</code> и перезапусти бота.", deal_keyboard(deal_id, role), message_id)
            return
        result = create_mono_invoice(deal)
        add_event(deal, role_title(role), "создал счет Monobank")
        save_deals(deals)
        if not result["ok"]:
            show_screen(chat_id, f"❌ <b>Не удалось создать оплату</b>\n\n<code>{esc(result['description'])}</code>", deal_keyboard(deal_id, role), message_id)
            return
        refresh_parties_deal(deal)
        return

    if action == "mono_status":
        result = refresh_mono_status(deal)
        add_event(deal, role_title(role), "проверил оплату")
        save_deals(deals)
        if not result["ok"]:
            show_screen(chat_id, f"❌ <b>Не удалось проверить оплату</b>\n\n<code>{esc(result['description'])}</code>", deal_keyboard(deal_id, role), message_id)
            return
        refresh_parties_deal(deal)
        return

    if action == "crypto_menu":
        if role != "buyer":
            show_screen(chat_id, "⛔ Crypto-оплату выбирает только покупатель.", deal_keyboard(deal_id, role), message_id)
            return
        if not terms_confirmed(deal):
            show_screen(chat_id, "🤝 Сначала обе стороны должны подтвердить условия сделки.", deal_keyboard(deal_id, role), message_id)
            return
        if not CRYPTO_TON_ADDRESS and not CRYPTO_USDT_ADDRESS:
            show_screen(chat_id, "🪙 Crypto-оплата не настроена. Добавь адрес TON или USDT в переменные окружения.", deal_keyboard(deal_id, role), message_id)
            return
        show_screen(chat_id, "🪙 <b>Выбери crypto-оплату</b>\n\nПосле перевода отправь TX hash, ссылку на транзакцию или скрин. Админ проверит оплату вручную.", crypto_payment_keyboard(deal_id), message_id)
        return

    if action.startswith("crypto_create_"):
        if role != "buyer":
            show_screen(chat_id, "⛔ Crypto-оплату создает только покупатель.", deal_keyboard(deal_id, role), message_id)
            return
        asset = action.replace("crypto_create_", "", 1)
        result = create_crypto_payment(deal, asset)
        if not result["ok"]:
            show_screen(chat_id, f"❌ <b>Crypto не настроена</b>\n\n{esc(result['description'])}", deal_keyboard(deal_id, role), message_id)
            return
        save_deals(deals)
        show_screen(chat_id, render_crypto_payment_instruction(deal), deal_nav_keyboard(deal_id, role), message_id)
        if ADMIN_CHAT_ID:
            send_message(ADMIN_CHAT_ID, "🪙 <b>Создана crypto-оплата</b>\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
        return

    if action == "crypto_card":
        if (deal.get("payment") or {}).get("provider") != "crypto":
            show_screen(chat_id, "🪙 Crypto-оплата еще не создана.", deal_keyboard(deal_id, role), message_id)
            return
        show_screen(chat_id, render_crypto_payment_instruction(deal), deal_nav_keyboard(deal_id, role), message_id)
        return

    if action == "crypto_proof":
        if role != "buyer":
            show_screen(chat_id, "⛔ Доказательство crypto-оплаты отправляет покупатель.", deal_keyboard(deal_id, role), message_id)
            return
        if (deal.get("payment") or {}).get("provider") != "crypto":
            show_screen(chat_id, "🪙 Сначала создай crypto-оплату.", deal_keyboard(deal_id, role), message_id)
            return
        sessions[chat_id] = {"flow": "crypto_proof", "deal_id": deal_id, "prompt_message_id": message_id}
        show_screen(chat_id, "🔎 <b>Доказательство crypto-оплаты</b>\n\nОтправь tx hash, ссылку на транзакцию или скрин оплаты.", cancel_menu(), message_id)
        return

    if action == "ship":
        if role != "seller":
            show_screen(chat_id, "⛔ Передачу результата выполняет только продавец.", deal_keyboard(deal_id, role), message_id)
            return
        if deal.get("status") not in ("paid", "paid_hold", "delivery", "inspection"):
            show_screen(chat_id, "💳 Сначала нужно подтвердить оплату.", deal_keyboard(deal_id, role), message_id)
            return
        sessions[chat_id] = {"flow": "handoff", "deal_id": deal_id, "prompt_message_id": message_id}
        prompt = (
            f"🔐 <b>Передача: {esc(seller_handoff_label(deal))}</b>\n\n"
            "Отправь следующим сообщением то, что должен получить покупатель: ключ, логин/пароль, файл, ссылку, архив, макет или описание результата.\n\n"
            "Это сообщение будет сохранено как доказательство, отправлено покупателю и попадет админу на проверку."
        )
        show_screen(chat_id, prompt, cancel_menu(), message_id)
        return

    if action == "receive":
        if role != "buyer":
            show_screen(chat_id, "⛔ Принятие результата подтверждает только покупатель.", deal_keyboard(deal_id, role), message_id)
            return
        if not deal.get("seller_shipped"):
            show_screen(chat_id, f"🔐 Сначала продавец должен передать {seller_handoff_label(deal)}.", deal_keyboard(deal_id, role), message_id)
            return
        payment_status = (deal.get("payment") or {}).get("status")
        if payment_status not in ("success", "hold"):
            show_screen(chat_id, "💳 Оплата еще не подтверждена Monobank.", deal_keyboard(deal_id, role), message_id)
            return
        if not (deal.get("admin_reviews") or {}).get("receive_approved"):
            deal.setdefault("admin_reviews", {})["receive_requested"] = True
            deal["status"] = "receive_review"
            add_event(deal, role_title(role), "отправил принятие результата на проверку")
            save_deals(deals)
            refresh_parties_deal(deal)
            if ADMIN_CHAT_ID:
                send_message(ADMIN_CHAT_ID, "🕵️ <b>Нужна проверка принятия результата</b>\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
            return
        finalize = finalize_mono_hold(deal) if (deal.get("payment") or {}).get("paymentType") == "hold" else {"ok": True}
        if not finalize["ok"]:
            show_screen(chat_id, f"❌ <b>Не удалось финализировать hold</b>\n\n<code>{esc(finalize['description'])}</code>", deal_keyboard(deal_id, role), message_id)
            return
        deal["buyer_received"] = True
        deal["status"] = "released"
        credit = credit_seller_balance(deal)
        add_event(deal, role_title(role), "завершил сделку", f"зачислено {format_uah(credit['net'])}")
        save_deals(deals)
        deals = load_deals()
        deal = deals[deal_id]
        refresh_parties_deal(deal)
        return

    if action == "proof":
        sessions[chat_id] = {"flow": "proof", "deal_id": deal_id, "prompt_message_id": message_id}
        show_screen(chat_id, "📎 <b>Добавление доказательства</b>\n\nОтправь трек-номер, ссылку, описание фото/видео или комментарий.", cancel_menu(), message_id)
        return

    if action == "payout_details":
        if role != "seller":
            show_screen(chat_id, "⛔ Реквизиты указывает только продавец.", deal_keyboard(deal_id, role), message_id)
            return
        sessions[chat_id] = {"flow": "profile_payout_details", "prompt_message_id": message_id}
        show_screen(chat_id, "🏦 <b>Реквизиты для вывода</b>\n\nОтправь карту/IBAN и имя получателя одним сообщением.\n\nНапример:\n<code>5168 **** **** 1234, Иван Петров</code>", cancel_menu(), message_id)
        return

    if action == "dispute":
        show_screen(chat_id, "⚠️ <b>Причина спора</b>\n\nВыбери причину, чтобы админ сразу понимал контекст.", dispute_reason_keyboard(deal_id), message_id)


def handle_message(message):
    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    user = message.get("from", {})

    if text == "/stopai":
        if sessions.get(chat_id, {}).get("flow") == "ai_agent":
            sessions.pop(chat_id, None)
            send_notice(chat_id, "🤖 Консультант выключен.")
            return
        send_notice(chat_id, "🤖 Консультант сейчас не включен.")
        return

    if text == "/stopchat":
        if sessions.get(chat_id, {}).get("flow") == "deal_chat":
            sessions.pop(chat_id, None)
            send_notice(chat_id, "💬 Чат сделки выключен.")
            return
        send_notice(chat_id, "💬 Сейчас ты не в чате сделки.")
        return

    if text.startswith("/start"):
        sessions.pop(chat_id, None)
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("join_"):
            join_deal(chat_id, user, parts[1].replace("join_", "", 1))
            return
        send_clean_message(chat_id, render_menu(), main_menu(chat_id))
        return
    if text == "/newdeal":
        sessions.pop(chat_id, None)
        start_new_deal(chat_id, user)
        return
    if text == "/mydeals":
        sessions.pop(chat_id, None)
        list_deals(chat_id, user)
        return
    if text.startswith("/join"):
        sessions.pop(chat_id, None)
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            join_deal(chat_id, user, parts[1].strip())
        else:
            send_notice(chat_id, "Напиши так: <code>/join номер_сделки</code>")
        return
    if text == "/whoami":
        send_notice(chat_id, f"🧭 <b>Твой Telegram ID:</b> <code>{chat_id}</code>\n<b>Username:</b> {esc(user_name(user))}")
        return
    if text == "/payouts":
        sessions.pop(chat_id, None)
        if ADMIN_CHAT_ID != chat_id:
            send_notice(chat_id, "⛔ Эта команда доступна только админу.")
            return
        list_pending_payouts(chat_id)
        return
    if text == "/cleanup":
        sessions.pop(chat_id, None)
        if ADMIN_CHAT_ID != chat_id:
            send_notice(chat_id, "⛔ Эта команда доступна только админу.")
            return
        show_cleanup_menu(chat_id)
        return
    if text == "/help":
        help_text = (
            "🧭 <b>Подсказка</b>\n\n"
            "/newdeal — создать сделку\n"
            "/mydeals — открыть список\n"
            "/join ID — присоединиться к сделке\n"
            "/stopchat — выйти из чата сделки\n"
            "/whoami — узнать свой ID\n"
            "/start — главное меню"
        )
        send_notice(chat_id, help_text)
        return
    if text.startswith("/"):
        send_notice(chat_id, "Команда не распознана. Нажми /start, чтобы открыть меню.")
        return

    if chat_id in sessions:
        flow = sessions[chat_id]["flow"]
        if flow == "ai_agent":
            answer = ask_ai_agent(text)
            send_notice(chat_id, f"🤖 <b>Консультант:</b>\n{esc(answer)}")
            return
        if flow == "deal_chat":
            forward_deal_chat(chat_id, user, text)
            return
        if flow == "new_deal":
            continue_new_deal(chat_id, text)
            return
        if flow == "crypto_proof":
            deal_id = sessions[chat_id]["deal_id"]
            deals = load_deals()
            deal = deals.get(deal_id)
            if deal:
                role = deal_role(deal, user, chat_id)
                if role != "buyer":
                    send_notice(chat_id, "⛔ Доказательство crypto-оплаты отправляет только покупатель.")
                    sessions.pop(chat_id, None)
                    return
                payment = deal.setdefault("payment", {})
                proof = proof_from_message(message, user)
                if not proof:
                    send_notice(chat_id, "🔎 Отправь tx hash, ссылку, фото или документ.")
                    return
                payment["status"] = "proof_sent"
                payment["tx_hash"] = proof.get("text") or proof.get("caption") or "медиа-доказательство"
                payment["proof"] = proof
                payment["proof_at"] = int(time.time())
                deal["payment"] = payment
                add_event(deal, role_title(role), "отправил доказательство crypto-оплаты", payment["tx_hash"])
                save_deals(deals)
                refresh_parties_deal(deal)
                if ADMIN_CHAT_ID:
                    send_message(ADMIN_CHAT_ID, "🪙 <b>Покупатель отправил crypto-доказательство</b>\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
                    send_proof_media(ADMIN_CHAT_ID, proof, deal_id, prefix="🪙 Crypto proof")
            sessions.pop(chat_id, None)
            return
        if flow == "handoff":
            deal_id = sessions[chat_id]["deal_id"]
            deals = load_deals()
            deal = deals.get(deal_id)
            if deal:
                role = deal_role(deal, user, chat_id)
                if role != "seller":
                    send_notice(chat_id, "⛔ Передать результат может только продавец.")
                    sessions.pop(chat_id, None)
                    return
                if len(deal.get("proofs") or []) >= MAX_PROOFS_PER_DEAL:
                    send_notice(chat_id, f"📎 Лимит доказательств: максимум {MAX_PROOFS_PER_DEAL} на сделку.")
                    sessions.pop(chat_id, None)
                    return
                proof = proof_from_message(message, user)
                if not proof:
                    send_notice(chat_id, "🔐 Отправь текст, фото или документ для передачи покупателю.")
                    return
                proof["category"] = "handoff"
                proof["caption"] = proof.get("caption") or proof.get("text") or seller_handoff_label(deal)
                deal["handoff"] = proof
                deal.setdefault("proofs", []).append(proof)
                deal.setdefault("admin_reviews", {})["ship_requested"] = True
                deal["status"] = "shipping_review"
                add_event(deal, role_title(role), f"передал {seller_handoff_label(deal)} на проверку")
                save_deals(deals)
                broadcast_proof(deal, proof)
                refresh_parties_deal(deal)
                if ADMIN_CHAT_ID:
                    send_message(ADMIN_CHAT_ID, f"🕵️ <b>Нужна проверка передачи: {esc(seller_handoff_label(deal))}</b>\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
            sessions.pop(chat_id, None)
            return
        if flow == "proof":
            deal_id = sessions[chat_id]["deal_id"]
            deals = load_deals()
            deal = deals.get(deal_id)
            if deal:
                if len(deal.get("proofs") or []) >= MAX_PROOFS_PER_DEAL:
                    send_notice(chat_id, f"📎 Лимит доказательств: максимум {MAX_PROOFS_PER_DEAL} на сделку.")
                    sessions.pop(chat_id, None)
                    return
                proof = proof_from_message(message, user)
                if not proof:
                    send_notice(chat_id, "📎 Отправь текст, фото или документ доказательства.")
                    return
                deal.setdefault("proofs", []).append(proof)
                add_event(deal, user_name(user), f"добавил доказательство: {proof.get('type')}")
                save_deals(deals)
                broadcast_proof(deal, proof)
                refresh_parties_deal(deal)
            sessions.pop(chat_id, None)
            return
        if flow == "admin_note":
            if ADMIN_CHAT_ID != chat_id:
                send_notice(chat_id, "⛔ Заметку может добавить только админ.")
                sessions.pop(chat_id, None)
                return
            deal_id = sessions[chat_id]["deal_id"]
            deals = load_deals()
            deal = deals.get(deal_id)
            if deal:
                deal.setdefault("admin_notes", []).append({"text": text, "at": int(time.time())})
                add_event(deal, "admin", "добавил внутреннюю заметку")
                save_deals(deals)
                send_clean_message(chat_id, render_deal_passport(deal), admin_deal_review_keyboard(deal_id))
            sessions.pop(chat_id, None)
            return
        if flow == "payout_details":
            deal_id = sessions[chat_id]["deal_id"]
            deals = load_deals()
            deal = deals.get(deal_id)
            if deal:
                role = deal_role(deal, user, chat_id)
                if role == "seller":
                    deal["seller_payout_details"] = text
                    save_deals(deals)
                    if sessions[chat_id].get("source") == "profile":
                        send_clean_message(chat_id, render_profile(chat_id, user), profile_keyboard(chat_id, user))
                    else:
                        refresh_parties_deal(deal)
                else:
                    send_notice(chat_id, "⛔ Реквизиты может указать только продавец.")
            sessions.pop(chat_id, None)
            return
        if flow == "profile_payout_details":
            users = load_users()
            key = user_key(user)
            record = users.setdefault(key, {"name": user_name(user), "blocked": False, "created_deals": []})
            record["name"] = user_name(user)
            record["chat_id"] = user.get("id")
            record["payout_details"] = text
            users[key] = record
            save_users(users)
            send_clean_message(chat_id, render_profile(chat_id, user), profile_keyboard(chat_id, user))
            sessions.pop(chat_id, None)
            return
        if flow == "admin_receipt":
            if ADMIN_CHAT_ID != chat_id:
                send_notice(chat_id, "⛔ Квитанцию может прикрепить только админ.")
                sessions.pop(chat_id, None)
                return
            deal_id = sessions[chat_id]["deal_id"]
            deals = load_deals()
            deal = deals.get(deal_id)
            if deal:
                deal.setdefault("payout", {})
                deal["payout"]["receipt"] = text
                deal["payout"]["receipt_added_at"] = int(time.time())
                save_deals(deals)
                send_clean_message(chat_id, "🧾 <b>Квитанция прикреплена</b>\n\n" + render_admin_payout(deal), admin_payout_keyboard(deal_id))
            sessions.pop(chat_id, None)
            return
        if flow == "withdrawal_receipt":
            if ADMIN_CHAT_ID != chat_id:
                send_notice(chat_id, "⛔ Квитанцию может прикрепить только админ.")
                sessions.pop(chat_id, None)
                return
            withdrawal_id = sessions[chat_id]["withdrawal_id"]
            withdrawals = load_withdrawals()
            withdrawal = withdrawals.get(withdrawal_id)
            if withdrawal:
                withdrawal["receipt"] = text
                withdrawal["receipt_added_at"] = int(time.time())
                withdrawals[withdrawal_id] = withdrawal
                save_withdrawals(withdrawals)
                send_clean_message(chat_id, "🧾 <b>Квитанция прикреплена</b>\n\n" + render_admin_withdrawal(withdrawal), admin_withdrawal_keyboard(withdrawal_id))
            sessions.pop(chat_id, None)
            return

    if text.startswith("/start"):
        sessions.pop(chat_id, None)
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].startswith("join_"):
            join_deal(chat_id, user, parts[1].replace("join_", "", 1))
            return
        send_clean_message(chat_id, render_menu(), main_menu(chat_id))
        return
    if text == "/newdeal":
        start_new_deal(chat_id, user)
        return
    if text == "/mydeals":
        list_deals(chat_id, user)
        return
    if text.startswith("/join"):
        parts = text.split(maxsplit=1)
        if len(parts) == 2:
            join_deal(chat_id, user, parts[1].strip())
        else:
            send_notice(chat_id, "Напиши так: <code>/join номер_сделки</code>")
        return
    if text == "/whoami":
        send_notice(chat_id, f"🧭 <b>Твой Telegram ID:</b> <code>{chat_id}</code>\n<b>Username:</b> {esc(user_name(user))}")
        return
    if text == "/payouts":
        if ADMIN_CHAT_ID != chat_id:
            send_notice(chat_id, "⛔ Эта команда доступна только админу.")
            return
        list_pending_payouts(chat_id)
        return
    if text == "/help":
        help_text = (
            "🧭 <b>Подсказка</b>\n\n"
            "/newdeal — создать сделку\n"
            "/mydeals — открыть список\n"
            "/join ID — присоединиться к сделке\n"
            "/whoami — узнать свой ID\n"
            "/payouts — заявки на выплаты для админа\n"
            "/start — главное меню"
        )
        send_notice(chat_id, help_text)
        return

    send_notice(chat_id, "👋 Нажми /start, чтобы открыть главное меню.")


def web_json(handler, status, payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type, X-Telegram-Init-Data")
    handler.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def web_read_json(handler):
    length = int(handler.headers.get("Content-Length", "0"))
    if not length:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def parse_telegram_init_data(init_data):
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    if not API_TOKEN or not received_hash:
        return None
    data_check = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", API_TOKEN.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(secret, data_check.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None


def web_user(handler):
    init_data = handler.headers.get("X-Telegram-Init-Data", "")
    user = parse_telegram_init_data(init_data) if init_data else None
    if user:
        return user
    host = handler.headers.get("Host", "")
    if MINI_APP_DEV_MODE or host.startswith("localhost") or host.startswith("127.0.0.1"):
        return {"id": ADMIN_CHAT_ID or 5783661880, "username": "dev", "first_name": "Dev"}
    return None


def web_user_name(user):
    username = user.get("username")
    return f"@{username}" if username else user.get("first_name") or str(user.get("id"))


def web_status_label(status):
    return {
        "awaiting_acceptance": "Ждет согласия",
        "waiting_payment": "Ждет оплату",
        "paid": "Оплачено",
        "paid_hold": "Оплата в hold",
        "payment_failed": "Оплата не прошла",
        "shipping_review": "Передача на проверке",
        "delivery": "Покупатель проверяет",
        "receive_review": "Принятие на проверке",
        "inspection": "Проверка",
        "dispute": "Спор",
        "released": "Завершена",
        "refunded": "Возврат",
    }.get(status, status or "unknown")


def web_deal_actions(deal, role, is_admin=False):
    status = deal.get("status")
    payment = deal.get("payment") or {}
    reviews = deal.get("admin_reviews") or {}
    actions = []
    if role in ("buyer", "seller") and not deal.get("confirmations", {}).get(f"{role}_terms"):
        actions.append({"id": "accept", "label": "Согласен с условиями", "tone": "primary"})
    if role == "buyer" and terms_confirmed(deal) and status == "waiting_payment":
        actions.append({"id": "mono_create", "label": "Оплатить Mono", "tone": "primary"})
        if CRYPTO_TON_ADDRESS:
            actions.append({"id": "crypto_create_ton", "label": "Оплатить TON", "tone": "secondary"})
        if CRYPTO_USDT_ADDRESS:
            actions.append({"id": "crypto_create_usdt", "label": f"Оплатить USDT {CRYPTO_USDT_NETWORK}", "tone": "secondary"})
    if payment.get("provider") == "monobank" and payment.get("status") not in ("success", "hold"):
        actions.append({"id": "mono_status", "label": "Проверить оплату", "tone": "secondary"})
    if payment.get("provider") == "crypto" and role == "buyer" and payment.get("status") != "success":
        actions.append({"id": "crypto_proof", "label": "Отправить TX", "tone": "secondary", "needsText": True})
    if role == "seller" and payment.get("status") in ("success", "hold") and not deal.get("seller_shipped") and status not in ("shipping_review", "released", "refunded", "dispute"):
        actions.append({"id": "ship", "label": "Передать результат", "tone": "primary", "needsText": True})
    if role == "buyer" and deal.get("seller_shipped") and payment.get("status") in ("success", "hold") and not deal.get("buyer_received") and status not in ("released", "refunded", "dispute"):
        actions.append({"id": "receive", "label": "Завершить сделку" if reviews.get("receive_approved") else "Принять результат", "tone": "primary"})
    if role in ("buyer", "seller") and status not in ("released", "refunded", "dispute"):
        actions.append({"id": "proof", "label": "Добавить доказательство", "tone": "secondary", "needsText": True})
        actions.append({"id": "dispute", "label": "Открыть спор", "tone": "danger", "needsText": True})
    if status == "released" and role in ("buyer", "seller"):
        actions.append({"id": "rate_5", "label": "Оценить 5", "tone": "secondary"})
    if is_admin:
        if status == "shipping_review":
            actions.append({"id": "admin_approve_ship", "label": "Разрешить передачу", "tone": "primary"})
        if status == "receive_review":
            actions.append({"id": "admin_approve_receive", "label": "Разрешить принятие", "tone": "primary"})
        if payment.get("provider") == "crypto" and payment.get("status") == "pending_manual":
            actions.append({"id": "admin_crypto_approve", "label": "Crypto оплачен", "tone": "primary"})
            actions.append({"id": "admin_crypto_reject", "label": "Crypto отклонить", "tone": "danger"})
        if status == "dispute":
            actions.append({"id": "admin_resolve_refund", "label": "Возврат покупателю", "tone": "danger"})
            actions.append({"id": "admin_resolve_seller", "label": "Деньги продавцу", "tone": "primary"})
    return actions


def web_serialize_deal(deal, user):
    chat_id = int(user.get("id"))
    role = deal_role(deal, user, chat_id)
    is_admin = ADMIN_CHAT_ID == chat_id
    payment = deal.get("payment") or {}
    return {
        "id": deal.get("id"),
        "type": deal.get("deal_type", "digital"),
        "typeLabel": "Услуга" if deal.get("deal_type") == "service" else "Цифровой товар",
        "item": deal.get("item", ""),
        "amount": deal.get("amount", ""),
        "status": deal.get("status", ""),
        "statusLabel": web_status_label(deal.get("status")),
        "role": "admin" if is_admin and role == "guest" else role,
        "buyer": deal.get("buyer", ""),
        "seller": deal.get("seller", ""),
        "sellerJoined": bool(deal.get("seller_chat_id")),
        "successTerms": deal.get("success_terms", ""),
        "inspectionTime": deal.get("inspection_time", ""),
        "confirmations": deal.get("confirmations", {}),
        "payment": {
            "provider": payment.get("provider"),
            "status": payment.get("status"),
            "pageUrl": payment.get("pageUrl"),
            "assetLabel": payment.get("asset_label"),
            "address": payment.get("address"),
            "txHash": payment.get("tx_hash"),
        },
        "handoff": deal.get("handoff"),
        "proofs": deal.get("proofs", []),
        "chat": deal.get("chat", [])[-20:],
        "events": deal.get("events", [])[-20:],
        "actions": web_deal_actions(deal, role, is_admin),
        "createdAt": deal.get("created_at"),
    }


def web_profile(user):
    chat_id = int(user.get("id"))
    key = user_key(user)
    deals = list(load_deals().values())
    related = [
        deal for deal in deals
        if chat_id in (deal.get("buyer_chat_id"), deal.get("seller_chat_id"))
        or key in (deal.get("buyer_key"), deal.get("seller_key"))
    ]
    balance = get_balance_record(load_balances(), key)
    record = user_record(user)
    return {
        "id": chat_id,
        "name": web_user_name(user),
        "isAdmin": ADMIN_CHAT_ID == chat_id,
        "payoutDetails": record.get("payout_details", ""),
        "stats": {
            "total": len(related),
            "active": len([deal for deal in related if deal.get("status") not in ("released", "refunded")]),
            "completed": len([deal for deal in related if deal.get("status") == "released"]),
            "disputes": len([deal for deal in related if deal.get("status") == "dispute"]),
        },
        "balance": {
            "available": format_uah(balance.get("available", 0)),
            "pending": format_uah(balance.get("pending", 0)),
            "totalCredited": format_uah(balance.get("total_credited", 0)),
        },
    }


def web_visible_deals(user):
    chat_id = int(user.get("id"))
    key = user_key(user)
    is_admin = ADMIN_CHAT_ID == chat_id
    deals = load_deals()
    visible = [
        deal for deal in deals.values()
        if is_admin
        or chat_id in (deal.get("buyer_chat_id"), deal.get("seller_chat_id"))
        or key in (deal.get("buyer_key"), deal.get("seller_key"))
    ]
    visible = sorted(visible, key=lambda deal: deal.get("created_at", 0), reverse=True)
    return [web_serialize_deal(deal, user) for deal in visible]


def web_create_deal(user, payload):
    if is_user_blocked(user):
        return {"ok": False, "description": "Профиль заблокирован для создания сделок."}
    if not can_create_deal(user):
        return {"ok": False, "description": f"Лимит: максимум {MAX_NEW_DEALS_PER_HOUR} сделки в час."}
    amount = str(payload.get("amount", "")).strip()
    amount_kop = parse_uah_to_kop(amount)
    if amount_kop is None:
        return {"ok": False, "description": "Не понял сумму. Например: 250 грн."}
    if amount_kop < MIN_DEAL_AMOUNT_KOP or amount_kop > MAX_DEAL_AMOUNT_KOP:
        return {"ok": False, "description": f"Сумма должна быть от {format_uah(MIN_DEAL_AMOUNT_KOP)} до {format_uah(MAX_DEAL_AMOUNT_KOP)}."}
    seller = str(payload.get("seller", "")).strip()
    if not seller:
        return {"ok": False, "description": "Укажи username продавца."}
    if normalize_username(seller) == user_key(user):
        return {"ok": False, "description": "Нельзя создать сделку с самим собой."}
    deal_id = str(int(time.time()))
    deal = {
        "id": deal_id,
        "deal_type": payload.get("dealType") if payload.get("dealType") in ("digital", "service") else "digital",
        "item": str(payload.get("item", "")).strip(),
        "amount": amount,
        "success_terms": str(payload.get("successTerms", "")).strip(),
        "inspection_time": str(payload.get("inspectionTime", "")).strip(),
        "buyer": web_user_name(user),
        "buyer_key": user_key(user),
        "buyer_chat_id": int(user.get("id")),
        "seller": seller,
        "seller_key": normalize_username(seller),
        "seller_chat_id": None,
        "status": "awaiting_acceptance",
        "confirmations": {"buyer_terms": False, "seller_terms": False},
        "proofs": [],
        "chat": [],
        "events": [],
        "admin_reviews": {},
        "created_at": int(time.time()),
    }
    if not deal["item"] or not deal["success_terms"] or not deal["inspection_time"]:
        return {"ok": False, "description": "Заполни предмет, условия успеха и время проверки."}
    deals = load_deals()
    add_event(deal, deal["buyer"], "создал сделку", deal["item"])
    deals[deal_id] = deal
    save_deals(deals)
    mark_deal_created(user)
    if ADMIN_CHAT_ID:
        send_message(ADMIN_CHAT_ID, "Новая сделка создана в Mini App\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
    return {"ok": True, "deal": web_serialize_deal(deal, user)}


def web_create_deal_v2(user, payload):
    if is_user_blocked(user):
        return {"ok": False, "description": "Профиль заблокирован для создания сделок."}
    if not can_create_deal(user):
        return {"ok": False, "description": f"Лимит: максимум {MAX_NEW_DEALS_PER_HOUR} сделки в час."}

    creator_role = payload.get("creatorRole") if payload.get("creatorRole") in ("buyer", "seller") else "buyer"
    counterparty = str(payload.get("counterparty") or payload.get("seller") or "").strip()
    if not counterparty:
        return {"ok": False, "description": "Укажи username второй стороны."}
    if normalize_username(counterparty) == user_key(user):
        return {"ok": False, "description": "Нельзя создать сделку с самим собой."}

    amount_value = str(payload.get("amountValue") or payload.get("amount") or "").strip().replace(",", ".")
    currency = payload.get("currency") or "UAH"
    if currency != "UAH":
        return {"ok": False, "description": "Сейчас доступна только гривна."}
    if not re.fullmatch(r"\d+(\.\d{1,2})?", amount_value):
        return {"ok": False, "description": "Сумма должна быть числом, например 250 или 250.50."}
    amount = f"{amount_value} грн"
    amount_kop = parse_uah_to_kop(amount)
    if amount_kop is None:
        return {"ok": False, "description": "Не понял сумму. Например: 250 грн."}
    if amount_kop < MIN_DEAL_AMOUNT_KOP or amount_kop > MAX_DEAL_AMOUNT_KOP:
        return {"ok": False, "description": f"Сумма должна быть от {format_uah(MIN_DEAL_AMOUNT_KOP)} до {format_uah(MAX_DEAL_AMOUNT_KOP)}."}

    deal_id = str(int(time.time()))
    current_name = web_user_name(user)
    current_key = user_key(user)
    current_chat_id = int(user.get("id"))
    if creator_role == "buyer":
        buyer, buyer_key, buyer_chat_id = current_name, current_key, current_chat_id
        seller, seller_key, seller_chat_id = counterparty, normalize_username(counterparty), None
    else:
        buyer, buyer_key, buyer_chat_id = counterparty, normalize_username(counterparty), None
        seller, seller_key, seller_chat_id = current_name, current_key, current_chat_id

    deal = {
        "id": deal_id,
        "deal_type": payload.get("dealType") if payload.get("dealType") in ("digital", "service") else "digital",
        "creator_role": creator_role,
        "item": str(payload.get("item", "")).strip(),
        "amount": amount,
        "amount_value": amount_value,
        "currency": currency,
        "success_terms": str(payload.get("successTerms", "")).strip(),
        "inspection_time": str(payload.get("inspectionTime", "")).strip(),
        "buyer": buyer,
        "buyer_key": buyer_key,
        "buyer_chat_id": buyer_chat_id,
        "seller": seller,
        "seller_key": seller_key,
        "seller_chat_id": seller_chat_id,
        "status": "awaiting_acceptance",
        "confirmations": {"buyer_terms": False, "seller_terms": False},
        "proofs": [],
        "chat": [],
        "events": [],
        "admin_reviews": {},
        "created_at": int(time.time()),
    }
    if not deal["item"] or not deal["success_terms"] or not deal["inspection_time"]:
        return {"ok": False, "description": "Заполни предмет, условия успеха и время проверки."}

    deals = load_deals()
    add_event(deal, current_name, "создал сделку в Mini App", deal["item"])
    deals[deal_id] = deal
    save_deals(deals)
    mark_deal_created(user)
    if ADMIN_CHAT_ID:
        send_plain_notice(ADMIN_CHAT_ID, f"Новая сделка #{deal_id} создана в Mini App.\nОткрой приложение: {mini_app_url() or '/app/'}")
    return {"ok": True, "deal": web_serialize_deal(deal, user)}


def web_join_deal(user, deal_id):
    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal:
        return {"ok": False, "description": "Сделка не найдена."}
    chat_id = int(user.get("id"))
    key = user_key(user)
    joined = False
    if key == deal.get("seller_key") and not deal.get("seller_chat_id"):
        deal["seller_chat_id"] = chat_id
        deal["seller"] = web_user_name(user)
        joined = True
    if key == deal.get("buyer_key") and not deal.get("buyer_chat_id"):
        deal["buyer_chat_id"] = chat_id
        deal["buyer"] = web_user_name(user)
        joined = True
    if not joined and deal_role(deal, user, chat_id) == "guest":
        return {"ok": False, "description": "Эта сделка не для твоего Telegram username."}
    if joined:
        add_event(deal, web_user_name(user), "подключился к сделке через Mini App")
        save_deals(deals)
    return {"ok": True, "deal": web_serialize_deal(deal, user)}


def web_apply_deal_action(user, deal_id, payload):
    deals = load_deals()
    deal = deals.get(deal_id)
    if not deal:
        return {"ok": False, "description": "Сделка не найдена."}
    chat_id = int(user.get("id"))
    key = user_key(user)
    if key == deal.get("seller_key") and not deal.get("seller_chat_id"):
        deal["seller_chat_id"] = chat_id
        deal["seller"] = web_user_name(user)
    if key == deal.get("buyer_key") and not deal.get("buyer_chat_id"):
        deal["buyer_chat_id"] = chat_id
        deal["buyer"] = web_user_name(user)
    role = deal_role(deal, user, chat_id)
    is_admin = ADMIN_CHAT_ID == chat_id
    action = payload.get("action")
    text = str(payload.get("text", "")).strip()
    if role == "guest" and not is_admin:
        return {"ok": False, "description": "Ты не участник этой сделки."}
    if action == "accept":
        deal.setdefault("confirmations", {})[f"{role}_terms"] = True
        if terms_confirmed(deal):
            deal["status"] = "waiting_payment"
        add_event(deal, role_title(role), "подтвердил условия")
    elif action == "mono_create" and role == "buyer":
        if not MONO_TOKEN:
            return {"ok": False, "description": "Monobank не настроен."}
        result = create_mono_invoice(deal)
        if not result["ok"]:
            return result
        add_event(deal, role_title(role), "создал счет Monobank")
    elif action == "mono_status":
        result = refresh_mono_status(deal)
        if not result["ok"]:
            return result
        add_event(deal, role_title(role), "проверил оплату")
    elif action in ("crypto_create_ton", "crypto_create_usdt") and role == "buyer":
        result = create_crypto_payment(deal, action.replace("crypto_create_", ""))
        if not result["ok"]:
            return result
    elif action == "crypto_proof" and role == "buyer":
        if not text:
            return {"ok": False, "description": "Добавь TX hash, ссылку или описание оплаты."}
        payment = deal.setdefault("payment", {})
        payment["tx_hash"] = text
        payment["status"] = "pending_manual"
        add_event(deal, role_title(role), "отправил доказательство crypto-оплаты", text)
        if ADMIN_CHAT_ID:
            send_message(ADMIN_CHAT_ID, "Покупатель отправил crypto-доказательство\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
    elif action == "ship" and role == "seller":
        if not text:
            return {"ok": False, "description": "Добавь результат, ссылку, ключ или описание передачи."}
        proof = {"type": "text", "text": text, "caption": seller_handoff_label(deal), "from": web_user_name(user), "at": int(time.time())}
        deal["handoff"] = proof
        deal.setdefault("proofs", []).append(proof)
        deal.setdefault("admin_reviews", {})["ship_requested"] = True
        deal["status"] = "shipping_review"
        add_event(deal, role_title(role), "передал результат на проверку")
        if ADMIN_CHAT_ID:
            send_message(ADMIN_CHAT_ID, "Нужна проверка передачи\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
    elif action == "receive" and role == "buyer":
        if not (deal.get("admin_reviews") or {}).get("receive_approved"):
            deal.setdefault("admin_reviews", {})["receive_requested"] = True
            deal["status"] = "receive_review"
            add_event(deal, role_title(role), "отправил принятие на проверку")
            if ADMIN_CHAT_ID:
                send_message(ADMIN_CHAT_ID, "Нужна проверка принятия результата\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
        else:
            finalize = finalize_mono_hold(deal) if (deal.get("payment") or {}).get("paymentType") == "hold" else {"ok": True}
            if not finalize["ok"]:
                return finalize
            deal["buyer_received"] = True
            deal["status"] = "released"
            credit = credit_seller_balance(deal)
            add_event(deal, role_title(role), "завершил сделку", format_uah(credit["net"]))
    elif action == "proof":
        if not text:
            return {"ok": False, "description": "Добавь текст доказательства."}
        deal.setdefault("proofs", []).append({"type": "text", "text": text, "from": web_user_name(user), "at": int(time.time())})
        add_event(deal, role_title(role), "добавил доказательство")
    elif action == "dispute":
        deal["status"] = "dispute"
        deal["dispute"] = {"reason": "mini_app", "reason_text": text or "Спор из Mini App", "opened_by": role, "opened_at": int(time.time())}
        add_event(deal, role_title(role), "открыл спор", text)
        if ADMIN_CHAT_ID:
            send_message(ADMIN_CHAT_ID, "Открыт спор\n\n" + render_admin_deal_review(deal), admin_deal_review_keyboard(deal_id))
    elif action == "rate_5":
        deal.setdefault("ratings", {})[role] = {"rating": 5, "from": web_user_name(user), "at": int(time.time())}
        add_event(deal, role_title(role), "оставил оценку 5/5")
    elif is_admin and action == "admin_approve_ship":
        deal.setdefault("admin_reviews", {})["ship_approved"] = True
        deal["seller_shipped"] = True
        deal["status"] = "delivery"
        add_event(deal, "admin", "разрешил передачу")
    elif is_admin and action == "admin_approve_receive":
        deal.setdefault("admin_reviews", {})["receive_approved"] = True
        add_event(deal, "admin", "разрешил принятие")
    elif is_admin and action == "admin_crypto_approve":
        payment = deal.setdefault("payment", {})
        payment["status"] = "success"
        deal["status"] = "paid"
        add_event(deal, "admin", "подтвердил crypto-оплату")
    elif is_admin and action == "admin_crypto_reject":
        deal.setdefault("payment_history", []).append(deal.get("payment") or {})
        deal["payment"] = {}
        deal["status"] = "payment_failed"
        add_event(deal, "admin", "отклонил crypto-оплату")
    elif is_admin and action == "admin_resolve_refund":
        deal["status"] = "refunded"
        deal["dispute_resolution"] = {"result": "refund", "at": int(time.time())}
        add_event(deal, "admin", "решил спор: возврат покупателю")
    elif is_admin and action == "admin_resolve_seller":
        credit = credit_seller_balance(deal)
        deal["status"] = "released"
        deal["dispute_resolution"] = {"result": "seller", "at": int(time.time())}
        add_event(deal, "admin", "решил спор: зачислить продавцу", format_uah(credit["net"]))
    else:
        return {"ok": False, "description": "Действие сейчас недоступно."}
    save_deals(deals)
    refresh_parties_deal(deal)
    return {"ok": True, "deal": web_serialize_deal(deal, user)}


def web_update_profile(user, payload):
    key = user_key(user)
    users = load_users()
    record = users.setdefault(key, {"name": web_user_name(user), "blocked": False, "created_deals": []})
    record["chat_id"] = int(user.get("id"))
    record["name"] = web_user_name(user)
    record["payout_details"] = str(payload.get("payoutDetails", "")).strip()
    users[key] = record
    save_users(users)
    return {"ok": True, "profile": web_profile(user)}


def web_create_withdrawal(user):
    result = create_profile_withdrawal_request(int(user.get("id")), user)
    if not result["ok"]:
        return result
    return {"ok": True, "withdrawal": result["withdrawal"], "profile": web_profile(user)}


def web_admin_withdrawals(user):
    if ADMIN_CHAT_ID != int(user.get("id")):
        return {"ok": False, "description": "Доступно только админу."}
    withdrawals = sorted(load_withdrawals().values(), key=lambda item: item.get("created_at", 0), reverse=True)
    return {"ok": True, "withdrawals": withdrawals[:100]}


def web_admin_withdrawal_action(user, withdrawal_id, payload):
    if ADMIN_CHAT_ID != int(user.get("id")):
        return {"ok": False, "description": "Доступно только админу."}
    withdrawals = load_withdrawals()
    withdrawal = withdrawals.get(withdrawal_id)
    if not withdrawal:
        return {"ok": False, "description": "Заявка не найдена."}
    action = payload.get("action")
    if action == "paid":
        withdrawal["status"] = "paid_manual"
        withdrawal["paid_at"] = int(time.time())
        withdrawal["admin_chat_id"] = int(user.get("id"))
        balances = load_balances()
        record = get_balance_record(balances, withdrawal["seller_key"])
        record["pending"] = max(0, record.get("pending", 0) - withdrawal.get("amount", 0))
        save_balances(balances)
        send_plain_notice(withdrawal.get("seller_chat_id"), f"Вывод {format_uah(withdrawal.get('amount', 0))} отмечен как выплаченный.")
    elif action == "reject":
        withdrawal["status"] = "rejected"
        withdrawal["rejected_at"] = int(time.time())
        balances = load_balances()
        record = get_balance_record(balances, withdrawal["seller_key"])
        amount = withdrawal.get("amount", 0)
        record["pending"] = max(0, record.get("pending", 0) - amount)
        record["available"] = record.get("available", 0) + amount
        save_balances(balances)
        send_plain_notice(withdrawal.get("seller_chat_id"), f"Заявка на вывод {format_uah(amount)} отклонена, сумма возвращена на баланс.")
    elif action == "receipt":
        withdrawal["receipt"] = str(payload.get("text", "")).strip()
        withdrawal["receipt_added_at"] = int(time.time())
    else:
        return {"ok": False, "description": "Неизвестное действие."}
    withdrawals[withdrawal_id] = withdrawal
    save_withdrawals(withdrawals)
    return {"ok": True, "withdrawal": withdrawal}


def serve_webapp_file(handler, path):
    if path in ("/app", "/app/"):
        target = WEBAPP_DIR / "index.html"
    else:
        relative = urllib.parse.unquote(path.removeprefix("/app/"))
        target = (WEBAPP_DIR / relative).resolve()
        if WEBAPP_DIR.resolve() not in target.parents and target != WEBAPP_DIR.resolve():
            handler.send_response(403)
            handler.end_headers()
            return
    if not target.exists() or not target.is_file():
        handler.send_response(404)
        handler.end_headers()
        return
    raw = target.read_bytes()
    handler.send_response(200)
    handler.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def handle_web_get(handler):
    path = urllib.parse.urlparse(handler.path).path
    if path.startswith("/app"):
        serve_webapp_file(handler, path)
        return
    user = web_user(handler)
    if not user:
        web_json(handler, 401, {"ok": False, "description": "Telegram Mini App auth failed."})
        return
    if path == "/api/me":
        web_json(handler, 200, {"ok": True, "profile": web_profile(user), "config": {"miniAppUrl": mini_app_url(), "monoEnabled": bool(MONO_TOKEN), "cryptoTon": bool(CRYPTO_TON_ADDRESS), "cryptoUsdt": bool(CRYPTO_USDT_ADDRESS)}})
        return
    if path == "/api/deals":
        web_json(handler, 200, {"ok": True, "deals": web_visible_deals(user)})
        return
    if path == "/api/admin/withdrawals":
        result = web_admin_withdrawals(user)
        web_json(handler, 200 if result.get("ok") else 403, result)
        return
    web_json(handler, 404, {"ok": False, "description": "Not found."})


def handle_web_post(handler):
    path = urllib.parse.urlparse(handler.path).path
    user = web_user(handler)
    if not user:
        web_json(handler, 401, {"ok": False, "description": "Telegram Mini App auth failed."})
        return
    try:
        payload = web_read_json(handler)
        if path == "/api/deals":
            result = web_create_deal_v2(user, payload)
            web_json(handler, 200 if result.get("ok") else 400, result)
            return
        if path.startswith("/api/deals/") and path.endswith("/join"):
            deal_id = path.split("/")[3]
            result = web_join_deal(user, deal_id)
            web_json(handler, 200 if result.get("ok") else 400, result)
            return
        if path.startswith("/api/deals/") and path.endswith("/action"):
            deal_id = path.split("/")[3]
            result = web_apply_deal_action(user, deal_id, payload)
            web_json(handler, 200 if result.get("ok") else 400, result)
            return
        if path == "/api/profile":
            web_json(handler, 200, web_update_profile(user, payload))
            return
        if path == "/api/withdrawals":
            result = web_create_withdrawal(user)
            web_json(handler, 200 if result.get("ok") else 400, result)
            return
        if path.startswith("/api/withdrawals/") and path.endswith("/action"):
            withdrawal_id = path.split("/")[3]
            result = web_admin_withdrawal_action(user, withdrawal_id, payload)
            web_json(handler, 200 if result.get("ok") else 400, result)
            return
        web_json(handler, 404, {"ok": False, "description": "Not found."})
    except Exception as error:
        web_json(handler, 500, {"ok": False, "description": str(error)})


def process_mono_webhook(payload):
    invoice_id = payload.get("invoiceId")
    reference = payload.get("reference") or payload.get("merchantPaymInfo", {}).get("reference")
    deals = load_deals()
    target_id = None
    for deal_id, deal in deals.items():
        payment = deal.get("payment") or {}
        if payment.get("invoiceId") == invoice_id or deal_id == reference:
            target_id = deal_id
            break
    if not target_id:
        return False
    deal = deals[target_id]
    apply_mono_payment_update(deal, payload)
    save_deals(deals)
    refresh_parties_deal(deal)
    return True


class MonoWebhookHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Telegram-Init-Data")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        handle_web_get(self)

    def do_POST(self):
        if self.path.startswith("/api/"):
            handle_web_post(self)
            return
        expected_path = f"/webhooks/monobank/{MONO_WEBHOOK_SECRET}"
        if self.path != expected_path:
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
            found = process_mono_webhook(payload)
            self.send_response(200 if found else 202)
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as error:
            print(f"Webhook error: {error}")
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"bad request")

    def log_message(self, format, *args):
        return


def start_webhook_server():
    if not PUBLIC_BASE_URL and not MINI_APP_DEV_MODE:
        print("PUBLIC_BASE_URL is not set, Monobank webhooks and Mini App hosting are disabled.")
        return
    server = HTTPServer(("0.0.0.0", WEBHOOK_PORT), MonoWebhookHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Web server listens on port {WEBHOOK_PORT}.")
    print(f"Mini App URL: {mini_app_url() or f'http://localhost:{WEBHOOK_PORT}/app/'}")
    if PUBLIC_BASE_URL:
        print(f"Webhook URL: {mono_webhook_url()}")


def poll():
    global BOT_USERNAME
    offset = None
    last_auto_cleanup = 0
    bot = get_bot_identity()
    BOT_USERNAME = bot.get("username", "")
    setup_bot_commands()
    start_webhook_server()
    print(f"@{BOT_USERNAME or 'unknown_bot'} is running. Press Ctrl+C to stop.")
    while True:
        if int(time.time()) - last_auto_cleanup >= 60:
            deleted = run_auto_delete_expired_deals()
            if deleted:
                print(f"Auto-deleted expired deals: {deleted}")
            run_deal_reminders()
            last_auto_cleanup = int(time.time())
        payload = {"timeout": 30}
        if offset:
            payload["offset"] = offset
        try:
            result = safe_api("getUpdates", payload)
            if not result.get("ok", True):
                print(f"Polling error: {result.get('error_code')} {result.get('description')}")
                time.sleep(3)
                continue
            for update in result.get("result", []):
                offset = update["update_id"] + 1
                if "callback_query" in update:
                    handle_callback(update["callback_query"])
                elif "message" in update:
                    handle_message(update["message"])
        except KeyboardInterrupt:
            raise
        except Exception as error:
            print(f"Polling error: {error}")
            time.sleep(3)


def run_mini_app_server():
    last_auto_cleanup = 0
    start_webhook_server()
    print("Mini App server is running. Press Ctrl+C to stop.")
    while True:
        if int(time.time()) - last_auto_cleanup >= 60:
            deleted = run_auto_delete_expired_deals()
            if deleted:
                print(f"Auto-deleted expired deals: {deleted}")
            last_auto_cleanup = int(time.time())
        time.sleep(1)


if __name__ == "__main__":
    run_mini_app_server()
