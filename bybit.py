"""
Telegram бот для работы с Bybit API (субаккаунт)
Функции:
  - /start  - приветствие и меню
  - /balance - показать баланс USDT
  - Кнопка "Выставить ордер" - лимитный ордер на покупку на 10% ниже рынка
  
Автор: Qwen3.7
Дата: 2026-07-20
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
from telebot.handler_backends import BaseMiddleware
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

# Проверяем обязательные переменные
if not TELEGRAM_BOT_TOKEN:
    sys.exit("❌ Ошибка: TELEGRAM_BOT_TOKEN не задан в .env")
if not BYBIT_API_KEY or not BYBIT_API_SECRET:
    sys.exit("❌ Ошибка: BYBIT_API_KEY или BYBIT_API_SECRET не заданы в .env")

# ============================================================
# 2. НАСТРОЙКА ЛОГИРОВАНИЯ (подробное)
# ============================================================
LOG_FILE = "bybit_bot.log"

# Создаём форматтер с миллисекундами для точного тайминга
log_formatter = logging.Formatter(
    fmt="[%(asctime)s.%(msecs)03d] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# Хэндлер для файла (подробный)
file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
file_handler.setLevel(logging.DEBUG)
file_handler.setFormatter(log_formatter)

# Хэндлер для консоли (информативный)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(log_formatter)

# Корневой логгер
logger = logging.getLogger("BybitBot")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Логгер для pybit (чтобы видеть HTTP-запросы)
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
# Telegram бот
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
logger.info("✅ Telegram бот инициализирован")

# Bybit клиент (V5 Unified Trading API)
# testnet=False - работаем на реальном аккаунте
# Для работы с субаккаунтом через мастер-ключ используется recv_window
try:
    bybit_session = HTTP(
        api_key=BYBIT_API_KEY,
        api_secret=BYBIT_API_SECRET,
        testnet=False,
        recv_window=10000,  # увеличенное окно для стабильности
        logging_level=logging.DEBUG  # подробное логирование HTTP
    )
    logger.info("✅ Bybit HTTP клиент инициализирован (V5 Unified Trading)")
except Exception as e:
    logger.critical(f"❌ Критическая ошибка инициализации Bybit: {e}")
    logger.critical(traceback.format_exc())
    sys.exit(1)


# ============================================================
# 4. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================
def notify_admin(message: str, level: str = "info") -> None:
    """
    Отправляет уведомление админу в Telegram И пишет в лог.
    Так как TELEGRAM_CHAT_ID не используется - отправляем в тот чат,
    откуда пришёл запрос (через сохранённый chat_id).
    """
    log_method = getattr(logger, level, logger.info)
    log_method(f"[NOTIFY] {message}")


def send_error_to_user(chat_id: int, error: Exception, context: str = "") -> None:
    """Отправляет понятное сообщение об ошибке пользователю."""
    error_text = f"❌ Ошибка{f' ({context})' if context else ''}:\n`{str(error)[:500]}`"
    try:
        bot.send_message(chat_id, error_text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Не удалось отправить сообщение об ошибке: {e}")


def get_market_price(symbol: str = "BTCUSDT") -> Tuple[float, str]:
    """
    Получает текущую рыночную цену тикера.
    Возвращает: (цена, категория)
    """
    logger.info(f"📊 Запрос рыночной цены для {symbol}")
    
    # Пробуем сначала SPOT, потом LINEAR (фьючерсы)
    for category in ["spot", "linear"]:
        try:
            response = bybit_session.get_tickers(
                category=category,
                symbol=symbol
            )
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
    """
    Получает баланс USDT (доступный и общий).
    Возвращает: (available_balance, total_balance)
    """
    logger.info("💰 Запрос баланса USDT")
    
    try:
        # Для субаккаунта через мастер-ключ - передаём заголовок
        headers = {}
        if BYBIT_SUBACCOUNT_UID:
            headers["X-BAPI-SUB-ACCOUNT-UID"] = BYBIT_SUBACCOUNT_UID
            logger.info(f"Используется субаккаунт UID: {BYBIT_SUBACCOUNT_UID}")
        
        # Получаем баланс из UNIFIED аккаунта
        response = bybit_session.get_wallet_balance(
            accountType="UNIFIED",
            coin="USDT"
        )
        logger.debug(f"Ответ get_wallet_balance: {response}")
        
        if response.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit вернул ошибку: {response.get('retMsg', 'unknown')}"
            )
        
        result_list = response.get("result", {}).get("list", [])
        if not result_list:
            raise RuntimeError("Пустой результат баланса")
        
        coin_list = result_list[0].get("coin", [])
        usdt_data = next((c for c in coin_list if c.get("coin") == "USDT"), None)
        
        if not usdt_data:
            raise RuntimeError("USDT не найден в балансе")
        
        available = float(usdt_data.get("availableToWithdraw", 0) or usdt_data.get("walletBalance", 0))
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
    """
    Выставляет лимитный ордер на покупку.
    
    Args:
        symbol: торговая пара
        usdt_amount: сумма в USDT, на которую покупаем
        price: цена ордера (если None - используется рыночная * 0.9)
        category: "spot" или "linear"
    
    Returns:
        dict с ответом от Bybit
    """
    logger.info(f"📝 Выставление лимитного ордера: {symbol}, сумма={usdt_amount} USDT")
    
    # 1. Получаем рыночную цену, если не указана
    if price is None:
        market_price, category = get_market_price(symbol)
        price = market_price * 0.9  # на 10% ниже рынка
        logger.info(f"💡 Цена ордера: {price} (рынок {market_price} - 10%)")
    else:
        logger.info(f"💡 Цена ордера (указана вручную): {price}")
    
    # 2. Получаем информацию о инструменте (lot size, tick size)
    logger.info(f"🔍 Запрос информации об инструменте {symbol}")
    instruments_response = bybit_session.get_instruments_info(
        category=category,
        symbol=symbol
    )
    logger.debug(f"Ответ get_instruments_info: {instruments_response}")
    
    if instruments_response.get("retCode") != 0:
        raise RuntimeError(
            f"Ошибка получения информации об инструменте: "
            f"{instruments_response.get('retMsg')}"
        )
    
    instrument = instruments_response["result"]["list"][0]
    lot_size = float(instrument["lotSizeFilter"]["qtyStep"])
    min_qty = float(instrument["lotSizeFilter"]["minOrderQty"])
    tick_size = float(instrument["priceFilter"]["tickSize"])
    
    logger.info(f"📏 Параметры инструмента: lot_step={lot_size}, min_qty={min_qty}, tick_size={tick_size}")
    
    # 3. Вычисляем количество (qty) с учётом step
    raw_qty = usdt_amount / price
    qty = float(Decimal(str(raw_qty)).quantize(
        Decimal(str(lot_size)), rounding=ROUND_DOWN
    ))
    
    # Округляем цену до tick_size
    price = float(Decimal(str(price)).quantize(
        Decimal(str(tick_size)), rounding=ROUND_DOWN
    ))
    
    # Проверяем минимальное количество
    if qty < min_qty:
        raise ValueError(
            f"Рассчитанное количество {qty} меньше минимального {min_qty}. "
            f"Увеличьте сумму ордера."
        )
    
    logger.info(f"🧮 Итоговые параметры ордера: qty={qty}, price={price}")
    
    # 4. Выставляем ордер
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
        
        # Если работаем через мастер-ключ с субаккаунтом
        if BYBIT_SUBACCOUNT_UID:
            order_params["subaccountId"] = BYBIT_SUBACCOUNT_UID
        
        logger.info(f"📤 Отправка ордера: {order_params}")
        
        response = bybit_session.place_order(**order_params)
        logger.info(f"✅ Ордер успешно выставлен: {response}")
        
        if response.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit вернул ошибку: {response.get('retMsg')} "
                f"(код: {response.get('retCode')})"
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
# 5. ОБРАБОТЧИКИ КОМАНД TELEGRAM
# ============================================================
@bot.message_handler(commands=["start", "help"])
def cmd_start(message: types.Message) -> None:
    """Приветствие и главное меню."""
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
    """Показать баланс USDT."""
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
    """Обработчик кнопки 'Показать баланс'."""
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
    """Обработчик кнопки 'Выставить ордер на покупку'."""
    chat_id = call.message.chat.id
    logger.info(f"🔘 Нажата кнопка 'Выставить ордер' (user={chat_id})")
    
    bot.answer_callback_query(call.id, "⏳ Выставляю ордер...")
    
    try:
        # 1. Получаем баланс
        available, total = get_usdt_balance()
        logger.info(f"Доступный баланс: {available} USDT")
        
        if available < 10:
            raise ValueError(
                f"Недостаточно средств. Доступно: {available:.2f} USDT, "
                f"нужно минимум 10 USDT для теста."
            )
        
        # 2. Используем 10 USDT для теста (или 10% от баланса, если меньше)
        usdt_amount = min(10.0, available * 0.1)
        usdt_amount = round(usdt_amount, 2)
        logger.info(f"Сумма для ордера: {usdt_amount} USDT")
        
        # 3. Выставляем ордер
        response = place_limit_buy_order(
            symbol="BTCUSDT",
            usdt_amount=usdt_amount
        )
        
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
        
        # Специальная обработка частых ошибок
        if "insufficient balance" in error_msg.lower():
            error_text = "❌ Недостаточно средств на аккаунте"
        elif "invalid price" in error_msg.lower():
            error_text = "❌ Неверная цена ордера"
        elif "min order" in error_msg.lower():
            error_text = "❌ Сумма ордера меньше минимальной"
        elif "permission" in error_msg.lower() or "access" in error_msg.lower():
            error_text = "❌ Нет прав на торговлю. Проверьте API-ключ субаккаунта."
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
    """Показать информацию об аккаунте."""
    chat_id = call.message.chat.id
    logger.info(f"🔘 Нажата кнопка 'Информация об аккаунте' (user={chat_id})")
    
    bot.answer_callback_query(call.id, "⏳ Загружаю информацию...")
    
    try:
        # Получаем информацию о кошельке
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


# ============================================================
# 6. ОБРАБОТКА НЕИЗВЕСТНЫХ КОМАНД
# ============================================================
@bot.message_handler(func=lambda m: True, content_types=["text"])
def handle_unknown(message: types.Message) -> None:
    """Обработка неизвестных текстовых сообщений."""
    logger.debug(f"Неизвестное сообщение от {message.chat.id}: {message.text}")
    bot.reply_to(message, "Неизвестная команда. Используйте /start для меню.")


# ============================================================
# 7. ГЛОБАЛЬНАЯ ОБРАБОТКА ИСКЛЮЧЕНИЙ
# ============================================================
def global_exception_handler(exctype, value, tb):
    """Перехват необработанных исключений."""
    logger.critical("💥 НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:")
    logger.critical("".join(traceback.format_exception(exctype, value, tb)))

sys.excepthook = global_exception_handler


# ============================================================
# 8. ЗАПУСК БОТА
# ============================================================
def main() -> None:
    """Главная функция запуска."""
    logger.info("=" * 60)
    logger.info("🎯 Бот готов к работе. Ожидание команд...")
    logger.info("=" * 60)
    
    # Проверяем подключение к Bybit
    try:
        logger.info("🔍 Проверка подключения к Bybit API...")
        market_price, _ = get_market_price("BTCUSDT")
        logger.info(f"✅ Подключение к Bybit успешно. BTC/USDT = {market_price}")
    except Exception as e:
        logger.warning(f"⚠️ Не удалось проверить подключение к Bybit: {e}")
        logger.warning("Бот продолжит работу, но могут быть проблемы с API")
    
    # Запускаем бота с авто-перезапуском при ошибках
    while True:
        try:
            logger.info("🚀 Запуск polling Telegram...")
            bot.infinity_polling(
                timeout=60,
                long_polling_timeout=60,
                skip_pending=True
            )
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