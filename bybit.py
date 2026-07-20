"""
Telegram бот для работы с Bybit API (субаккаунт)
Функции:
  /start  - приветствие и меню
  /balance - показать баланс USDT
  Кнопка "Выставить ордер" - лимитный ордер на покупку на 10% ниже рынка
"""
import os
import sys
import time
import logging
import traceback
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
    sys.exit("❌ Ошибка: TELEGRAM_BOT_TOKEN не задан в .env")
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    sys.exit("❌ Ошибка: BYBIT_API_KEY или BYBIT_API_SECRET не заданы в .env")

# ============================================================
# 2. НАСТРОЙКА ЛОГИРОВАНИЯ
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

pybit_logger = logging.getLogger("pybit")
pybit_logger.setLevel(logging.WARNING)
pybit_logger.addHandler(file_handler)

logger.info("=" * 60)
logger.info("🚀 Запуск Telegram бота для Bybit")
logger.info("=" * 60)
logger.info(f"Bybit API Key (первые 8 символов): {BYBIT_API_KEY[:8]}...")
logger.info(f"Bybit Subaccount UID: {BYBIT_SUBACCOUNT_UID or '(не задан - работаем с основным аккаунтом)'}")

# ============================================================
# 3. ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ
# ============================================================
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
logger.info("✅ Telegram бот инициализирован")

try:
    bybit_session = HTTP(
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_API_SECRET,
        testnet=False,
        recv_window=10000,
        logging_level=logging.DEBUG
    )
    logger.info("✅ Bybit HTTP клиент инициализирован (V5 Unified Trading)")
except Exception as e:
    logger.critical(f"❌ Критическая ошибка инициализации Bybit: {e}")
    logger.critical(traceback.format_exc())
    sys.exit(1)


