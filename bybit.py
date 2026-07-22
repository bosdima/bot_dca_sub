"""
Telegram бот для работы с Bybit API (субаккаунт)
С подробным логированием ошибок для отправки в поддержку Bybit.

Автор: Qwen3.7
Дата: 2026-07-21
"""
import os
import sys
import time
import json
import hmac
import hashlib
import logging
import traceback
import requests
from datetime import datetime
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple

from dotenv import load_dotenv
import telebot
from telebot import types
from pybit.unified_trading import HTTP
from pybit.exceptions import FailedRequestError, InvalidRequestError

# ============================================================
# 1. ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ============================================================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BYBIT_SUBACCOUNT_UID = os.getenv("BYBIT_SUBACCOUNT_UID", "").strip()

if not TELEGRAM_BOT_TOKEN:
    sys.exit("❌ TELEGRAM_BOT_TOKEN не задан в .env")
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    sys.exit("❌ BYBIT_API_KEY или BYBIT_API_SECRET не заданы в .env")

# ============================================================
# 2. ПОДРОБНОЕ ЛОГИРОВАНИЕ
# ============================================================
LOG_FILE = "bybit_bot.log"
ERROR_REPORT_FILE = "error_report.txt"  # файл для отправки в поддержку

log_formatter = logging.Formatter(
    fmt="[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_formatter)

logger = logging.getLogger("BybitBot")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

logger.info("=" * 60)
logger.info("🚀 Запуск Telegram бота для Bybit")
logger.info("=" * 60)
logger.info(f"Bybit API Key (первые 8 символов): {BYBIT_API_KEY[:8]}...")
logger.info(f"Bybit Subaccount UID: {BYBIT_SUBACCOUNT_UID or '(не задан)'}")


# ============================================================
# 3. НИЗКОУРОВНЕВЫЙ HTTP КЛИЕНТ ДЛЯ ПОДРОБНОГО ЛОГИРОВАНИЯ
# ============================================================
class DetailedBybitClient:
    """
    Обёртка над requests, которая логирует ВСЁ:
    - URL, method, headers, body запроса
    - Статус код, headers, body ответа
    - Время выполнения
    - Подпись (timestamp + recv_window + querystring)
    """
    
    BASE_URL = "https://api.bybit.com"
    
    def __init__(self, api_key: str, api_secret: str, subaccount_uid: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.subaccount_uid = subaccount_uid
        self.session = requests.Session()
    
    def _generate_signature(self, timestamp: str, recv_window: str, query_string: str) -> str:
        """Генерация HMAC SHA256 подписи."""
        sign_str = f"{timestamp}{self.api_key}{recv_window}{query_string}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature
    
    def request(self, method: str, endpoint: str, params: Optional[dict] = None, body: Optional[dict] = None) -> dict:
        """
        Выполняет HTTP запрос с ПОДРОБНЫМ логированием.
        Возвращает полный ответ для отчёта в поддержку.
        """
        url = f"{self.BASE_URL}{endpoint}"
        timestamp = str(int(time.time() * 1000))
        recv_window = "10000"
        
        # Формируем заголовки
        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "Content-Type": "application/json"
        }
        
        # ✅ ВАЖНО: субаккаунт передаётся через ЗАГОЛОВОК, а не в теле!
        if self.subaccount_uid:
            headers["X-BAPI-SUB-ACCOUNT-UID"] = self.subaccount_uid
        
        # Формируем тело/параметры
        if method.upper() == "GET":
            query_string = "&".join([f"{k}={v}" for k, v in sorted((params or {}).items())])
            body_json = None
        else:
            query_string = json.dumps(body or {}, separators=(",", ":"), sort_keys=True)
            body_json = body
        
        # Подпись
        signature = self._generate_signature(timestamp, recv_window, query_string)
        headers["X-BAPI-SIGN"] = signature
        
        # === ПОДРОБНОЕ ЛОГИРОВАНИЕ ЗАПРОСА ===
        logger.info("=" * 60)
        logger.info(f"📤 HTTP ЗАПРОС → {method} {url}")
        logger.info(f"📅 Timestamp: {timestamp}")
        logger.info(f"🔐 Recv Window: {recv_window}")
        logger.info(f"🔑 API Key: {self.api_key[:8]}...")
        if self.subaccount_uid:
            logger.info(f"🔀 Subaccount UID (в заголовке): {self.subaccount_uid}")
        logger.info(f"📝 Query/Body: {query_string}")
        logger.info(f"✍️  Signature: {signature}")
        logger.info(f"📋 Headers: {json.dumps({k: (v[:20] + '...' if len(str(v)) > 20 else v) for k, v in headers.items()}, indent=2)}")
        
        # Выполняем запрос
        start_time = time.time()
        try:
            if method.upper() == "GET":
                response = self.session.get(url, headers=headers, params=params, timeout=10)
            else:
                response = self.session.post(url, headers=headers, json=body, timeout=10)
            
            elapsed = time.time() - start_time
            
            # === ПОДРОБНОЕ ЛОГИРОВАНИЕ ОТВЕТА ===
            logger.info(f"📥 HTTP ОТВЕТ ← {response.status_code} ({elapsed:.3f}s)")
            logger.info(f"📋 Response Headers: {dict(response.headers)}")
            
            try:
                response_json = response.json()
                logger.info(f"📦 Response Body: {json.dumps(response_json, indent=2)}")
            except Exception:
                logger.info(f"📦 Response Body (raw): {response.text[:1000]}")
                response_json = {"raw": response.text}
            
            logger.info("=" * 60)
            
            # Сохраняем полный отчёт
            report = {
                "timestamp": datetime.now().isoformat(),
                "request": {
                    "method": method,
                    "url": url,
                    "timestamp": timestamp,
                    "recv_window": recv_window,
                    "api_key_prefix": self.api_key[:8] + "...",
                    "subaccount_uid": self.subaccount_uid or None,
                    "query_or_body": query_string,
                    "signature": signature,
                    "headers": {k: v for k, v in headers.items() if k != "X-BAPI-SIGN"}
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                    "body": response_json,
                    "elapsed_seconds": round(elapsed, 3)
                }
            }
            
            # Сохраняем последний отчёт в файл
            with open(ERROR_REPORT_FILE, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
            
            return response_json
            
        except requests.exceptions.RequestException as e:
            elapsed = time.time() - start_time
            logger.error(f"❌ Сетевая ошибка: {e} ({elapsed:.3f}s)")
            logger.error(traceback.format_exc())
            raise


# ============================================================
# 4. ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ
# ============================================================
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
logger.info("✅ Telegram бот инициализирован")

# Низкоуровневый клиент для подробного логирования
detailed_client = DetailedBybitClient(
    api_key=BYBIT_API_KEY,
    api_secret=BYBIT_API_SECRET,
    subaccount_uid=BYBIT_SUBACCOUNT_UID
)

# Высокоуровневый pybit клиент (для удобства)
try:
    bybit_session = HTTP(
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_API_SECRET,
        testnet=False,
        recv_window=10000,
        logging_level=logging.WARNING
    )
    logger.info("✅ Bybit HTTP клиент инициализирован")
except Exception as e:
    logger.critical(f"❌ Ошибка инициализации Bybit: {e}")
    sys.exit(1)


# ============================================================
# 5. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def send_error_to_user(chat_id: int, error: Exception, context: str = "") -> None:
    error_text = f"❌ Ошибка{f' ({context})' if context else ''}:\n`{str(error)[:500]}`"
    try:
        bot.send_message(chat_id, error_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение: {e}")


def format_error_for_support(error: Exception, request_info: dict = None) -> str:
    """
    Форматирует ошибку для отправки в поддержку Bybit.
    Включает ВСЮ необходимую информацию для диагностики.
    """
    report = []
    report.append("=" * 60)
    report.append("📋 ОТЧЁТ ОБ ОШИБКЕ ДЛЯ ПОДДЕРЖКИ BYBIT")
    report.append("=" * 60)
    report.append(f"📅 Дата/время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append(f"🔑 API Key (первые 8 символов): {BYBIT_API_KEY[:8]}...")
    report.append(f"🔀 Subaccount UID: {BYBIT_SUBACCOUNT_UID or '(не используется)'}")
    report.append("")
    
    report.append("🐍 ИНФОРМАЦИЯ О КЛИЕНТЕ:")
    report.append(f"  • Python версия: {sys.version}")
    report.append(f"  • pybit версия: {__import__('pybit').__version__ if hasattr(__import__('pybit'), '__version__') else 'unknown'}")
    report.append(f"  • OS: {sys.platform}")
    report.append("")
    
    report.append("❌ ТЕКСТ ОШИБКИ:")
    report.append(f"  {type(error).__name__}: {str(error)}")
    report.append("")
    
    if request_info:
        report.append("📤 ДЕТАЛИ ЗАПРОСА:")
        for k, v in request_info.items():
            report.append(f"  • {k}: {v}")
        report.append("")
    
    # Пытаемся прочитать последний отчёт из файла
    try:
        if os.path.exists(ERROR_REPORT_FILE):
            with open(ERROR_REPORT_FILE, "r", encoding="utf-8") as f:
                last_report = json.load(f)
            
            report.append("📡 ПОСЛЕДНИЙ HTTP ЗАПРОС:")
            req = last_report.get("request", {})
            report.append(f"  • URL: {req.get('url')}")
            report.append(f"  • Method: {req.get('method')}")
            report.append(f"  • Timestamp: {req.get('timestamp')}")
            report.append(f"  • Recv Window: {req.get('recv_window')}")
            report.append(f"  • API Key: {req.get('api_key_prefix')}")
            report.append(f"  • Subaccount UID: {req.get('subaccount_uid')}")
            report.append(f"  • Body/Query: {req.get('query_or_body')}")
            report.append(f"  • Signature: {req.get('signature')}")
            report.append("")
            
            report.append("📥 ПОСЛЕДНИЙ HTTP ОТВЕТ:")
            resp = last_report.get("response", {})
            report.append(f"  • Status Code: {resp.get('status_code')}")
            report.append(f"  • Elapsed: {resp.get('elapsed_seconds')}s")
            report.append(f"  • Body: {json.dumps(resp.get('body'), indent=2, ensure_ascii=False)}")
            report.append("")
    except Exception as e:
        report.append(f"⚠️ Не удалось прочитать отчёт: {e}")
        report.append("")
    
    report.append("=" * 60)
    report.append("💡 ПОЖАЛУЙСТА, ОПИШИТЕ:")
    report.append("  1. Что вы пытались сделать?")
    report.append("  2. Когда это произошло?")
    report.append("  3. Повторяется ли ошибка?")
    report.append("=" * 60)
    
    return "\n".join(report)


def get_market_price(symbol: str = "BTCUSDT") -> Tuple[float, str]:
    logger.info(f"📊 Запрос рыночной цены для {symbol}")
    
    for category in ["spot", "linear"]:
        try:
            response = bybit_session.get_tickers(category=category, symbol=symbol)
            logger.debug(f"Ответ get_tickers [{category}]: {response}")
            
            if response.get("retCode") == 0 and response.get("result", {}).get("list"):
                price = float(response["result"]["list"][0]["lastPrice"])
                logger.info(f"✅ Рыночная цена {symbol} ({category}): {price}")
                return price, category
        except Exception as e:
            logger.warning(f"Не удалось получить цену в {category}: {e}")
            continue
    
    raise RuntimeError(f"Не удалось получить рыночную цену для {symbol}")


def get_usdt_balance() -> Tuple[float, float]:
    logger.info("💰 Запрос баланса USDT")
    
    try:
        response = bybit_session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        logger.debug(f"Ответ get_wallet_balance: {response}")
        
        if response.get("retCode") != 0:
            raise RuntimeError(f"Bybit: {response.get('retMsg')}")
        
        result_list = response.get("result", {}).get("list", [])
        if not result_list:
            raise RuntimeError("Пустой результат баланса")
        
        coin_list = result_list[0].get("coin", [])
        usdt_data = next((c for c in coin_list if c.get("coin") == "USDT"), None)
        
        if not usdt_data:
            raise RuntimeError("USDT не найден")
        
        available_raw = usdt_data.get("availableToWithdraw") or usdt_data.get("walletBalance", 0)
        available = float(available_raw)
        total = float(usdt_data.get("walletBalance", 0))
        
        logger.info(f"✅ Баланс USDT: доступно={available}, всего={total}")
        return available, total
        
    except (FailedRequestError, InvalidRequestError) as e:
        logger.error(f"Ошибка Bybit API: {e}")
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка: {e}")
        logger.error(traceback.format_exc())
        raise


def place_order_via_detailed_client(
    symbol: str = "BTCUSDT",
    side: str = "Buy",
    usdt_amount: float = 10.0,
    price: Optional[float] = None,
    category: str = "spot"
) -> dict:
    """
    Выставляет ордер через низкоуровневый клиент с ПОДРОБНЫМ логированием.
    Это позволит увидеть ВСЁ для отправки в поддержку.
    """
    logger.info(f"📝 Выставление ордера: {side} {symbol}, {usdt_amount} USDT, категория={category}")
    
    # 1. Получаем рыночную цену
    if price is None:
        market_price, category = get_market_price(symbol)
        price = market_price * 0.9  # на 10% ниже рынка
        logger.info(f"💡 Цена ордера: {price} (рынок {market_price} - 10%)")
    
    # 2. Получаем информацию об инструменте
    instruments_response = bybit_session.get_instruments_info(category=category, symbol=symbol)
    logger.debug(f"Ответ get_instruments_info: {instruments_response}")
    
    if instruments_response.get("retCode") != 0:
        raise RuntimeError(f"Ошибка инструмента: {instruments_response.get('retMsg')}")
    
    instrument = instruments_response["result"]["list"][0]
    lot_filter = instrument["lotSizeFilter"]
    
    # Для SPOT - basePrecision, для фьючерсов - qtyStep
    if category == "spot":
        lot_size = float(lot_filter.get("basePrecision", "0.000001"))
        min_qty = float(lot_filter.get("minOrderQty", "0"))
        min_amt = float(lot_filter.get("minOrderAmt", "0"))
    else:
        lot_size = float(lot_filter.get("qtyStep", "0.000001"))
        min_qty = float(lot_filter.get("minOrderQty", "0"))
        min_amt = 0
    
    tick_size = float(instrument["priceFilter"]["tickSize"])
    
    logger.info(f"📏 Параметры: lot_step={lot_size}, min_qty={min_qty}, min_amt={min_amt}, tick_size={tick_size}")
    
    # 3. Вычисляем количество
    raw_qty = usdt_amount / price
    qty = float(Decimal(str(raw_qty)).quantize(Decimal(str(lot_size)), rounding=ROUND_DOWN))
    price = float(Decimal(str(price)).quantize(Decimal(str(tick_size)), rounding=ROUND_DOWN))
    
    logger.info(f"🧮 Расчёт: raw_qty={raw_qty}, qty={qty}, price={price}")
    
    # 4. Проверки
    if category == "spot" and min_amt > 0 and usdt_amount < min_amt:
        raise ValueError(f"Сумма {usdt_amount} USDT < минимальной {min_amt} USDT")
    
    if qty < min_qty:
        raise ValueError(f"Количество {qty} < минимального {min_qty}")
    
    # 5. Выставляем ордер через низкоуровневый клиент (с подробным логом)
    body = {
        "category": category,
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": str(qty),
        "price": str(price),
        "timeInForce": "GTC"
    }
    
    # ✅ НЕ добавляем subaccountId в тело! Он передаётся через заголовок.
    
    request_info = {
        "endpoint": "/v5/order/create",
        "method": "POST",
        "body": json.dumps(body, indent=2),
        "subaccount_in_header": bool(BYBIT_SUBACCOUNT_UID)
    }
    
    try:
        response = detailed_client.request("POST", "/v5/order/create", body=body)
        
        if response.get("retCode") != 0:
            err_code = response.get("retCode")
            err_msg = response.get("retMsg", "unknown")
            
            error_text = format_error_for_support(
                RuntimeError(f"Bybit retCode={err_code}: {err_msg}"),
                request_info
            )
            
            # Сохраняем полный отчёт
            with open("last_error_report.txt", "w", encoding="utf-8") as f:
                f.write(error_text)
            
            logger.error(f"❌ Ошибка Bybit:\n{error_text}")
            raise RuntimeError(error_text)
        
        logger.info(f"✅ Ордер успешно выставлен: {response}")
        return response
        
    except Exception as e:
        error_text = format_error_for_support(e, request_info)
        with open("last_error_report.txt", "w", encoding="utf-8") as f:
            f.write(error_text)
        logger.error(f"❌ Ошибка:\n{error_text}")
        raise


# ============================================================
# 6. ОБРАБОТЧИКИ TELEGRAM
# ============================================================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message) -> None:
    chat_id = message.chat.id
    logger.info(f"👤 /start от {chat_id}")
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💰 Показать баланс USDT", callback_data="show_balance"),
        types.InlineKeyboardButton("📉 Купить BTC (−10% от рынка)", callback_data="place_buy"),
        types.InlineKeyboardButton("📈 Продать BTC (+10% от рынка)", callback_data="place_sell"),
        types.InlineKeyboardButton("📋 Получить отчёт об ошибке", callback_data="get_error_report"),
        types.InlineKeyboardButton("ℹ️ Информация об аккаунте", callback_data="account_info")
    )
    
    bot.send_message(
        chat_id,
        "🤖 <b>Bybit Trading Bot</b>\n\n"
        "Я работаю с Bybit API (V5 Unified Trading).\n"
        f"{'🔀 Режим: СУБАККАУНТ (UID: ' + BYBIT_SUBACCOUNT_UID + ')' if BYBIT_SUBACCOUNT_UID else '🏠 Режим: ОСНОВНОЙ АККАУНТ'}\n\n"
        "Выберите действие:",
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data == "show_balance")
def callback_show_balance(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    logger.info(f"🔘 Кнопка 'Баланс' (user={chat_id})")
    bot.answer_callback_query(call.id, "⏳ Загружаю баланс...")
    
    try:
        available, total = get_usdt_balance()
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=f"💰 <b>Баланс USDT</b>\n\n"
                 f"📊 Доступно: <code>{available:.4f}</code> USDT\n"
                 f"💼 Всего: <code>{total:.4f}</code> USDT\n\n"
                 f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("Ошибка при получении баланса")
        send_error_to_user(chat_id, e, "получение баланса")


@bot.callback_query_handler(func=lambda call: call.data in ["place_buy", "place_sell"])
def callback_place_order(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    side = "Buy" if call.data == "place_buy" else "Sell"
    logger.info(f"🔘 Кнопка '{side}' (user={chat_id})")
    bot.answer_callback_query(call.id, f"⏳ Выставляю ордер на {side}...")
    
    try:
        available, total = get_usdt_balance()
        
        if available < 10:
            raise ValueError(f"Недостаточно средств: {available:.2f} USDT")
        
        usdt_amount = min(10.0, available * 0.1)
        usdt_amount = round(usdt_amount, 2)
        logger.info(f"Сумма для ордера: {usdt_amount} USDT")
        
        response = place_order_via_detailed_client(
            symbol="BTCUSDT",
            side=side,
            usdt_amount=usdt_amount
        )
        
        order_id = response["result"]["orderId"]
        
        success_text = (
            f"✅ <b>Ордер {side} выставлен!</b>\n\n"
            f"📋 ID: <code>{order_id}</code>\n"
            f"💵 Сумма: <code>{usdt_amount}</code> USDT\n"
            f"📉 Тип: Limit {side} (±10% от рынка)\n"
            f"🎯 Пара: BTCUSDT\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=success_text,
            parse_mode="HTML"
        )
        logger.info(f"✅ Ордер {order_id} выставлен")
        
    except Exception as e:
        logger.exception("Ошибка при выставлении ордера")
        error_msg = str(e)
        
        # Отправляем ПОДРОБНЫЙ отчёт в Telegram (разбиваем на части)
        try:
            # Читаем отчёт из файла
            if os.path.exists("last_error_report.txt"):
                with open("last_error_report.txt", "r", encoding="utf-8") as f:
                    report = f.read()
            else:
                report = format_error_for_support(e)
            
            # Разбиваем на части по 4000 символов (лимит Telegram)
            parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
            
            # Отправляем первое сообщение как edit
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=f"❌ <b>Ошибка при выставлении ордера</b>\n\n"
                     f"Подробный отчёт в следующих сообщениях ↓",
                parse_mode="HTML"
            )
            
            # Отправляем остальные части
            for i, part in enumerate(parts, 1):
                bot.send_message(
                    chat_id,
                    f"📋 <b>Отчёт часть {i}/{len(parts)}</b>\n\n"
                    f"<pre>{part}</pre>",
                    parse_mode="HTML"
                )
            
            bot.send_message(
                chat_id,
                "💡 <b>Что делать:</b>\n"
                "1. Скопируйте отчёт выше\n"
                "2. Отправьте в поддержку Bybit\n"
                "3. Укажите, что вы пытались сделать\n"
                "4. Приложите скриншот из Bybit"
            )
            
        except Exception as send_err:
            logger.error(f"Не удалось отправить отчёт: {send_err}")
            bot.send_message(
                chat_id,
                f"❌ Ошибка: {error_msg[:400]}\n\n"
                f"Полный отчёт сохранён в файл <code>last_error_report.txt</code>",
                parse_mode="HTML"
            )


@bot.callback_query_handler(func=lambda call: call.data == "get_error_report")
def callback_get_error_report(call: types.CallbackQuery) -> None:
    """Отправляет последний отчёт об ошибке."""
    chat_id = call.message.chat.id
    logger.info(f"🔘 Кнопка 'Отчёт об ошибке' (user={chat_id})")
    
    try:
        if os.path.exists("last_error_report.txt"):
            with open("last_error_report.txt", "r", encoding="utf-8") as f:
                report = f.read()
            
            parts = [report[i:i+4000] for i in range(0, len(report), 4000)]
            
            for i, part in enumerate(parts, 1):
                bot.send_message(
                    chat_id,
                    f"📋 <b>Отчёт часть {i}/{len(parts)}</b>\n\n"
                    f"<pre>{part}</pre>",
                    parse_mode="HTML"
                )
        else:
            bot.send_message(chat_id, "ℹ️ Отчётов об ошибках пока нет.")
    except Exception as e:
        logger.exception("Ошибка при отправке отчёта")
        send_error_to_user(chat_id, e, "отчёт об ошибке")


@bot.callback_query_handler(func=lambda call: call.data == "account_info")
def callback_account_info(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    logger.info(f"🔘 Кнопка 'Инфо' (user={chat_id})")
    bot.answer_callback_query(call.id, "⏳ Загружаю...")
    
    try:
        info_text = (
            f"ℹ️ <b>Информация об аккаунте</b>\n\n"
            f"🔑 API Key: <code>{BYBIT_API_KEY[:8]}...{BYBIT_API_KEY[-4:]}</code>\n"
            f"{'🔀 Субаккаунт UID: <code>' + BYBIT_SUBACCOUNT_UID + '</code>' if BYBIT_SUBACCOUNT_UID else '🏠 Основной аккаунт'}\n"
            f"🌐 Сеть: <b>Mainnet</b>\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=info_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("Ошибка")
        send_error_to_user(chat_id, e, "информация")


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_unknown(message: types.Message) -> None:
    bot.reply_to(message, "Используйте /start")


def main() -> None:
    logger.info("🎯 Бот готов к работе")
    
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            logger.error(f"❌ Ошибка polling: {e}")
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Остановлен")