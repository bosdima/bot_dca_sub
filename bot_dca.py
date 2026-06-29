#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DCA Bybit Trading Bot - МАРТИНГЕЙЛ ЛЕСЕНКОЙ
Версия 5.16.0 (29.06.2026)
Оптимизированная версия
"""

import os
import sys
import asyncio
import logging
import json
import sqlite3
import re
import time
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from contextlib import contextmanager
from functools import wraps

# Импорты с автоматической установкой
try:
    import pytz
except ImportError:
    os.system(f"{sys.executable} -m pip install pytz")
    import pytz

from dotenv import load_dotenv
from colorama import init, Fore
from logging.handlers import RotatingFileHandler
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, ConversationHandler, filters
from telegram.request import HTTPXRequest
from pybit.unified_trading import HTTP

# Конфигурация
load_dotenv()
init(autoreset=True)

# ============ КОНСТАНТЫ ============
DEFAULT_SYMBOL = "ETHUSDT"
POPULAR_SYMBOLS = ["ETHUSDT", "XRPUSDT", "BTCUSDT"]
BOT_VERSION = "5.16.0 (29.06.2026)"
CONVERSATION_TIMEOUT = 180
SELL_DECIMALS_FALLBACK = 5
MAX_DROP_DEPTH = 80
DB_EXPORT_FILE = 'dca_data_export.json'
MOSCOW_TZ = pytz.timezone('Europe/Moscow')

# ============ ЛОГИРОВАНИЕ ============
log_handler = RotatingFileHandler("bot_errors.log", encoding='utf-8', maxBytes=200*1024, backupCount=2)
log_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler, logging.StreamHandler(sys.stdout)])
logger = logging.getLogger(__name__)

# ============ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ============
@dataclass
class Config:
    telegram_token: str = os.getenv('TELEGRAM_BOT_TOKEN')
    authorized_user: str = os.getenv('AUTHORIZED_USER', '@bosdima')
    testnet: bool = os.getenv('BYBIT_TESTNET', 'false').lower() == 'true'
    api_key: str = os.getenv('BYBIT_API_KEY')
    api_secret: str = os.getenv('BYBIT_API_SECRET')

def get_moscow_time() -> datetime:
    return datetime.now(MOSCOW_TZ)

def get_moscow_time_naive() -> datetime:
    return datetime.now(MOSCOW_TZ).replace(tzinfo=None)

def format_price(price: float, decimals: int = 4) -> str:
    return f"{price:.{decimals}f}" if price else "N/A"

def format_quantity(qty: float, decimals: int = 5) -> str:
    return f"{qty:.{decimals}f}" if qty else "N/A"

def calculate_drop(current: float, avg: float) -> float:
    return max(0, ((avg - current) / avg * 100)) if avg > 0 else 0

def calculate_apy(profit: float, invested: float, days: int) -> float:
    return (profit / invested) * (365 / max(days, 1)) * 100 if invested > 0 else 0

def round_by_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 4)
    rounded = math.floor(price / tick) * tick
    if rounded <= 0:
        rounded = tick
    decimals = len(str(tick).split('.')[-1]) if '.' in str(tick) else 4
    return round(rounded, decimals)

def round_quantity(qty: float, step: float, min_qty: float) -> float:
    if step <= 0:
        step = 0.01
    decimals = len(str(step).split('.')[-1]) if '.' in str(step) else 0
    rounded = math.ceil(qty / step) * step
    if rounded < min_qty:
        rounded = math.ceil(min_qty / step) * step
    return round(rounded, decimals)

def round_sell_qty(qty: float, decimals: int = SELL_DECIMALS_FALLBACK) -> float:
    return math.floor(qty * (10 ** decimals)) / (10 ** decimals) if qty > 0 else 0

@contextmanager
def db_connection(db_file: str = "dca_bot.db"):
    conn = sqlite3.connect(db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

# ============ БАЗА ДАННЫХ ============
class Database:
    def __init__(self, db_file: str = "dca_bot.db"):
        self.db_file = db_file
        self._init_db()

    def _init_db(self):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            
            # Основные таблицы
            cursor.executescript('''
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS dca_purchases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL, amount_usdt REAL NOT NULL,
                    price REAL NOT NULL, quantity REAL NOT NULL, multiplier REAL DEFAULT 1.0,
                    drop_percent REAL DEFAULT 0, step_level INTEGER DEFAULT 0,
                    date TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, order_id TEXT
                );
                CREATE TABLE IF NOT EXISTS sell_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL UNIQUE, quantity REAL NOT NULL,
                    target_price REAL NOT NULL, profit_percent REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'active'
                );
                CREATE TABLE IF NOT EXISTS pending_sell_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                    quantity REAL NOT NULL, target_price REAL NOT NULL, profit_percent REAL NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT DEFAULT 'pending',
                    retry_count INTEGER DEFAULT 0, last_retry TIMESTAMP, fail_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS completed_sells (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                    order_id TEXT NOT NULL, quantity REAL NOT NULL, sell_price REAL NOT NULL,
                    profit_percent REAL NOT NULL, profit_usdt REAL NOT NULL,
                    sold_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notified BOOLEAN DEFAULT 0,
                    stats_cleared BOOLEAN DEFAULT 0, clear_deadline TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                    symbol TEXT, details TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS dca_start (id INTEGER PRIMARY KEY, start_date TIMESTAMP, symbol TEXT, initial_price REAL);
                CREATE TABLE IF NOT EXISTS notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, enabled BOOLEAN DEFAULT 1,
                    alert_percent REAL DEFAULT 10.0, alert_interval_minutes INTEGER DEFAULT 30,
                    last_check TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ladder_settings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                    max_depth REAL NOT NULL, base_amount REAL NOT NULL,
                    max_amount REAL NOT NULL, step_percent REAL DEFAULT 1.0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS executed_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL, price REAL NOT NULL, quantity REAL NOT NULL,
                    amount_usdt REAL NOT NULL, executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    added_to_stats BOOLEAN DEFAULT 0, skipped BOOLEAN DEFAULT 0, notified_at TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT);
            ''')
            
            # Добавление колонок если отсутствуют
            for table, columns in [
                ('dca_purchases', ['order_id']),
                ('pending_sell_orders', ['retry_count', 'last_retry', 'fail_reason']),
                ('completed_sells', ['clear_deadline']),
                ('ladder_settings', ['step_percent']),
                ('executed_orders', ['skipped', 'notified_at', 'added_to_stats'])
            ]:
                for col in columns:
                    cursor.execute(f"PRAGMA table_info({table})")
                    if col not in [c[1] for c in cursor.fetchall()]:
                        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

            # Настройки по умолчанию
            defaults = [
                ('symbol', DEFAULT_SYMBOL), ('invest_amount', '5.0'), ('manual_amount', '1.1'),
                ('profit_percent', '5'), ('max_drop_percent', '80'), ('schedule_time', '05:00'),
                ('frequency_hours', '24'), ('dca_active', 'false'), ('order_execution_notify', 'true'),
                ('order_check_interval_minutes', '5'), ('sell_tracking_enabled', 'true'),
                ('purchase_notify_enabled', 'true'), ('purchase_notify_time', '06:00'),
                ('trading_mode', 'real'), ('api_status', 'unknown'), ('api_error_message', '')
            ]
            for key, value in defaults:
                cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
            
            cursor.execute('INSERT OR IGNORE INTO notifications (id, enabled, alert_percent, alert_interval_minutes, last_check) VALUES (1, 1, 10.0, 30, CURRENT_TIMESTAMP)')
            conn.commit()

    def get_setting(self, key: str, default: str = '') -> str:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
            row = cursor.fetchone()
            return row[0] if row else default

    def set_setting(self, key: str, value: str):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP)', (key, value))
            conn.commit()

    # Краткие методы для часто используемых настроек
    def get_bool(self, key: str, default: str = 'true') -> bool:
        return self.get_setting(key, default) == 'true'
    
    def set_bool(self, key: str, value: bool):
        self.set_setting(key, 'true' if value else 'false')
    
    def get_float(self, key: str, default: str = '0') -> float:
        return float(self.get_setting(key, default))
    
    def get_int(self, key: str, default: str = '0') -> int:
        return int(self.get_setting(key, default))
    
    def get_trading_mode(self) -> str:
        return self.get_setting('trading_mode', 'real')
    
    def is_demo(self) -> bool:
        return self.get_trading_mode() == 'demo'

    def get_manual_amount(self) -> float:
        return self.get_float('manual_amount', '1.1')

    def get_authorized_user_id(self) -> Optional[int]:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM bot_state WHERE key = "authorized_user_id"')
            row = cursor.fetchone()
            return int(row[0]) if row else None
    
    def set_authorized_user_id(self, user_id: int):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)', ('authorized_user_id', str(user_id)))
            conn.commit()

    # Методы работы с покупками
    def add_purchase(self, symbol: str, amount_usdt: float, price: float, quantity: float,
                     drop_percent: float = 0, step_level: int = 0, order_id: str = None) -> Optional[int]:
        if order_id:
            with db_connection(self.db_file) as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT 1 FROM dca_purchases WHERE order_id = ?', (order_id,))
                if cursor.fetchone():
                    logger.warning(f"Order {order_id} already exists")
                    return None
        
        date = get_moscow_time_naive().strftime("%Y-%m-%d %H:%M:%S")
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO dca_purchases (symbol, amount_usdt, price, quantity, drop_percent, step_level, date, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (symbol, amount_usdt, price, quantity, drop_percent, step_level, date, order_id))
            purchase_id = cursor.lastrowid
            conn.commit()
        
        self._update_first_order_date()
        logger.info(f"Purchase added: ID={purchase_id}, {quantity} {symbol} @ {price}")
        return purchase_id

    def get_purchases(self, symbol: str = None) -> List[Dict]:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM dca_purchases WHERE symbol = ? ORDER BY date ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM dca_purchases ORDER BY date ASC')
            return [dict(row) for row in cursor.fetchall()]

    def get_purchase_by_id(self, purchase_id: int) -> Optional[Dict]:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM dca_purchases WHERE id = ?', (purchase_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    def update_purchase(self, purchase_id: int, **kwargs) -> bool:
        allowed = {'symbol', 'amount_usdt', 'price', 'quantity', 'multiplier', 'drop_percent', 'step_level', 'date', 'order_id'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False
        
        set_clause = ', '.join(f"{k} = ?" for k in updates)
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute(f'UPDATE dca_purchases SET {set_clause} WHERE id = ?', list(updates.values()) + [purchase_id])
            success = cursor.rowcount > 0
            conn.commit()
        if success:
            self._update_first_order_date()
        return success

    def delete_purchase(self, purchase_id: int) -> bool:
        purchase = self.get_purchase_by_id(purchase_id)
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE id = ?', (purchase_id,))
            success = cursor.rowcount > 0
            conn.commit()
        if success and purchase:
            self._reset_executed_order(purchase['price'], purchase['quantity'], purchase['symbol'])
            self._update_first_order_date()
        return success

    def clear_all_purchases(self, symbol: str) -> int:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM dca_purchases WHERE symbol = ?', (symbol,))
            count = cursor.rowcount
            cursor.execute("DELETE FROM sqlite_sequence WHERE name='dca_purchases'")
            conn.commit()
        self._update_first_order_date()
        return count

    def get_dca_stats(self, symbol: str) -> Optional[Dict]:
        purchases = self.get_purchases(symbol)
        if not purchases:
            return None
        total_usdt = sum(p['amount_usdt'] for p in purchases)
        total_qty = sum(p['quantity'] for p in purchases)
        return {
            'total_purchases': len(purchases),
            'total_usdt': total_usdt,
            'total_quantity': total_qty,
            'avg_price': total_usdt / total_qty if total_qty > 0 else 0
        }

    def _update_first_order_date(self):
        purchases = self.get_purchases()
        if purchases:
            try:
                first = min(purchases, key=lambda x: x['date'])
                self.set_setting('first_order_date', datetime.strptime(first['date'], "%Y-%m-%d %H:%M:%S").isoformat())
            except:
                pass
        else:
            self.set_setting('first_order_date', '')

    def _reset_executed_order(self, price: float, quantity: float, symbol: str):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE executed_orders SET added_to_stats = 0, skipped = 0, notified_at = NULL 
                WHERE symbol = ? AND ABS(price - ?) < 0.0001 AND ABS(quantity - ?) < 0.0001
            ''', (symbol, price, quantity))
            conn.commit()

    # Методы для ордеров на продажу
    def add_sell_order(self, symbol: str, order_id: str, quantity: float, target_price: float, profit_percent: float):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute('INSERT INTO sell_orders (symbol, order_id, quantity, target_price, profit_percent) VALUES (?, ?, ?, ?, ?)',
                              (symbol, order_id, quantity, target_price, profit_percent))
            except sqlite3.IntegrityError:
                cursor.execute('UPDATE sell_orders SET target_price = ?, profit_percent = ?, status = "active" WHERE order_id = ?',
                              (target_price, profit_percent, order_id))
            conn.commit()

    def get_active_sell_orders(self, symbol: str = None) -> List[Dict]:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM sell_orders WHERE symbol = ? AND status = "active" ORDER BY created_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM sell_orders WHERE status = "active" ORDER BY created_at DESC')
            return [dict(row) for row in cursor.fetchall()]

    def update_sell_order_status(self, order_id: str, status: str):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE sell_orders SET status = ? WHERE order_id = ?', (status, order_id))
            conn.commit()

    # Отложенные ордера
    def add_pending_sell_order(self, symbol: str, quantity: float, target_price: float, profit_percent: float, fail_reason: str = None) -> int:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO pending_sell_orders (symbol, quantity, target_price, profit_percent, status, retry_count, last_retry, fail_reason)
                VALUES (?, ?, ?, ?, 'pending', 0, CURRENT_TIMESTAMP, ?)
            ''', (symbol, quantity, target_price, profit_percent, fail_reason))
            order_id = cursor.lastrowid
            conn.commit()
            return order_id

    def get_pending_sell_orders(self, symbol: str = None) -> List[Dict]:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM pending_sell_orders WHERE symbol = ? AND status = "pending" ORDER BY created_at ASC', (symbol,))
            else:
                cursor.execute('SELECT * FROM pending_sell_orders WHERE status = "pending" ORDER BY created_at ASC')
            return [dict(row) for row in cursor.fetchall()]

    def delete_pending_sell_order(self, order_id: int):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM pending_sell_orders WHERE id = ?', (order_id,))
            conn.commit()

    def update_pending_sell_retry(self, order_id: int, fail_reason: str = None):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE pending_sell_orders SET retry_count = retry_count + 1, last_retry = CURRENT_TIMESTAMP, fail_reason = ? WHERE id = ?',
                          (fail_reason, order_id))
            conn.commit()

    # Завершенные продажи
    def add_completed_sell(self, symbol: str, order_id: str, quantity: float, sell_price: float, profit_percent: float, profit_usdt: float) -> int:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO completed_sells (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt, notified, stats_cleared)
                VALUES (?, ?, ?, ?, ?, ?, 0, 0)
            ''', (symbol, order_id, quantity, sell_price, profit_percent, profit_usdt))
            sell_id = cursor.lastrowid
            conn.commit()
            return sell_id

    def mark_completed_sell_notified(self, sell_id: int):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE completed_sells SET notified = 1 WHERE id = ?', (sell_id,))
            conn.commit()

    def get_completed_sells_not_notified(self, symbol: str = None) -> List[Dict]:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            if symbol:
                cursor.execute('SELECT * FROM completed_sells WHERE symbol = ? AND notified = 0 ORDER BY sold_at DESC', (symbol,))
            else:
                cursor.execute('SELECT * FROM completed_sells WHERE notified = 0 ORDER BY sold_at DESC')
            return [dict(row) for row in cursor.fetchall()]

    # Лестница Мартингейла
    def get_ladder_settings(self, symbol: str = None) -> Dict:
        if symbol is None:
            symbol = self.get_setting('symbol', DEFAULT_SYMBOL)
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM ladder_settings WHERE symbol = ? ORDER BY created_at DESC LIMIT 1', (symbol,))
            row = cursor.fetchone()
            if row:
                return dict(row)
        return {
            'symbol': symbol,
            'max_depth': self.get_float('ladder_max_depth', '80'),
            'base_amount': self.get_float('invest_amount', '5.0'),
            'max_amount': self.get_float('invest_amount', '5.0') * 3,
            'step_percent': 1.0
        }

    def save_ladder_settings(self, settings: Dict):
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('DELETE FROM ladder_settings WHERE symbol = ?', (settings['symbol'],))
            cursor.execute('''
                INSERT INTO ladder_settings (symbol, max_depth, base_amount, max_amount, step_percent)
                VALUES (?, ?, ?, ?, ?)
            ''', (settings['symbol'], settings['max_depth'], settings['base_amount'], settings['max_amount'], settings.get('step_percent', 1.0)))
            conn.commit()
        self.set_setting('ladder_max_depth', str(settings['max_depth']))
        self.set_setting('ladder_base_amount', str(settings['base_amount']))
        self.set_setting('ladder_max_amount', str(settings['max_amount']))
        self.set_setting('invest_amount', str(settings['base_amount']))

    def calculate_ladder_purchase(self, current_price: float, symbol: str = None) -> Dict:
        symbol = symbol or self.get_setting('symbol', DEFAULT_SYMBOL)
        stats = self.get_dca_stats(symbol)
        
        if not stats or stats['total_quantity'] <= 0:
            return {
                'should_buy': True,
                'step_level': 0,
                'amount_usdt': self.get_ladder_settings(symbol)['base_amount'],
                'target_price': current_price,
                'drop_percent': 0,
                'reason': 'Первая покупка'
            }
        
        settings = self.get_ladder_settings(symbol)
        avg_price = stats['avg_price']
        current_drop = calculate_drop(current_price, avg_price)
        purchases = self.get_purchases(symbol)
        max_purchased_drop = max((p.get('drop_percent', 0) for p in purchases), default=0)
        
        if current_drop > max_purchased_drop + 0.01:
            amount = max_purchased_drop + 1
            if current_drop >= settings['max_depth']:
                return {
                    'should_buy': False,
                    'step_level': int(current_drop),
                    'amount_usdt': amount,
                    'reason': f'Достигнута максимальная глубина ({settings["max_depth"]}%)'
                }
            return {
                'should_buy': True,
                'step_level': int(current_drop),
                'amount_usdt': amount,
                'target_price': current_price,
                'drop_percent': current_drop,
                'reason': f'Падение {current_drop:.1f}% от средней цены'
            }
        
        next_drop = max_purchased_drop + 1
        return {
            'should_buy': False,
            'step_level': 0,
            'amount_usdt': 0,
            'target_price': avg_price * (1 - next_drop / 100),
            'current_drop': current_drop,
            'next_drop': next_drop,
            'reason': f'Ждем падения до {next_drop:.1f}%'
        }

    # Отслеживание ордеров
    def get_order_check_interval(self) -> int:
        return self.get_int('order_check_interval_minutes', '60')
    
    def get_order_execution_notify(self) -> bool:
        return self.get_bool('order_execution_notify', 'true')
    
    def get_sell_tracking_enabled(self) -> bool:
        return self.get_bool('sell_tracking_enabled', 'true')
    
    def get_purchase_notify_enabled(self) -> bool:
        return self.get_bool('purchase_notify_enabled', 'true')
    
    def get_purchase_notify_time(self) -> str:
        return self.get_setting('purchase_notify_time', '06:00')

    # Выполненные ордера
    def add_executed_order(self, order_id: str, symbol: str, price: float, quantity: float, amount_usdt: float, executed_at: str = None) -> bool:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            if executed_at:
                cursor.execute('''
                    INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt, executed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (order_id, symbol, price, quantity, amount_usdt, executed_at))
            else:
                cursor.execute('''
                    INSERT OR IGNORE INTO executed_orders (order_id, symbol, price, quantity, amount_usdt)
                    VALUES (?, ?, ?, ?, ?)
                ''', (order_id, symbol, price, quantity, amount_usdt))
            success = cursor.rowcount > 0
            conn.commit()
            return success

    def mark_order_as_added(self, order_id: str) -> bool:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET added_to_stats = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            return success

    def mark_order_as_skipped(self, order_id: str) -> bool:
        with db_connection(self.db_file) as conn:
            cursor = conn.cursor()
            cursor.execute('UPDATE executed_orders SET skipped = 1, notified_at = CURRENT_TIMESTAMP WHERE order_id = ?', (order_id,))
            success = cursor.rowcount > 0
            conn.commit()
            return success

# ============ BYBIT КЛИЕНТ ============
class BybitClient:
    def __init__(self, testnet: bool = False):
        self.config = Config()
        self.testnet = testnet
        self.session = None
        self._price_cache = {}
        self._cache_time = {}
        self._cache_ttl = 5
        self._instrument_cache = {}
        self._init_session()

    def _init_session(self):
        if self.config.api_key and self.config.api_secret:
            try:
                self.session = HTTP(testnet=self.testnet, api_key=self.config.api_key, api_secret=self.config.api_secret, recv_window=10000)
                logger.info(f"Bybit session initialized (testnet={self.testnet})")
            except Exception as e:
                logger.error(f"Session init error: {e}")
                self.session = None

    def _is_available(self) -> bool:
        if not self.session:
            self._init_session()
        return self.session is not None

    async def check_api_health(self) -> Dict:
        if not self._is_available():
            return {'success': False, 'user_message': 'API ключи не настроены', 'is_api_error': True}
        
        try:
            response = self.session.get_wallet_balance(accountType="UNIFIED")
            if response['retCode'] == 0:
                return {'success': True}
            error_code = response.get('retCode', 0)
            error_msgs = {10003: 'Ключ не найден', 10004: 'Ключ истек или неверный', 10005: 'Неверный ключ или секрет',
                         10006: 'Недостаточно прав', 10010: 'IP не в белом списке', 10016: 'Лимит запросов'}
            return {
                'success': False,
                'error_code': error_code,
                'user_message': error_msgs.get(error_code, response.get('retMsg', 'Неизвестная ошибка')),
                'is_api_error': error_code in (10003, 10004, 10005, 10006, 10010, 10016)
            }
        except Exception as e:
            return {'success': False, 'user_message': str(e), 'is_api_error': True}

    async def get_symbol_price(self, symbol: str) -> Optional[float]:
        if not self._is_available():
            return None
        now = time.time()
        if symbol in self._cache_time and now - self._cache_time.get(symbol, 0) < self._cache_ttl:
            return self._price_cache.get(symbol)
        
        try:
            response = self.session.get_tickers(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                price = float(response['result']['list'][0]['lastPrice'])
                self._price_cache[symbol] = price
                self._cache_time[symbol] = now
                return price
            return None
        except Exception as e:
            logger.error(f"Price error {symbol}: {e}")
            return None

    async def get_balance(self, coin: str = None) -> Dict:
        if not self._is_available():
            return {'error': 'API не доступен'}
        
        try:
            for acc_type in ["UNIFIED", "SPOT"]:
                try:
                    response = self.session.get_wallet_balance(accountType=acc_type)
                    if response['retCode'] == 0 and response['result']['list']:
                        account = response['result']['list'][0]
                        coins = account.get('coin', [])
                        if coin:
                            for c in coins:
                                if c.get('coin') == coin:
                                    wallet = float(c.get('walletBalance', 0) or 0)
                                    locked = float(c.get('locked', 0) or 0)
                                    return {
                                        'coin': coin,
                                        'equity': float(c.get('equity', 0) or 0) or wallet,
                                        'available': max(0, wallet - locked),
                                        'usdValue': float(c.get('usdValue', 0) or 0)
                                    }
                            return {'coin': coin, 'equity': 0, 'available': 0, 'usdValue': 0}
                        return {'total_equity': float(account.get('totalEquity', 0) or 0), 'coins': coins}
                except:
                    continue
        except Exception as e:
            logger.error(f"Balance error: {e}")
        return {'error': 'Не удалось получить баланс'}

    async def get_open_orders(self, symbol: str = None) -> List[Dict]:
        if not self._is_available():
            return []
        try:
            params = {"category": "spot"}
            if symbol:
                params['symbol'] = symbol
            response = self.session.get_open_orders(**params)
            return response['result']['list'] if response['retCode'] == 0 else []
        except Exception as e:
            logger.error(f"Open orders error: {e}")
            return []

    async def get_order_history(self, symbol: str = None, limit: int = 500) -> List[Dict]:
        if not self._is_available():
            return []
        try:
            params = {"category": "spot", "limit": limit}
            if symbol:
                params['symbol'] = symbol
            response = self.session.get_order_history(**params)
            return response['result']['list'] if response['retCode'] == 0 else []
        except Exception as e:
            logger.error(f"Order history error: {e}")
            return []

    async def get_instrument_info(self, symbol: str) -> Dict:
        default = {'min_qty': 0.01, 'min_amt': 5, 'qty_step': 0.01, 'qty_decimals': 2, 'tick_size': 0.0001, 'price_decimals': 4}
        if not self._is_available():
            return default
        
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        
        try:
            response = self.session.get_instruments_info(category="spot", symbol=symbol)
            if response['retCode'] == 0 and response['result']['list']:
                info = response['result']['list'][0]
                lot = info.get('lotSizeFilter', {})
                price_f = info.get('priceFilter', {})
                qty_step = float(lot.get('qtyStep', '0.01'))
                result = {
                    'min_qty': float(lot.get('minOrderQty', 0.01)),
                    'min_amt': float(lot.get('minOrderAmt', 5)),
                    'qty_step': qty_step,
                    'qty_decimals': len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 2,
                    'tick_size': float(price_f.get('tickSize', '0.0001')),
                    'price_decimals': len(str(price_f.get('tickSize', '0.0001')).split('.')[-1]) if '.' in str(price_f.get('tickSize', '0.0001')) else 4
                }
                self._instrument_cache[symbol] = result
                return result
        except Exception as e:
            logger.error(f"Instrument info error: {e}")
        return default

    async def place_limit_buy(self, symbol: str, price: float, amount_usdt: float, is_auto: bool = True) -> Dict:
        if not self._is_available():
            return {'success': False, 'error': 'API не доступен'}
        
        info = await self.get_instrument_info(symbol)
        min_amt = info['min_amt']
        tick = info['tick_size']
        qty_step = info['qty_step']
        qty_dec = info['qty_decimals']
        
        rounded_price = round_by_tick(price, tick)
        
        if is_auto and amount_usdt < min_amt:
            amount_usdt = min_amt
        elif not is_auto and amount_usdt < min_amt:
            return {'success': False, 'error': f'Сумма {amount_usdt:.2f} USDT меньше минимальной {min_amt} USDT'}
        
        quantity = amount_usdt / rounded_price
        rounded_qty = round_quantity(quantity, qty_step, info['min_qty'])
        rounded_qty = round(rounded_qty, qty_dec)
        
        order_value = rounded_qty * rounded_price
        if order_value < min_amt:
            needed = min_amt / rounded_price
            needed = round_quantity(needed, qty_step, info['min_qty'])
            if needed * rounded_price >= min_amt:
                rounded_qty = needed
                order_value = rounded_qty * rounded_price
            else:
                return {'success': False, 'error': f'Минимальная сумма ордера: {min_amt} USDT'}
        
        try:
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Buy", orderType="Limit",
                qty=str(rounded_qty), price=str(rounded_price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'],
                       'quantity': float(rounded_qty), 'price': rounded_price, 'total_usdt': order_value}
            if response['retCode'] == 170131:
                return {'success': False, 'error': 'insufficient_balance'}
            return {'success': False, 'error': response.get('retMsg', 'Unknown error')}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def place_limit_sell(self, symbol: str, quantity: float, price: float) -> Dict:
        if not self._is_available():
            return {'success': False, 'error': 'API не доступен'}
        
        info = await self.get_instrument_info(symbol)
        min_qty = info['min_qty']
        min_amt = info['min_amt']
        tick = info['tick_size']
        qty_dec = info.get('qty_decimals', SELL_DECIMALS_FALLBACK)
        
        rounded_price = round_by_tick(price, tick)
        rounded_qty = round_sell_qty(quantity, qty_dec)
        
        if rounded_qty < min_qty:
            for d in range(qty_dec, 0, -1):
                test = math.floor(quantity * (10 ** d)) / (10 ** d)
                if test >= min_qty:
                    rounded_qty = test
                    break
            if rounded_qty < min_qty:
                return {'success': False, 'error': f'Минимальное количество: {min_qty}'}
        
        if rounded_qty <= 0:
            return {'success': False, 'error': f'Недостаточно средств'}
        
        order_value = rounded_qty * rounded_price
        if order_value < min_amt:
            return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt}
        
        try:
            response = self.session.place_order(
                category="spot", symbol=symbol, side="Sell", orderType="Limit",
                qty=str(rounded_qty), price=str(rounded_price), timeInForce="GTC"
            )
            if response['retCode'] == 0:
                return {'success': True, 'order_id': response['result']['orderId'],
                       'quantity': rounded_qty, 'price': rounded_price}
            if response['retCode'] == 170131:
                return {'success': False, 'error': 'insufficient_balance'}
            if response['retCode'] == 170140:
                return {'success': False, 'error': 'min_amount_error', 'min_amt': min_amt}
            return {'success': False, 'error': response.get('retMsg', 'Unknown error')}
        except Exception as e:
            return {'success': False, 'error': str(e)}

    async def cancel_all_sell_orders(self, symbol: str) -> Tuple[int, List[str]]:
        if not self._is_available():
            return 0, []
        try:
            orders = await self.get_open_orders(symbol)
            sell_orders = [o for o in orders if o.get('side') == 'Sell']
            cancelled = []
            for order in sell_orders:
                result = await self._cancel_order(symbol, order.get('orderId'))
                if result['success']:
                    cancelled.append(order.get('orderId'))
            return len(cancelled), cancelled
        except Exception as e:
            return 0, []

    async def _cancel_order(self, symbol: str, order_id: str) -> Dict:
        if not self._is_available():
            return {'success': False, 'error': 'API не доступен'}
        try:
            response = self.session.cancel_order(category="spot", symbol=symbol, orderId=order_id)
            return {'success': response['retCode'] == 0}
        except Exception as e:
            return {'success': False, 'error': str(e)}

# ============ ОСНОВНОЙ БОТ ============
class BotStates:
    SELECTING_ACTION, SET_SYMBOL, SET_SYMBOL_MANUAL, SET_AMOUNT, SET_PROFIT_PERCENT, SET_SCHEDULE_TIME, SET_FREQUENCY_HOURS, MANAGE_ORDERS, EDIT_ORDER_PRICE, MANUAL_BUY_PRICE, MANUAL_BUY_AMOUNT, MANUAL_ADD_PRICE, MANUAL_ADD_AMOUNT, EDIT_PURCHASE_SELECT, EDIT_PRICE, EDIT_AMOUNT, EDIT_DATE, DELETE_CONFIRM, SETTINGS_MENU, NOTIFICATION_SETTINGS_MENU, WAITING_ORDER_CHECK_INTERVAL, WAITING_ORDER_ID_TO_CANCEL, WAITING_PURCHASE_NOTIFY_TIME, AUTO_DCA_SETTINGS, SET_MANUAL_AMOUNT, LADDER_MENU, SET_LADDER_DEPTH, SET_LADDER_BASE_AMOUNT = range(28)

class FastDCABot:
    def __init__(self):
        self.db = Database()
        self.bybit = BybitClient(testnet=Config.testnet)
        self.authorized_user_id = self.db.get_authorized_user_id()
        self.scheduler_running = False
        self.background_tasks = []
        self._sell_check_task = None
        self._is_running = False
        self._api_was_working = False
        self._api_error_count = 0
        
        # Telegram Application
        request = HTTPXRequest(connect_timeout=60.0, read_timeout=60.0, write_timeout=60.0, pool_timeout=60.0)
        self.application = Application.builder().token(Config.telegram_token).request(request).build()
        self._setup_handlers()

    # ============ ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ============
    async def _check_user(self, update: Update) -> bool:
        user = update.effective_user
        if self.authorized_user_id is None:
            username = f"@{user.username}" if user.username else f"ID:{user.id}"
            if username == Config.authorized_user:
                self.authorized_user_id = user.id
                self.db.set_authorized_user_id(user.id)
                return True
        elif user.id == self.authorized_user_id:
            return True
        await update.message.reply_text("⛔ Доступ запрещен")
        return False

    async def _reset_state(self, context: ContextTypes.DEFAULT_TYPE):
        context.user_data.clear()

    def _main_keyboard(self):
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        return ReplyKeyboardMarkup([
            ["📊 Мой Портфель", "⏹ Остановить Авто DCA" if is_active else "🚀 Запустить Авто DCA"],
            ["💰 Ручная покупка (лимит)", "📈 Статистика DCA"],
            ["➕ Добавить покупку вручную", "✏️ Редактировать покупки"],
            ["⚙️ Настройки", "📝 Управление ордерами"],
            ["📋 Статус бота"]
        ], resize_keyboard=True)

    def _settings_keyboard(self):
        mode = self.db.get_trading_mode()
        return ReplyKeyboardMarkup([
            ["🪙 Выбор токена", "🚀 Настройки Авто DCA"],
            ["📊 Процент прибыли", "🪜 Лестница Мартингейла"],
            ["💵 Сумма для ручного ордера", "⚙️ Настройки отслеживания"],
            ["🔔 Уведомления о покупке", f"🌐 Режим: {'Демо' if mode == 'demo' else 'Обычный'}"],
            ["📤 Экспорт базы", "📥 Импорт базы"],
            ["🔙 Назад в меню"]
        ], resize_keyboard=True)

    def _cancel_keyboard(self):
        return ReplyKeyboardMarkup([["❌ Отмена"]], resize_keyboard=True)

    async def _safe_send(self, chat_id: int, text: str, parse_mode=None, reply_markup=None):
        try:
            return await self.application.bot.send_message(chat_id=chat_id, text=text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception as e:
            if "Can't parse entities" in str(e) or "Bad Request" in str(e):
                clean = text.replace('*', '').replace('`', '').replace('_', '')
                return await self.application.bot.send_message(chat_id=chat_id, text=clean, reply_markup=reply_markup)
            raise e

    def _calculate_next_purchase(self) -> datetime:
        schedule = self.db.get_setting('schedule_time', '05:00')
        hours = self.db.get_int('frequency_hours', '24')
        h, m = map(int, schedule.split(':'))
        now = get_moscow_time()
        next_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
        while next_time <= now:
            next_time += timedelta(hours=hours)
        return next_time

    # ============ КОМАНДЫ ============
    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        await self._reset_state(context)
        await self._safe_send(update.effective_user.id,
            f"👋 Привет! DCA Bybit Bot v{BOT_VERSION}\n🕐 {get_moscow_time().strftime('%H:%M')} МСК",
            parse_mode='Markdown', reply_markup=self._main_keyboard())

    async def settings_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        await self._reset_state(context)
        await update.message.reply_text("⚙️ *Настройки*", parse_mode='Markdown', reply_markup=self._settings_keyboard())
        return BotStates.SETTINGS_MENU

    async def back_to_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⚙️ *Настройки*", parse_mode='Markdown', reply_markup=self._settings_keyboard())
        return BotStates.SETTINGS_MENU

    async def back_to_main(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_state(context)
        await update.message.reply_text("Главное меню:", reply_markup=self._main_keyboard())
        return ConversationHandler.END

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_state(context)
        await update.message.reply_text("❌ Отменено", reply_markup=self._main_keyboard())
        return ConversationHandler.END

    # ============ ПОРТФЕЛЬ ============
    async def show_portfolio(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        coin = symbol.replace('USDT', '')
        
        price = await self.bybit.get_symbol_price(symbol)
        balance = await self.bybit.get_balance(coin)
        usdt = await self.bybit.get_balance('USDT')
        stats = self.db.get_dca_stats(symbol)
        
        msg = f"📊 *Портфель*\n\n"
        if usdt and 'equity' in usdt:
            msg += f"💵 USDT: `{usdt.get('available', 0):.2f}`\n"
        if balance and 'equity' in balance:
            equity = balance['equity']
            msg += f"🪙 {coin}: `{format_quantity(equity, 5)}`\n"
            if stats and price:
                pnl = (price - stats['avg_price']) * equity
                pnl_pct = (pnl / stats['total_usdt'] * 100) if stats['total_usdt'] > 0 else 0
                msg += f"📈 PnL: `{pnl:+.2f}` USDT (`{pnl_pct:+.2f}%`)\n"
        await update.message.reply_text(msg, parse_mode='Markdown')

    # ============ DCA СТАТИСТИКА ============
    async def show_dca_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        stats = self.db.get_dca_stats(symbol)
        if not stats:
            await update.message.reply_text("📈 Покупок пока нет", parse_mode='Markdown')
            return
        
        price = await self.bybit.get_symbol_price(symbol)
        profit_pct = self.db.get_float('profit_percent', '5')
        
        msg = f"📊 *Статистика DCA*\n"
        msg += f"🪙 {symbol}\n"
        msg += f"📊 Покупок: `{stats['total_purchases']}`\n"
        msg += f"💵 Вложено: `{stats['total_usdt']:.2f}` USDT\n"
        msg += f"📈 Средняя цена: `{format_price(stats['avg_price'], 4)}`\n"
        if price:
            drop = calculate_drop(price, stats['avg_price'])
            msg += f"📉 Падение: `{drop:.1f}%`\n"
            msg += f"💰 Текущая: `{format_price(price, 4)}`\n"
        
        target = stats['avg_price'] * (1 + profit_pct / 100)
        msg += f"\n🎯 Цель {profit_pct}%: `{format_price(target, 4)}` USDT"
        await update.message.reply_text(msg, parse_mode='Markdown')

    # ============ УПРАВЛЕНИЕ DCA ============
    async def toggle_dca(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        
        is_active = self.db.get_setting('dca_active', 'false') == 'true'
        if is_active:
            self.db.set_setting('dca_active', 'false')
            if self._sell_check_task and not self._sell_check_task.done():
                self._sell_check_task.cancel()
            await update.message.reply_text("⏹ DCA ОСТАНОВЛЕН", parse_mode='Markdown', reply_markup=self._main_keyboard())
            return
        
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        price = await self.bybit.get_symbol_price(symbol)
        if not price:
            await update.message.reply_text("❌ Не удалось получить цену")
            return
        
        self.db.set_setting('dca_active', 'true')
        
        # Проверяем и создаем ордер на продажу если есть покупки
        stats = self.db.get_dca_stats(symbol)
        if stats and stats['total_quantity'] > 0:
            await self._create_sell_order(symbol)
        
        # Запускаем проверку ордера
        if not self._sell_check_task or self._sell_check_task.done():
            self._sell_check_task = asyncio.create_task(self._sell_check_loop(symbol))
        
        await update.message.reply_text(f"✅ DCA ЗАПУЩЕН\n🪙 {symbol}", parse_mode='Markdown', reply_markup=self._main_keyboard())

    async def _create_sell_order(self, symbol: str):
        stats = self.db.get_dca_stats(symbol)
        if not stats:
            return
        
        coin = symbol.replace('USDT', '')
        balance = await self.bybit.get_balance(coin)
        if not balance or balance.get('equity', 0) <= 0:
            return
        
        profit_pct = self.db.get_float('profit_percent', '5')
        target_price = stats['avg_price'] * (1 + profit_pct / 100)
        qty = balance['equity']
        
        result = await self.bybit.place_limit_sell(symbol, qty, target_price)
        if result['success']:
            self.db.add_sell_order(symbol, result['order_id'], result['quantity'], result['price'], profit_pct)
            await self._safe_send(self.authorized_user_id,
                f"✅ Ордер на продажу создан!\n🪙 {symbol}\n💰 {format_price(result['price'], 4)} USDT\n📈 +{profit_pct}%",
                parse_mode='Markdown')

    async def _sell_check_loop(self, symbol: str):
        while True:
            try:
                await asyncio.sleep(3600)
                if self.db.get_setting('dca_active', 'false') != 'true':
                    break
                
                open_orders = await self.bybit.get_open_orders(symbol)
                sell_exists = any(o.get('side') == 'Sell' for o in open_orders)
                
                if not sell_exists:
                    stats = self.db.get_dca_stats(symbol)
                    if stats and stats['total_quantity'] > 0:
                        await self._create_sell_order(symbol)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Sell check error: {e}")
                await asyncio.sleep(60)

    # ============ РУЧНАЯ ПОКУПКА ============
    async def manual_buy_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        price = await self.bybit.get_symbol_price(symbol)
        if not price:
            await update.message.reply_text("❌ Не удалось получить цену")
            return
        
        context.user_data['manual_symbol'] = symbol
        await update.message.reply_text(
            f"💰 Текущая цена {symbol}: `{format_price(price, 4)}` USDT\n\nВведите цену лимитного ордера:",
            parse_mode='Markdown', reply_markup=self._cancel_keyboard()
        )
        return BotStates.MANUAL_BUY_PRICE

    async def manual_buy_price(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel(update, context)
        try:
            price = float(text.replace(',', '.'))
            if price <= 0:
                raise ValueError
            context.user_data['manual_price'] = price
            amount = self.db.get_manual_amount()
            await update.message.reply_text(
                f"💰 Цена: `{format_price(price, 4)}` USDT\n\nВведите сумму в USDT:\n*Рекомендуемая: {amount:.2f}*",
                parse_mode='Markdown', reply_markup=self._cancel_keyboard()
            )
            return BotStates.MANUAL_BUY_AMOUNT
        except:
            await update.message.reply_text("❌ Введите корректную цену", reply_markup=self._cancel_keyboard())
            return BotStates.MANUAL_BUY_PRICE

    async def manual_buy_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.cancel(update, context)
        try:
            amount = float(text.replace(',', '.'))
            if amount < 1.1:
                raise ValueError
            symbol = context.user_data.get('manual_symbol', DEFAULT_SYMBOL)
            price = context.user_data.get('manual_price')
            if not price:
                raise ValueError
            
            result = await self.bybit.place_limit_buy(symbol, price, amount, is_auto=False)
            if result['success']:
                profit_pct = self.db.get_float('profit_percent', '5')
                purchase_id = self.db.add_purchase(symbol, result['total_usdt'], result['price'], result['quantity'],
                                                  order_id=result.get('order_id'))
                if purchase_id:
                    await update.message.reply_text(
                        f"✅ Ордер создан!\n💰 Цена: `{format_price(result['price'], 4)}`\n📊 {format_quantity(result['quantity'], 5)} {symbol.replace('USDT', '')}",
                        parse_mode='Markdown', reply_markup=self._main_keyboard()
                    )
                    # Создаем ордер на продажу
                    await self._create_sell_order(symbol)
                else:
                    await update.message.reply_text("❌ Ошибка сохранения", reply_markup=self._main_keyboard())
            else:
                await update.message.reply_text(f"❌ Ошибка: {result.get('error')}", reply_markup=self._main_keyboard())
            return ConversationHandler.END
        except ValueError as e:
            await update.message.reply_text(f"❌ {str(e)}", reply_markup=self._cancel_keyboard())
            return BotStates.MANUAL_BUY_AMOUNT

    # ============ НАСТРОЙКИ ============
    async def set_profit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(f"📊 Введите процент прибыли (текущий: {self.db.get_setting('profit_percent', '5')}%):", reply_markup=self._cancel_keyboard())
        return BotStates.SET_PROFIT_PERCENT

    async def set_profit_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.back_to_settings(update, context)
        try:
            pct = float(text)
            if pct < 0.1:
                raise ValueError
            self.db.set_setting('profit_percent', str(pct))
            await update.message.reply_text(f"✅ Процент изменен на {pct}%", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Некорректное значение", reply_markup=self._cancel_keyboard())
            return BotStates.SET_PROFIT_PERCENT

    async def set_manual_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_manual_amount()
        await update.message.reply_text(f"💵 Текущая сумма: `{current}` USDT\nВведите новую сумму (мин 1.1):", parse_mode='Markdown', reply_markup=self._cancel_keyboard())
        return BotStates.SET_MANUAL_AMOUNT

    async def set_manual_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.back_to_settings(update, context)
        try:
            amount = float(text)
            if amount < 1.1:
                raise ValueError
            self.db.set_setting('manual_amount', str(amount))
            await update.message.reply_text(f"✅ Сумма изменена на {amount} USDT", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Минимум 1.1 USDT", reply_markup=self._cancel_keyboard())
            return BotStates.SET_MANUAL_AMOUNT

    # ============ НАСТРОЙКИ АВТО DCA ============
    async def auto_dca_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_state(context)
        await update.message.reply_text(
            f"🚀 *Настройки Авто DCA*\n"
            f"💵 Сумма: `{self.db.get_setting('invest_amount', '5.0')}` USDT\n"
            f"⏰ Время: `{self.db.get_setting('schedule_time', '05:00')}`\n"
            f"🔄 Частота: `{self.db.get_setting('frequency_hours', '24')}` ч",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup([
                [f"💵 Сумма покупки авто ({self.db.get_setting('invest_amount', '5.0')} USDT)"],
                [f"⏰ Время покупки ({self.db.get_setting('schedule_time', '05:00')})"],
                [f"🔄 Частота покупки ({self.db.get_setting('frequency_hours', '24')} ч)"],
                ["🔙 Назад в настройки"]
            ], resize_keyboard=True)
        )
        return BotStates.AUTO_DCA_SETTINGS

    async def set_auto_amount(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💵 Введите сумму (мин 5 USDT):", reply_markup=self._cancel_keyboard())
        return BotStates.SET_AMOUNT

    async def set_auto_amount_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.auto_dca_menu(update, context)
        try:
            amount = float(text)
            if amount < 5:
                raise ValueError
            self.db.set_setting('invest_amount', str(amount))
            ladder = self.db.get_ladder_settings()
            ladder['base_amount'] = amount
            ladder['max_amount'] = amount * 3
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Сумма изменена на {amount} USDT", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Минимум 5 USDT", reply_markup=self._cancel_keyboard())
            return BotStates.SET_AMOUNT

    async def set_auto_time(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏰ Введите время (ЧЧ:ММ):", reply_markup=self._cancel_keyboard())
        return BotStates.SET_SCHEDULE_TIME

    async def set_auto_time_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.auto_dca_menu(update, context)
        try:
            datetime.strptime(text, "%H:%M")
            self.db.set_setting('schedule_time', text)
            if self.db.get_setting('dca_active', 'false') == 'true':
                self.db.set_setting('next_dca_purchase_time', self._calculate_next_purchase().isoformat())
            await update.message.reply_text(f"✅ Время изменено на {text}", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Используйте ЧЧ:ММ", reply_markup=self._cancel_keyboard())
            return BotStates.SET_SCHEDULE_TIME

    async def set_auto_frequency(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("🔄 Введите частоту (часы, 1-720):", reply_markup=self._cancel_keyboard())
        return BotStates.SET_FREQUENCY_HOURS

    async def set_auto_frequency_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.auto_dca_menu(update, context)
        try:
            hours = int(text)
            if hours < 1 or hours > 720:
                raise ValueError
            self.db.set_setting('frequency_hours', str(hours))
            if self.db.get_setting('dca_active', 'false') == 'true':
                self.db.set_setting('next_dca_purchase_time', self._calculate_next_purchase().isoformat())
            await update.message.reply_text(f"✅ Частота изменена на {hours} ч", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Введите число от 1 до 720", reply_markup=self._cancel_keyboard())
            return BotStates.SET_FREQUENCY_HOURS

    # ============ ОТСЛЕЖИВАНИЕ ============
    async def tracking_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_state(context)
        await update.message.reply_text(
            f"⚙️ *Настройки отслеживания*\n"
            f"📋 Ордера: {'✅ Вкл' if self.db.get_order_execution_notify() else '⏹ Выкл'}\n"
            f"💰 Продажи: {'✅ Вкл' if self.db.get_sell_tracking_enabled() else '⏹ Выкл'}\n"
            f"⏱ Интервал: `{self.db.get_order_check_interval()}` мин",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup([
                [f"{'✅ Отслеживание ордеров Вкл' if self.db.get_order_execution_notify() else '❌ Отслеживание ордеров Выкл'}"],
                [f"{'💰 Отслеживание продаж Вкл' if self.db.get_sell_tracking_enabled() else '⏳ Отслеживание продаж Выкл'}"],
                [f"⏱ Интервал проверки Ордеров {self.db.get_order_check_interval()} мин"],
                ["🔙 Назад в настройки"]
            ], resize_keyboard=True)
        )
        return BotStates.NOTIFICATION_SETTINGS_MENU

    async def toggle_order_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        new = not self.db.get_order_execution_notify()
        self.db.set_bool('order_execution_notify', new)
        return await self.tracking_settings(update, context)

    async def toggle_sell_tracking(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        new = not self.db.get_sell_tracking_enabled()
        self.db.set_bool('sell_tracking_enabled', new)
        return await self.tracking_settings(update, context)

    async def set_tracking_interval(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏱ Введите интервал (5-1440 минут):", reply_markup=self._cancel_keyboard())
        return BotStates.WAITING_ORDER_CHECK_INTERVAL

    async def set_tracking_interval_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.tracking_settings(update, context)
        try:
            minutes = int(text)
            if minutes < 5 or minutes > 1440:
                raise ValueError
            self.db.set_setting('order_check_interval_minutes', str(minutes))
            await update.message.reply_text(f"✅ Интервал изменен на {minutes} минут", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Введите число от 5 до 1440", reply_markup=self._cancel_keyboard())
            return BotStates.WAITING_ORDER_CHECK_INTERVAL

    # ============ ЛЕСТНИЦА МАРТИНГЕЙЛА ============
    async def ladder_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await self._reset_state(context)
        await update.message.reply_text(
            "🪜 *Лестница Мартингейла*\n\n"
            "Стратегия: при падении на 1% докупка с ростом суммы",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup([
                ["📉 Глубина просадки (%)", "💵 Базовая сумма"],
                ["📋 Текущие настройки", "🔄 Сбросить лестницу"],
                ["🔙 Назад в настройки"]
            ], resize_keyboard=True)
        )
        return BotStates.LADDER_MENU

    async def show_ladder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        ladder = self.db.get_ladder_settings(symbol)
        price = await self.bybit.get_symbol_price(symbol)
        stats = self.db.get_dca_stats(symbol)
        
        msg = f"🪜 *Настройки лестницы*\n"
        msg += f"📉 Глубина: `{ladder['max_depth']}%`\n"
        msg += f"💵 Базовая: `{ladder['base_amount']}` USDT\n"
        msg += f"💰 Макс: `{ladder['max_amount']}` USDT\n"
        if stats and price:
            drop = calculate_drop(price, stats['avg_price'])
            msg += f"📊 Текущее падение: `{drop:.1f}%`"
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=self._settings_keyboard())
        return BotStates.LADDER_MENU

    async def set_ladder_depth(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("📉 Введите глубину (30-95%):", reply_markup=self._cancel_keyboard())
        return BotStates.SET_LADDER_DEPTH

    async def set_ladder_depth_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.ladder_menu(update, context)
        try:
            depth = float(text)
            if depth < 30 or depth > 95:
                raise ValueError
            ladder = self.db.get_ladder_settings()
            ladder['max_depth'] = depth
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Глубина: {depth}%", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Введите число 30-95", reply_markup=self._cancel_keyboard())
            return BotStates.SET_LADDER_DEPTH

    async def set_ladder_base(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("💵 Введите базовую сумму (мин 5 USDT):", reply_markup=self._cancel_keyboard())
        return BotStates.SET_LADDER_BASE_AMOUNT

    async def set_ladder_base_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text.strip()
        if text == "❌ Отмена":
            return await self.ladder_menu(update, context)
        try:
            amount = float(text)
            if amount < 5:
                raise ValueError
            ladder = self.db.get_ladder_settings()
            ladder['base_amount'] = amount
            ladder['max_amount'] = amount * 3
            self.db.save_ladder_settings(ladder)
            await update.message.reply_text(f"✅ Базовая сумма: {amount} USDT\n💰 Макс: {amount * 3} USDT", reply_markup=self._settings_keyboard())
            return BotStates.SETTINGS_MENU
        except:
            await update.message.reply_text("❌ Минимум 5 USDT", reply_markup=self._cancel_keyboard())
            return BotStates.SET_LADDER_BASE_AMOUNT

    async def reset_ladder(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        self.db.clear_all_purchases(symbol)
        await update.message.reply_text("🔄 Лестница сброшена", reply_markup=self._settings_keyboard())
        return BotStates.SETTINGS_MENU

    # ============ ЭКСПОРТ/ИМПОРТ ============
    async def export_db(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("⏳ Экспортирую...")
        try:
            with open(DB_EXPORT_FILE, 'w', encoding='utf-8') as f:
                json.dump({'export_date': get_moscow_time_naive().isoformat(), 'version': BOT_VERSION}, f)
            with open(DB_EXPORT_FILE, 'rb') as f:
                await update.message.reply_document(document=InputFile(f, filename=DB_EXPORT_FILE))
        except Exception as e:
            await update.message.reply_text(f"❌ Ошибка: {e}")

    # ============ УПРАВЛЕНИЕ ОРДЕРАМИ ============
    async def orders_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        orders = await self.bybit.get_open_orders(symbol)
        sell = [o for o in orders if o.get('side') == 'Sell']
        buy = [o for o in orders if o.get('side') == 'Buy']
        await update.message.reply_text(
            f"📝 *Ордера {symbol}*\n🔴 Sell: {len(sell)}\n🟢 Buy: {len(buy)}",
            parse_mode='Markdown',
            reply_markup=ReplyKeyboardMarkup([
                ["📋 Список открытых ордеров", "❌ Удалить ордер"],
                ["🔙 Назад в меню"]
            ], resize_keyboard=True)
        )
        return BotStates.MANAGE_ORDERS

    async def show_orders(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
        orders = await self.bybit.get_open_orders(symbol)
        if not orders:
            await update.message.reply_text("📭 Нет открытых ордеров", reply_markup=self._main_keyboard())
            return
        
        msg = f"📋 *Открытые ордера*\n"
        for o in orders[:20]:
            side = o.get('side', '')
            price = float(o.get('price', 0))
            qty = float(o.get('qty', 0))
            msg += f"{'🔴' if side == 'Sell' else '🟢'} {format_quantity(qty, 5)} @ {format_price(price, 4)}\n"
        await update.message.reply_text(msg, parse_mode='Markdown', reply_markup=self._main_keyboard())

    # ============ ФОНОВЫЕ ЗАДАЧИ ============
    async def _scheduler_loop(self):
        logger.info("Scheduler started")
        while self.scheduler_running:
            try:
                await asyncio.sleep(30)
                if self.db.get_setting('dca_active', 'false') != 'true':
                    continue
                
                next_str = self.db.get_setting('next_dca_purchase_time', '')
                if not next_str:
                    self.db.set_setting('next_dca_purchase_time', self._calculate_next_purchase().isoformat())
                    continue
                
                next_time = datetime.fromisoformat(next_str)
                if get_moscow_time() >= next_time:
                    symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                    profit_pct = self.db.get_float('profit_percent', '5')
                    
                    # Выполняем покупку
                    price = await self.bybit.get_symbol_price(symbol)
                    if price:
                        stats = self.db.get_dca_stats(symbol)
                        base = self.db.get_float('invest_amount', '5.0')
                        amount = base
                        drop = 0
                        if stats and stats['avg_price'] > 0:
                            drop = calculate_drop(price, stats['avg_price'])
                            if drop > 0:
                                ladder = self.db.get_ladder_settings(symbol)
                                amount = base + (ladder['max_amount'] - base) * min(drop / ladder['max_depth'], 1)
                        
                        result = await self.bybit.place_limit_buy(symbol, price, amount, is_auto=True)
                        if result['success']:
                            self.db.add_purchase(symbol, result['total_usdt'], result['price'], result['quantity'],
                                               drop_percent=drop, order_id=result.get('order_id'))
                            await self._create_sell_order(symbol)
                            await self._safe_send(self.authorized_user_id,
                                f"🪜 *Авто DCA*\n🪙 {symbol}\n💵 {result['total_usdt']:.2f} USDT\n📉 {drop:.1f}%",
                                parse_mode='Markdown')
                    
                    # Обновляем время
                    hours = self.db.get_int('frequency_hours', '24')
                    next_time += timedelta(hours=hours)
                    while next_time <= get_moscow_time():
                        next_time += timedelta(hours=hours)
                    self.db.set_setting('next_dca_purchase_time', next_time.isoformat())
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)

    async def _order_checker_loop(self):
        logger.info("Order checker started")
        while self.scheduler_running:
            try:
                interval = self.db.get_order_check_interval() * 60
                if self.db.get_order_execution_notify():
                    symbol = self.db.get_setting('symbol', DEFAULT_SYMBOL)
                    # Проверяем новые исполненные ордера
                    orders = await self.bybit.get_order_history(symbol, limit=100)
                    for order in orders:
                        if order.get('orderStatus') in ('Filled', 'PartiallyFilled') and order.get('side') == 'Buy':
                            order_id = order.get('orderId')
                            if not self.db.is_order_already_added(order_id):
                                price = float(order.get('avgPrice', 0) or order.get('price', 0))
                                qty = float(order.get('cumExecQty', 0) or order.get('qty', 0))
                                if price > 0 and qty > 0:
                                    amount = price * qty
                                    self.db.add_executed_order(order_id, symbol, price, qty, amount)
                                    await self._safe_send(self.authorized_user_id,
                                        f"✅ *Исполнен ордер*\n🪙 {symbol}\n💰 {format_price(price, 4)}\n📊 {format_quantity(qty, 5)}",
                                        parse_mode='Markdown')
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Order checker error: {e}")
                await asyncio.sleep(60)

    # ============ НАСТРОЙКА ХЭНДЛЕРОВ ============
    def _setup_handlers(self):
        app = self.application
        
        # Команды
        app.add_handler(CommandHandler("start", self.start))
        
        # Основные кнопки
        app.add_handler(MessageHandler(filters.Regex("^📊 Мой Портфель$"), self.show_portfolio))
        app.add_handler(MessageHandler(filters.Regex("^(🚀 Запустить Авто DCA|⏹ Остановить Авто DCA)$"), self.toggle_dca))
        app.add_handler(MessageHandler(filters.Regex("^📈 Статистика DCA$"), self.show_dca_stats))
        app.add_handler(MessageHandler(filters.Regex("^📝 Управление ордерами$"), self.orders_menu))
        app.add_handler(MessageHandler(filters.Regex("^📋 Список открытых ордеров$"), self.show_orders))
        app.add_handler(MessageHandler(filters.Regex("^📤 Экспорт базы$"), self.export_db))
        
        # Conversation: Главное меню -> Настройки
        settings_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^⚙️ Настройки$"), self.settings_menu)],
            states={
                BotStates.SETTINGS_MENU: [
                    MessageHandler(filters.Regex("^🚀 Настройки Авто DCA$"), self.auto_dca_menu),
                    MessageHandler(filters.Regex("^📊 Процент прибыли$"), self.set_profit),
                    MessageHandler(filters.Regex("^💵 Сумма для ручного ордера$"), self.set_manual_amount),
                    MessageHandler(filters.Regex("^⚙️ Настройки отслеживания$"), self.tracking_settings),
                    MessageHandler(filters.Regex("^🪜 Лестница Мартингейла$"), self.ladder_menu),
                    MessageHandler(filters.Regex("^🌐 Режим: (Обычный|Демо)$"), self._toggle_mode),
                    MessageHandler(filters.Regex("^🔙 Назад в меню$"), self.back_to_main),
                ],
                BotStates.SET_PROFIT_PERCENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_profit_done)],
                BotStates.SET_MANUAL_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_manual_amount_done)],
                BotStates.AUTO_DCA_SETTINGS: [
                    MessageHandler(filters.Regex("^💵 Сумма покупки авто"), self.set_auto_amount),
                    MessageHandler(filters.Regex("^⏰ Время покупки"), self.set_auto_time),
                    MessageHandler(filters.Regex("^🔄 Частота покупки"), self.set_auto_frequency),
                    MessageHandler(filters.Regex("^🔙 Назад в настройки$"), self.back_to_settings),
                ],
                BotStates.SET_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_auto_amount_done)],
                BotStates.SET_SCHEDULE_TIME: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_auto_time_done)],
                BotStates.SET_FREQUENCY_HOURS: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_auto_frequency_done)],
                BotStates.NOTIFICATION_SETTINGS_MENU: [
                    MessageHandler(filters.Regex("^(✅ Отслеживание ордеров Вкл|❌ Отслеживание ордеров Выкл)$"), self.toggle_order_tracking),
                    MessageHandler(filters.Regex("^(💰 Отслеживание продаж Вкл|⏳ Отслеживание продаж Выкл)$"), self.toggle_sell_tracking),
                    MessageHandler(filters.Regex("^⏱ Интервал проверки Ордеров"), self.set_tracking_interval),
                    MessageHandler(filters.Regex("^🔙 Назад в настройки$"), self.back_to_settings),
                ],
                BotStates.WAITING_ORDER_CHECK_INTERVAL: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_tracking_interval_done)],
                BotStates.LADDER_MENU: [
                    MessageHandler(filters.Regex("^📉 Глубина просадки \(%\)$"), self.set_ladder_depth),
                    MessageHandler(filters.Regex("^💵 Базовая сумма$"), self.set_ladder_base),
                    MessageHandler(filters.Regex("^📋 Текущие настройки$"), self.show_ladder),
                    MessageHandler(filters.Regex("^🔄 Сбросить лестницу$"), self.reset_ladder),
                    MessageHandler(filters.Regex("^🔙 Назад в настройки$"), self.back_to_settings),
                ],
                BotStates.SET_LADDER_DEPTH: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_depth_done)],
                BotStates.SET_LADDER_BASE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.set_ladder_base_done)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="settings_conv"
        )
        app.add_handler(settings_conv)
        
        # Conversation: Ручная покупка
        manual_conv = ConversationHandler(
            entry_points=[MessageHandler(filters.Regex("^💰 Ручная покупка \(лимит\)$"), self.manual_buy_start)],
            states={
                BotStates.MANUAL_BUY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_price)],
                BotStates.MANUAL_BUY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, self.manual_buy_amount)],
            },
            fallbacks=[CommandHandler("cancel", self.cancel)],
            name="manual_conv"
        )
        app.add_handler(manual_conv)
        
        # Кнопка "Отмена"
        app.add_handler(MessageHandler(filters.Regex("^❌ Отмена$"), self.cancel))
        
        # Обработка неизвестных команд
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._handle_unknown))

    async def _handle_unknown(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._check_user(update):
            return
        await self._reset_state(context)
        await update.message.reply_text("Используйте кнопки меню", reply_markup=self._main_keyboard())

    async def _toggle_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        current = self.db.get_trading_mode()
        new = 'demo' if current == 'real' else 'real'
        self.db.set_setting('trading_mode', new)
        self.bybit = BybitClient(testnet=(new == 'demo'))
        await update.message.reply_text(f"✅ Режим: {'Демо' if new == 'demo' else 'Обычный'}", reply_markup=self._settings_keyboard())
        return BotStates.SETTINGS_MENU

    # ============ ЗАПУСК ============
    async def _post_init(self, app: Application):
        if self._is_running:
            return
        self._is_running = True
        self.scheduler_running = True
        
        # Запускаем фоновые задачи
        self.background_tasks = [
            asyncio.create_task(self._scheduler_loop()),
            asyncio.create_task(self._order_checker_loop()),
        ]
        
        logger.info("Bot started with background tasks")

    def run(self):
        if self._is_running:
            return
        
        print(f"\n{Fore.CYAN}{'='*60}")
        print(f"{Fore.CYAN}🚀 DCA BOT v{BOT_VERSION}")
        print(f"{Fore.CYAN}{'='*60}")
        print(f"👤 {Config.authorized_user}")
        print(f"💾 dca_bot.db")
        print(f"🕐 {get_moscow_time().strftime('%H:%M')}")
        print(f"{Fore.CYAN}{'='*60}\n")
        
        self.application.post_init = self._post_init
        self.application.run_polling(allowed_updates=Update.ALL_TYPES, poll_interval=1.0, timeout=60)


if __name__ == "__main__":
    bot = FastDCABot()
    bot.run()