# ============================================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def send_error_to_user(chat_id: int, error: Exception, context: str = "") -> None:
    error_text = f"❌ Ошибка{f' ({context})' if context else ''}:\n`{str(error)[:500]}`"
    try:
        bot.send_message(chat_id, error_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке: {e}")


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
            logger.warning(f"Не удалось получить цену в категории {category}: {e}")
            continue
    
    raise RuntimeError(f"Не удалось получить рыночную цену для {symbol}")


def get_usdt_balance() -> Tuple[float, float]:
    logger.info("💰 Запрос баланса USDT")
    
    try:
        response = bybit_session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
        logger.debug(f"Ответ get_wallet_balance: {response}")
        
        if response.get("retCode") != 0:
            raise RuntimeError(f"Bybit вернул ошибку: {response.get('retMsg', 'unknown')}")
        
        result_list = response.get("result", {}).get("list", [])
        if not result_list:
            raise RuntimeError("Пустой результат баланса")
        
        coin_list = result_list[0].get("coin", [])
        usdt_data = next((c for c in coin_list if c.get("coin") == "USDT"), None)
        
        if not usdt_data:
            raise RuntimeError("USDT не найден в балансе")
        
        # availableToWithdraw может быть пустой строкой ''
        available_raw = usdt_data.get("availableToWithdraw") or usdt_data.get("walletBalance", 0)
        available = float(available_raw)
        total = float(usdt_data.get("walletBalance", 0))
        
        logger.info(f"✅ Баланс USDT: доступно={available}, всего={total}")
        return available, total
        
    except (FailedRequestError, InvalidRequestError) as e:
        logger.error(f"Ошибка Bybit API при получении баланса: {e}")
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка при получении баланса: {e}")
        logger.error(traceback.format_exc())
        raise


def place_limit_buy_order(
    symbol: str = "BTCUSDT",
    usdt_amount: float = 10.0,
    price: Optional[float] = None,
    category: str = "spot"
) -> dict:
    logger.info(f"📝 Выставление лимитного ордера: {symbol}, сумма={usdt_amount} USDT, категория={category}")
    
    # 1. Получаем рыночную цену
    if price is None:
        market_price, category = get_market_price(symbol)
        price = market_price * 0.9
        logger.info(f"💡 Цена ордера: {price} (рынок {market_price} - 10%)")
    else:
        logger.info(f"💡 Цена ордера (указана вручную): {price}")
    
    # 2. Получаем информацию об инструменте
    logger.info(f"🔍 Запрос информации об инструменте {symbol}")
    instruments_response = bybit_session.get_instruments_info(category=category, symbol=symbol)
    logger.debug(f"Ответ get_instruments_info: {instruments_response}")
    
    if instruments_response.get("retCode") != 0:
        raise RuntimeError(f"Ошибка получения информации об инструменте: {instruments_response.get('retMsg')}")
    
    instrument = instruments_response["result"]["list"][0]
    lot_filter = instrument["lotSizeFilter"]
    
    # 🔑 ИСПРАВЛЕНИЕ: для SPOT используется basePrecision, для фьючерсов - qtyStep
    if category == "spot":
        lot_size = float(lot_filter.get("basePrecision", "0.000001"))
        min_qty = float(lot_filter.get("minOrderQty", "0"))
        min_amt = float(lot_filter.get("minOrderAmt", "0"))
    else:
        lot_size = float(lot_filter.get("qtyStep", "0.000001"))
        min_qty = float(lot_filter.get("minOrderQty", "0"))
        min_amt = 0
    
    tick_size = float(instrument["priceFilter"]["tickSize"])
    
    logger.info(
        f"📏 Параметры инструмента: "
        f"lot_step={lot_size}, min_qty={min_qty}, "
        f"min_amt={min_amt}, tick_size={tick_size}"
    )
    
    # 3. Вычисляем количество
    raw_qty = usdt_amount / price
    qty = float(Decimal(str(raw_qty)).quantize(Decimal(str(lot_size)), rounding=ROUND_DOWN))
    price = float(Decimal(str(price)).quantize(Decimal(str(tick_size)), rounding=ROUND_DOWN))
    
    logger.info(f"🧮 Расчёт: raw_qty={raw_qty}, qty={qty}, price={price}")
    
    # 4. Проверки
    if category == "spot" and min_amt > 0 and usdt_amount < min_amt:
        raise ValueError(
            f"Сумма ордера {usdt_amount} USDT меньше минимальной {min_amt} USDT для этой пары."
        )
    
    if qty < min_qty:
        raise ValueError(
            f"Рассчитанное количество {qty} меньше минимального {min_qty}. Увеличьте сумму ордера."
        )
    
    # 5. Выставляем ордер
    try:
        order_params = {
            "category": category,
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": str(qty),
            "price": str(price),
            "timeInForce": "GTC"
        }
        
        if BYBIT_SUBACCOUNT_UID:
            order_params["subaccountId"] = BYBIT_SUBACCOUNT_UID
        
        logger.info(f"📤 Отправка ордера: {order_params}")
        
        response = bybit_session.place_order(**order_params)
        logger.info(f"✅ Ордер успешно выставлен: {response}")
        
        if response.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit вернул ошибку: {response.get('retMsg')} (код: {response.get('retCode')})"
            )
        
        return response
        
    except (FailedRequestError, InvalidRequestError) as e:
        logger.error(f"Ошибка Bybit API при выставлении ордера: {e}")
        logger.error(f"Полный ответ: {getattr(e, 'resp', 'N/A')}")
        raise
    except Exception as e:
        logger.error(f"Неожиданная ошибка при выставлении ордера: {e}")
        logger.error(traceback.format_exc())
        raise


