"""
Telegram бот для проверки способности выставлять ордера на Bybit API (V5)
Использует официальную HMAC-SHA256 подпись согласно документации Bybit.
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
from urllib.parse import urlencode
from dotenv import load_dotenv
import telebot
from telebot import types

# ============================================================
# ⚙️ НАСТРОЙКИ БОТА (МЕНЯТЬ ЗДЕСЬ)
# ============================================================
TRADING_SYMBOL = "BTCUSDT"
CATEGORY = "spot"
MIN_ORDER_AMOUNT = 5.0
TARGET_ORDER_AMOUNT = 10.0
PRICE_OFFSET_PERCENT = 10

# ============================================================
# 1. ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ============================================================
load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "").strip()
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "").strip()
BYBIT_SUBACCOUNT_UID = os.getenv("BYBIT_SUBACCOUNT_UID", "").strip()

if not TELEGRAM_BOT_TOKEN:
    sys.exit("❌ TELEGRAM_BOT_TOKEN не задан в .env")
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    sys.exit("❌ BYBIT_API_KEY или BYBIT_API_SECRET не заданы в .env")

# ============================================================
# 2. ЛОГИРОВАНИЕ
# ============================================================
LOG_FILE = "bybit_bot.log"
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
logger.info("🚀 Запуск Telegram бота для Bybit (V5 Official Signature)")
logger.info("=" * 60)
logger.info(f"📊 Торговая пара: {TRADING_SYMBOL} ({CATEGORY})")
logger.info(f"💰 Мин. сумма ордера: {MIN_ORDER_AMOUNT} USDT")
logger.info(f"💵 Желаемая сумма: {TARGET_ORDER_AMOUNT} USDT")
logger.info(f"📉 Отступ от рынка: {PRICE_OFFSET_PERCENT}%")
logger.info(f"🔑 Bybit API Key: {BYBIT_API_KEY[:8]}...")
logger.info(f"🔀 Subaccount UID: {BYBIT_SUBACCOUNT_UID or '(не задан)'}")

# ============================================================
# 3. ОФИЦИАЛЬНЫЙ КЛИЕНТ BYBIT V5 (HMAC-SHA256)
# ============================================================
class BybitV5Client:
    """Клиент с официальной подписью Bybit V5"""
    def __init__(self, api_key: str, api_secret: str, subaccount_uid: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.subaccount_uid = subaccount_uid
        self.base_url = "https://api.bybit.com"
        self.session = requests.Session()

    def _generate_signature(self, timestamp: str, recv_window: str, query_string: str) -> str:
        """Официальный алгоритм подписи Bybit V5"""
        param_str = f"{timestamp}{self.api_key}{recv_window}{query_string}"
        signature = hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    def request(self, method: str, endpoint: str, payload: Optional[dict] = None) -> dict:
        """HTTP запрос с официальной подписью"""
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"

        if payload and method.upper() == "POST":
            query_string = json.dumps(payload)
        elif payload and method.upper() == "GET":
            query_string = urlencode(payload)
        else:
            query_string = ""

        signature = self._generate_signature(timestamp, recv_window, query_string)

        headers = {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN": signature,
            "Content-Type": "application/json"
        }

        if self.subaccount_uid:
            headers["X-BAPI-SUB-ACCOUNT-UID"] = self.subaccount_uid

        url = f"{self.base_url}{endpoint}"
        logger.debug(f"📤 {method} {endpoint}")
        logger.debug(f"🔑 Строка для подписи: {timestamp}{self.api_key}{recv_window}{query_string}")

        try:
            if method.upper() == "GET":
                response = self.session.get(url, headers=headers, params=payload, timeout=10)
            else:
                response = self.session.post(url, headers=headers, data=query_string, timeout=10)

            response.raise_for_status()
            result = response.json()
            logger.debug(f"📥 Ответ: {json.dumps(result, ensure_ascii=False)}")

            if result.get("retCode") != 0:
                error_msg = f"Bybit Error {result.get('retCode')}: {result.get('retMsg')}"
                logger.error(f"❌ {error_msg}")
                raise RuntimeError(error_msg)
            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Сетевая ошибка: {e}")
            logger.error(f"Ответ сервера: {e.response.text if e.response else 'No response'}")
            raise


bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
bybit = BybitV5Client(BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_SUBACCOUNT_UID)
logger.info("✅ Клиенты инициализированы")

# ============================================================
# 4. БИЗНЕС-ЛОГИКА
# ============================================================
def get_market_price(symbol: str = TRADING_SYMBOL) -> Tuple[float, str]:
    """Получает текущую рыночную цену"""
    logger.info(f"📊 Запрос цены для {symbol}")
    categories = [CATEGORY] + (["spot", "linear"] if CATEGORY not in ["spot", "linear"] else [])
    categories = list(dict.fromkeys(categories))

    for category in categories:
        try:
            response = bybit.request("GET", "/v5/market/tickers", {
                "category": category,
                "symbol": symbol
            })
            if response.get("result", {}).get("list"):
                price = float(response["result"]["list"][0]["lastPrice"])
                logger.info(f"✅ Цена {symbol} ({category}): {price}")
                return price, category
        except Exception as e:
            logger.warning(f"Не удалось получить цену в {category}: {e}")
            continue
    raise RuntimeError(f"Не удалось получить цену для {symbol}")


def get_instrument_info(symbol: str = TRADING_SYMBOL, category: str = CATEGORY) -> dict:
    """Получает информацию об инструменте"""
    logger.info(f"🔍 Запрос информации об инструменте {symbol}")
    response = bybit.request("GET", "/v5/market/instruments-info", {
        "category": category,
        "symbol": symbol
    })
    if not response.get("result", {}).get("list"):
        raise RuntimeError(f"Инструмент {symbol} не найден")
    return response["result"]["list"][0]


def get_usdt_balance() -> float:
    """Получает доступный баланс USDT"""
    logger.info("💰 Запрос баланса USDT")
    response = bybit.request("GET", "/v5/account/wallet-balance", {
        "accountType": "UNIFIED",
        "coin": "USDT"
    })
    coin_list = response["result"]["list"][0].get("coin", [])
    usdt_data = next((c for c in coin_list if c.get("coin") == "USDT"), None)
    if not usdt_data:
        raise RuntimeError("USDT не найден в балансе")
    available = float(usdt_data.get("availableToWithdraw") or usdt_data.get("walletBalance", 0))
    logger.info(f"✅ Доступный баланс USDT: {available}")
    return available


def get_coin_balance(coin: str) -> float:
    """Получает доступный баланс конкретной монеты"""
    logger.info(f"💰 Запрос баланса {coin}")
    response = bybit.request("GET", "/v5/account/wallet-balance", {
        "accountType": "UNIFIED",
        "coin": coin
    })
    coin_list = response["result"]["list"][0].get("coin", [])
    coin_data = next((c for c in coin_list if c.get("coin") == coin), None)
    if not coin_data:
        return 0.0
    available = float(coin_data.get("availableToWithdraw") or coin_data.get("walletBalance", 0))
    logger.info(f"✅ Доступный баланс {coin}: {available}")
    return available


def place_limit_order(side: str, usdt_amount: Optional[float] = None, coin_amount: Optional[float] = None) -> dict:
    """Выставляет лимитный ордер на ±PRICE_OFFSET_PERCENT% от рынка."""
    symbol = TRADING_SYMBOL
    category = CATEGORY
    logger.info(f"📝 Подготовка ордера: {side} {symbol}")

    market_price, category = get_market_price(symbol)

    if side == "Buy":
        target_price = market_price * (1 - PRICE_OFFSET_PERCENT / 100)
    else:
        target_price = market_price * (1 + PRICE_OFFSET_PERCENT / 100)

    logger.info(f"💡 Целевая цена: {target_price:.4f} (рынок: {market_price}, отступ: {PRICE_OFFSET_PERCENT}%)")

    inst = get_instrument_info(symbol, category)
    lot_filter = inst["lotSizeFilter"]

    if category == "spot":
        lot_size = float(lot_filter.get("basePrecision", "0.000001"))
        min_qty = float(lot_filter.get("minOrderQty", "0"))
        min_amt_api = float(lot_filter.get("minOrderAmt", "0"))
    else:
        lot_size = float(lot_filter.get("qtyStep", "0.000001"))
        min_qty = float(lot_filter.get("minOrderQty", "0"))
        min_amt_api = 0

    tick_size = float(inst["priceFilter"]["tickSize"])
    min_amt = max(MIN_ORDER_AMOUNT, min_amt_api)

    logger.info(f"📏 Параметры: lot_step={lot_size}, min_qty={min_qty}, min_amt={min_amt}, tick_size={tick_size}")

    if side == "Buy":
        if usdt_amount is None:
            raise ValueError("Для покупки нужна сумма в USDT")
        raw_qty = usdt_amount / target_price
        if category == "spot" and min_amt > 0 and usdt_amount < min_amt:
            raise ValueError(f"Сумма {usdt_amount} USDT меньше минимальной {min_amt} USDT")
    else:
        if coin_amount is None:
            raise ValueError("Для продажи нужно количество монеты")
        raw_qty = coin_amount

    qty = float(Decimal(str(raw_qty)).quantize(Decimal(str(lot_size)), rounding=ROUND_DOWN))
    price = float(Decimal(str(target_price)).quantize(Decimal(str(tick_size)), rounding=ROUND_DOWN))

    logger.info(f"🧮 Расчёт: raw_qty={raw_qty}, qty={qty}, price={price}")

    if qty < min_qty:
        raise ValueError(f"Количество {qty} меньше минимального {min_qty}. Увеличьте сумму ордера.")

    payload = {
        "category": category,
        "symbol": symbol,
        "side": side,
        "orderType": "Limit",
        "qty": str(qty),
        "price": str(price),
        "timeInForce": "GTC"
    }

    logger.info("📤 Отправка ордера на Bybit...")
    response = bybit.request("POST", "/v5/order/create", payload)
    order_id = response["result"]["orderId"]
    logger.info(f"✅ Ордер {side} успешно создан: {order_id}")

    return {
        "orderId": order_id,
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "price": price,
        "marketPrice": market_price,
        "offsetPercent": PRICE_OFFSET_PERCENT
    }

# ============================================================
# 5. TELEGRAM ОБРАБОТЧИКИ
# ============================================================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message):
    chat_id = message.chat.id
    logger.info(f"👤 /start от {chat_id}")
    base_coin = TRADING_SYMBOL.replace("USDT", "")

    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💰 Баланс USDT", callback_data="balance_usdt"),
        types.InlineKeyboardButton(f"💎 Баланс {base_coin}", callback_data="balance_coin"),
        types.InlineKeyboardButton(f"📉 Купить {base_coin} (-{PRICE_OFFSET_PERCENT}%)", callback_data="buy"),
        types.InlineKeyboardButton(f"📈 Продать {base_coin} (+{PRICE_OFFSET_PERCENT}%)", callback_data="sell"),
        types.InlineKeyboardButton("⚙️ Настройки бота", callback_data="settings")
    )

    bot.send_message(
        chat_id,
        "🤖 <b>Bybit V5 Test Bot</b>\n\n"
        "Бот для проверки способности выставлять ордера.\n"
        "Ордера выставляются лимитные с отступом от рынка,\n"
        "чтобы они <b>НЕ исполнялись</b>.\n\n"
        f"📊 <b>Пара:</b> {TRADING_SYMBOL}\n"
        f"💰 <b>Мин. сумма:</b> {MIN_ORDER_AMOUNT} USDT\n"
        f"💵 <b>Желаемая сумма:</b> {TARGET_ORDER_AMOUNT} USDT\n"
        f"📉 <b>Отступ:</b> {PRICE_OFFSET_PERCENT}%\n"
        f"🏦 <b>Категория:</b> {CATEGORY}\n"
        f"🔀 <b>Режим:</b> {'СУБАККАУНТ' if BYBIT_SUBACCOUNT_UID else 'ОСНОВНОЙ'}",
        parse_mode="HTML",
        reply_markup=markup
    )


@bot.callback_query_handler(func=lambda call: call.data == "balance_usdt")
def cb_balance_usdt(call: types.CallbackQuery):
    bot.answer_callback_query(call.id, "Загрузка...")
    logger.info(f"🔘 Нажата кнопка: balance_usdt от {call.from_user.id}")
    try:
        bal = get_usdt_balance()
        bot.edit_message_text(
            f"💰 <b>Баланс USDT</b>\n\n"
            f"Доступно: <code>{bal:.4f}</code> USDT\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("Ошибка при получении баланса USDT")
        bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "balance_coin")
def cb_balance_coin(call: types.CallbackQuery):
    bot.answer_callback_query(call.id, "Загрузка...")
    base_coin = TRADING_SYMBOL.replace("USDT", "")
    logger.info(f"🔘 Нажата кнопка: balance_coin от {call.from_user.id}")
    try:
        bal = get_coin_balance(base_coin)
        bot.edit_message_text(
            f"💎 <b>Баланс {base_coin}</b>\n\n"
            f"Доступно: <code>{bal:.8f}</code> {base_coin}\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            call.message.chat.id,
            call.message.message_id,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("Ошибка при получении баланса монеты")
        bot.send_message(call.message.chat.id, f"❌ Ошибка: {e}")


@bot.callback_query_handler(func=lambda call: call.data == "buy")
def cb_buy(call: types.CallbackQuery):
    bot.answer_callback_query(call.id, "Выставляю ордер на покупку...")
    base_coin = TRADING_SYMBOL.replace("USDT", "")
    logger.info(f"🔘 Нажата кнопка: buy от {call.from_user.id}")
    try:
        bal = get_usdt_balance()
        logger.info(f"💰 Доступный баланс: {bal} USDT")

        if bal < MIN_ORDER_AMOUNT:
            raise ValueError(f"Недостаточно средств. Баланс: {bal:.4f} USDT, минимум: {MIN_ORDER_AMOUNT} USDT")

        if bal >= TARGET_ORDER_AMOUNT:
            amount = TARGET_ORDER_AMOUNT
        else:
            amount = round(bal - 0.01, 2)
            if amount < MIN_ORDER_AMOUNT:
                raise ValueError(f"После округления сумма {amount} USDT меньше минимальной {MIN_ORDER_AMOUNT} USDT")

        logger.info(f"💵 Сумма для покупки: {amount} USDT")
        result = place_limit_order(side="Buy", usdt_amount=amount)

        text = (
            f"✅ <b>Ордер на покупку создан!</b>\n\n"
            f"🆔 Order ID: <code>{result['orderId']}</code>\n"
            f"🎯 Пара: {result['symbol']}\n"
            f"📉 Тип: Limit Buy\n"
            f"💵 Сумма: <code>{amount}</code> USDT\n"
            f"🔢 Количество: <code>{result['qty']}</code> {base_coin}\n"
            f"💲 Цена ордера: <code>{result['price']}</code> USDT\n"
            f"📊 Рыночная цена: <code>{result['marketPrice']}</code> USDT\n"
            f"📏 Отступ от рынка: <code>{result['offsetPercent']}%</code>\n\n"
            f"💡 Ордер НЕ исполнится, пока цена не упадёт на {result['offsetPercent']}%\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        logger.info(f"✅ Ордер {result['orderId']} создан")

    except Exception as e:
        error_text = str(e)
        logger.exception("Ошибка при создании ордера на покупку")
        if "10003" in error_text or "sign" in error_text.lower():
            error_text += "\n\n⚠️ Проверьте API Secret в .env (нет ли пробелов/переносов)"
        elif "минимальной" in error_text.lower() or "min" in error_text.lower():
            error_text += f"\n\n💡 Пополните субаккаунт минимум до {MIN_ORDER_AMOUNT} USDT"
        bot.send_message(call.message.chat.id, f"❌ Ошибка:\n<code>{error_text[:400]}</code>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "sell")
def cb_sell(call: types.CallbackQuery):
    bot.answer_callback_query(call.id, "Выставляю ордер на продажу...")
    base_coin = TRADING_SYMBOL.replace("USDT", "")
    logger.info(f"🔘 Нажата кнопка: sell от {call.from_user.id}")
    try:
        coin_bal = get_coin_balance(base_coin)
        logger.info(f"💎 Доступный баланс {base_coin}: {coin_bal}")

        if coin_bal <= 0:
            raise ValueError(f"Нет монет {base_coin} для продажи. Сначала купите {base_coin} за USDT.")

        if coin_bal >= 0.001:
            sell_amount = round(coin_bal * 0.1, 8)
        else:
            sell_amount = coin_bal

        logger.info(f"💵 Количество для продажи: {sell_amount} {base_coin}")
        result = place_limit_order(side="Sell", coin_amount=sell_amount)

        text = (
            f"✅ <b>Ордер на продажу создан!</b>\n\n"
            f"🆔 Order ID: <code>{result['orderId']}</code>\n"
            f"🎯 Пара: {result['symbol']}\n"
            f"📈 Тип: Limit Sell\n"
            f"🔢 Количество: <code>{result['qty']}</code> {base_coin}\n"
            f"💲 Цена ордера: <code>{result['price']}</code> USDT\n"
            f"📊 Рыночная цена: <code>{result['marketPrice']}</code> USDT\n"
            f"📏 Отступ от рынка: <code>+{result['offsetPercent']}%</code>\n\n"
            f"💡 Ордер НЕ исполнится, пока цена не вырастет на {result['offsetPercent']}%\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML")
        logger.info(f"✅ Ордер {result['orderId']} создан")

    except Exception as e:
        error_text = str(e)
        logger.exception("Ошибка при создании ордера на продажу")
        bot.send_message(call.message.chat.id, f"❌ Ошибка:\n<code>{error_text[:400]}</code>", parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "settings")
def cb_settings(call: types.CallbackQuery):
    bot.answer_callback_query(call.id, "Загрузка...")
    logger.info(f"🔘 Нажата кнопка: settings от {call.from_user.id}")
    text = (
        "⚙️ <b>Текущие настройки бота</b>\n\n"
        f"📊 <b>Торговая пара:</b> <code>{TRADING_SYMBOL}</code>\n"
        f"🏦 <b>Категория:</b> <code>{CATEGORY}</code>\n"
        f"💰 <b>Мин. сумма ордера:</b> <code>{MIN_ORDER_AMOUNT}</code> USDT\n"
        f"💵 <b>Желаемая сумма:</b> <code>{TARGET_ORDER_AMOUNT}</code> USDT\n"
        f"📉 <b>Отступ от рынка:</b> <code>{PRICE_OFFSET_PERCENT}%</code>\n"
        f"🔑 <b>API Key:</b> <code>{BYBIT_API_KEY[:8]}...{BYBIT_API_KEY[-4:]}</code>\n"
        f"🔀 <b>Subaccount UID:</b> <code>{BYBIT_SUBACCOUNT_UID or '(не задан)'}</code>\n\n"
        "💡 <b>Чтобы изменить настройки:</b>\n"
        "Откройте файл <code>bybit.py</code> и измените значения в начале файла."
    )
    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, parse_mode="HTML")


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_unknown(message: types.Message):
    bot.reply_to(message, "Используйте /start для меню")

# ============================================================
# 6. ЗАПУСК БОТА
# ============================================================
def main() -> None:
    logger.info("=" * 60)
    logger.info("🎯 Бот готов к работе. Ожидание команд...")
    logger.info("=" * 60)

    try:
        logger.info("🔌 Проверка и удаление активного вебхука...")
        bot.delete_webhook()
        logger.info("✅ Вебхук удален, можно безопасно использовать polling")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось удалить вебхук: {e}")

    try:
        logger.info("🔍 Проверка подключения к Bybit API...")
        market_price, _ = get_market_price(TRADING_SYMBOL)
        logger.info(f"✅ Подключение к Bybit успешно. {TRADING_SYMBOL} = {market_price}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось проверить подключение к Bybit: {e}")

    while True:
        try:
            logger.info("🚀 Запуск polling Telegram...")
            bot.infinity_polling(timeout=60, long_polling_timeout=60, skip_pending=True)
        except Exception as e:
            logger.error(f"❌ Ошибка polling: {e}")
            logger.error(traceback.format_exc())
            logger.info("⏳ Перезапуск через 5 секунд...")
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("🛑 Бот остановлен пользователем (Ctrl+C)")
    except Exception as e:
        logger.critical(f"💥 Критическая ошибка: {e}")
        logger.critical(traceback.format_exc())