# ============================================================
# 5. ОБРАБОТЧИКИ TELEGRAM
# ============================================================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message) -> None:
    chat_id = message.chat.id
    logger.info(f"👤 Команда /start от пользователя {chat_id}")
    
    markup = types.InlineKeyboardMarkup(row_width=1)
    markup.add(
        types.InlineKeyboardButton("💰 Показать баланс USDT", callback_data="show_balance"),
        types.InlineKeyboardButton("📉 Выставить ордер на покупку (−10%)", callback_data="place_order"),
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


@bot.message_handler(commands=["balance"])
def cmd_balance(message: types.Message) -> None:
    chat_id = message.chat.id
    logger.info(f"👤 Команда /balance от пользователя {chat_id}")
    try:
        available, total = get_usdt_balance()
        bot.send_message(
            chat_id,
            f"💰 <b>Баланс USDT</b>\n\n"
            f"📊 Доступно: <code>{available:.4f}</code> USDT\n"
            f"💼 Всего: <code>{total:.4f}</code> USDT\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("Ошибка при получении баланса")
        send_error_to_user(chat_id, e, "получение баланса")


@bot.callback_query_handler(func=lambda call: call.data == "show_balance")
def callback_show_balance(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    logger.info(f"🔘 Нажата кнопка 'Показать баланс' (user={chat_id})")
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
        logger.exception("Ошибка при получении баланса (callback)")
        send_error_to_user(chat_id, e, "получение баланса")
        bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}")


@bot.callback_query_handler(func=lambda call: call.data == "place_order")
def callback_place_order(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    logger.info(f"🔘 Нажата кнопка 'Выставить ордер' (user={chat_id})")
    bot.answer_callback_query(call.id, "⏳ Выставляю ордер...")
    
    try:
        available, total = get_usdt_balance()
        logger.info(f"Доступный баланс: {available} USDT")
        
        if available < 10:
            raise ValueError(f"Недостаточно средств. Доступно: {available:.2f} USDT, нужно минимум 10 USDT.")
        
        usdt_amount = min(10.0, available * 0.1)
        usdt_amount = round(usdt_amount, 2)
        logger.info(f"Сумма для ордера: {usdt_amount} USDT")
        
        response = place_limit_buy_order(symbol="BTCUSDT", usdt_amount=usdt_amount)
        
        order_id = response["result"]["orderId"]
        order_link = response["result"].get("orderLinkId", "N/A")
        
        success_text = (
            f"✅ <b>Ордер успешно выставлен!</b>\n\n"
            f"📋 ID ордера: <code>{order_id}</code>\n"
            f"🔗 Link ID: <code>{order_link}</code>\n"
            f"💵 Сумма: <code>{usdt_amount}</code> USDT\n"
            f"📉 Тип: Limit Buy (−10% от рынка)\n"
            f"🎯 Пара: BTCUSDT\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"💡 Ордер исполнится, если цена упадёт на 10%."
        )
        
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=success_text,
            parse_mode="HTML"
        )
        logger.info(f"✅ Ордер {order_id} успешно выставлен")
        
    except Exception as e:
        logger.exception("Ошибка при выставлении ордера")
        error_msg = str(e)
        
        if "insufficient balance" in error_msg.lower():
            error_text = "❌ Недостаточно средств на аккаунте"
        elif "invalid price" in error_msg.lower():
            error_text = "❌ Неверная цена ордера"
        elif "min order" in error_msg.lower():
            error_text = "❌ Сумма ордера меньше минимальной"
        elif "permission" in error_msg.lower() or "access" in error_msg.lower():
            error_text = "❌ Нет прав на торговлю. Проверьте API-ключ."
        else:
            error_text = f"❌ Ошибка: {error_msg[:300]}"
        
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=error_text,
                parse_mode="HTML"
            )
        except Exception:
            bot.send_message(chat_id, error_text, parse_mode="HTML")


@bot.callback_query_handler(func=lambda call: call.data == "account_info")
def callback_account_info(call: types.CallbackQuery) -> None:
    chat_id = call.message.chat.id
    logger.info(f"🔘 Нажата кнопка 'Информация об аккаунте' (user={chat_id})")
    bot.answer_callback_query(call.id, "⏳ Загружаю информацию...")
    
    try:
        wallet_info = bybit_session.get_wallet_balance(accountType="UNIFIED")
        account_type = "UNIFIED"
        if wallet_info.get("retCode") == 0 and wallet_info.get("result", {}).get("list"):
            account_type = wallet_info["result"]["list"][0].get("accountType", "UNIFIED")
        
        info_text = (
            f"ℹ️ <b>Информация об аккаунте</b>\n\n"
            f"🔑 API Key: <code>{BYBIT_API_KEY[:8]}...{BYBIT_API_KEY[-4:]}</code>\n"
            f"🏦 Тип аккаунта: <b>{account_type}</b>\n"
            f"{'🔀 Субаккаунт UID: <code>' + BYBIT_SUBACCOUNT_UID + '</code>' if BYBIT_SUBACCOUNT_UID else '🏠 Основной аккаунт'}\n"
            f"🌐 Сеть: <b>Mainnet</b> (реальные средства)\n\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        bot.edit_message_text(
            chat_id=chat_id,
            message_id=call.message.message_id,
            text=info_text,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.exception("Ошибка при получении информации об аккаунте")
        send_error_to_user(chat_id, e, "информация об аккаунте")


@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_unknown(message: types.Message) -> None:
    logger.debug(f"Неизвестное сообщение от {message.chat.id}: {message.text}")
    bot.reply_to(message, "Неизвестная команда. Используйте /start для меню.")


def global_exception_handler(exctype, value, tb):
    logger.critical("💥 НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:")
    logger.critical("".join(traceback.format_exception(exctype, value, tb)))

sys.excepthook = global_exception_handler


def main() -> None:
    logger.info("=" * 60)
    logger.info("🎯 Бот готов к работе. Ожидание команд...")
    logger.info("=" * 60)
    
    try:
        logger.info("🔍 Проверка подключения к Bybit API...")
        market_price, _ = get_market_price("BTCUSDT")
        logger.info(f"✅ Подключение к Bybit успешно. BTC/USDT = {market_price}")
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