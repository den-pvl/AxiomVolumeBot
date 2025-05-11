# database.py
import sqlite3
import logging
import threading # Для блокировки доступа к БД из разных потоков/задач asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple

# Импортируем имя БД из конфига
from config import DATABASE_NAME

# Получаем логгер для этого модуля
logger = logging.getLogger(__name__)

# Создаем объект блокировки для потокобезопасного доступа к БД
# SQLite по умолчанию может иметь проблемы с многопоточностью без правильной настройки
# или сериализации доступа. Эта блокировка - простой способ это обеспечить.
db_lock = threading.Lock()

def get_db_connection() -> sqlite3.Connection:
    """Устанавливает соединение с базой данных SQLite."""
    conn = sqlite3.connect(DATABASE_NAME, timeout=10) # Увеличим таймаут на всякий случай
    # Устанавливаем ROW factory для удобного доступа к данным по именам колонок
    conn.row_factory = sqlite3.Row
    return conn

def create_tables():
    """Создает таблицу 'coins' в БД, если она еще не существует."""
    with db_lock: # Захватываем блокировку перед доступом к БД
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            # SQL запрос для создания таблицы
            # CA - Contract Address, уникальный ключ
            # Tic - Ticker
            # age_str - возраст монеты в строковом виде (как спарсили)
            # mc - Market Cap (число)
            # l - Liquidity (число)
            # v - Volume (число)
            # ath_seconds - возраст ATH в секундах (число)
            # status - текущий статус обработки монеты ("Processing", "Buy Success!", "Sell Success!")
            # updated_at - время последнего обновления записи (текст ISO формат)
            # group_message_id - ID сообщения, отправленного в группу для этой монеты (для reply)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS coins (
                    ca TEXT PRIMARY KEY,
                    tic TEXT,
                    age_str TEXT,
                    mc INTEGER,
                    l INTEGER,
                    v INTEGER,
                    ath_seconds INTEGER,
                    status TEXT,
                    updated_at TEXT NOT NULL,
                    group_message_id INTEGER 
                )
            """)
            conn.commit()
            logger.info(f"Таблица 'coins' успешно проверена/создана в БД '{DATABASE_NAME}'.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при создании таблицы 'coins': {e}")
            # В реальном приложении здесь можно было бы вызвать sys.exit() или обработать иначе
        finally:
            if conn:
                conn.close()

def clear_all_coin_data():
    """Удаляет все данные из таблицы 'coins'."""
    with db_lock:
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("DELETE FROM coins")
            conn.commit()
            logger.info("Все данные из таблицы 'coins' были удалены.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при удалении данных из таблицы 'coins': {e}")
        finally:
            if conn:
                conn.close()

def upsert_coin(coin_data: Dict[str, Any]):
    """
    Вставляет новую монету или обновляет существующую в таблице 'coins'.
    Ожидает словарь coin_data со следующими ключами:
    'ca', 'tic', 'age_str', 'mc', 'l', 'v', 
    'ath_seconds' (может быть None), 
    'status', 
    'group_message_id' (может быть None).
    'updated_at' будет установлен автоматически.
    """
    with db_lock:
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Текущее время в формате ISO для 'updated_at'
            updated_at_iso = datetime.now().isoformat()

            # SQL-запрос для UPSERT (INSERT OR REPLACE или INSERT ON CONFLICT)
            # Используем INSERT ... ON CONFLICT для SQLite
            sql = """
                INSERT INTO coins (ca, tic, age_str, mc, l, v, ath_seconds, status, updated_at, group_message_id)
                VALUES (:ca, :tic, :age_str, :mc, :l, :v, :ath_seconds, :status, :updated_at, :group_message_id)
                ON CONFLICT(ca) DO UPDATE SET
                    tic=excluded.tic,
                    age_str=excluded.age_str,
                    mc=excluded.mc,
                    l=excluded.l,
                    v=excluded.v,
                    ath_seconds=excluded.ath_seconds,
                    status=excluded.status,
                    updated_at=excluded.updated_at,
                    group_message_id= CASE 
                                        WHEN excluded.group_message_id IS NOT NULL THEN excluded.group_message_id
                                        ELSE coins.group_message_id 
                                     END; 
            """
            # При обновлении group_message_id, мы не хотим его затирать на None, если он уже был установлен
            # а новое значение None. Поэтому используем CASE.
            
            # Подготовка данных для запроса, убедимся что все ключи есть
            params = {
                "ca": coin_data.get("ca"),
                "tic": coin_data.get("tic"),
                "age_str": coin_data.get("age_str"),
                "mc": coin_data.get("mc"),
                "l": coin_data.get("l"),
                "v": coin_data.get("v"),
                "ath_seconds": coin_data.get("ath_seconds"), # Может быть None
                "status": coin_data.get("status"),
                "updated_at": updated_at_iso,
                "group_message_id": coin_data.get("group_message_id") # Может быть None
            }

            cursor.execute(sql, params)
            conn.commit()
            logger.debug(f"Данные для монеты {params['ca']} успешно записаны/обновлены (Статус: {params['status']}).")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при UPSERT для монеты {coin_data.get('ca', 'N/A')}: {e}")
            logger.error(f"Данные, которые пытались записать: {coin_data}")

        finally:
            if conn:
                conn.close()

def get_coin_status(ca: str) -> Optional[str]:
    """Возвращает статус монеты по её CA или None, если монета не найдена."""
    with db_lock:
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT status FROM coins WHERE ca = ?", (ca,))
            row = cursor.fetchone()
            if row:
                return row["status"]
            return None
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении статуса для CA {ca}: {e}")
            return None
        finally:
            if conn:
                conn.close()

def get_coin_details(ca: str) -> Optional[Dict[str, Any]]:
    """Возвращает все детали монеты по её CA или None, если монета не найдена."""
    with db_lock:
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM coins WHERE ca = ?", (ca,))
            row = cursor.fetchone()
            if row:
                # Преобразуем sqlite3.Row в обычный dict для удобства
                return dict(row)
            return None
        except sqlite3.Error as e:
            logger.error(f"Ошибка при получении деталей для CA {ca}: {e}")
            return None
        finally:
            if conn:
                conn.close()

def update_coin_status(ca: str, new_status: str):
    """Обновляет статус конкретной монеты и время updated_at."""
    with db_lock:
        conn = None
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            updated_at_iso = datetime.now().isoformat()
            cursor.execute(
                "UPDATE coins SET status = ?, updated_at = ? WHERE ca = ?",
                (new_status, updated_at_iso, ca)
            )
            conn.commit()
            if cursor.rowcount > 0:
                logger.info(f"Статус для CA {ca} обновлен на '{new_status}'.")
            else:
                logger.warning(f"Попытка обновить статус для несуществующего CA {ca} на '{new_status}'.")
        except sqlite3.Error as e:
            logger.error(f"Ошибка при обновлении статуса для CA {ca} на '{new_status}': {e}")
        finally:
            if conn:
                conn.close()


if __name__ == '__main__':
    # Этот блок выполнится, только если запустить database.py напрямую
    # Используется для инициализации и тестирования
    from config import setup_logging
    setup_logging() # Настраиваем логирование из конфига

    logger.info("Тестирование модуля database.py...")
    create_tables() # Убедимся, что таблица создана

    # Пример очистки данных (раскомментируйте для теста)
    # clear_all_coin_data()

    # Пример UPSERT
    test_coin1 = {
        "ca": "CA_TEST_001", "tic": "TEST1", "age_str": "10m", "mc": 100000, "l": 5000, "v": 1000,
        "ath_seconds": 30, "status": "Processing", "group_message_id": None
    }
    upsert_coin(test_coin1)

    test_coin2 = {
        "ca": "CA_TEST_002", "tic": "TEST2", "age_str": "1h", "mc": 250000, "l": 15000, "v": 7000,
        "ath_seconds": 3600, "status": "Processing", "group_message_id": 12345
    }
    upsert_coin(test_coin2)

    # Пример получения статуса
    status1 = get_coin_status("CA_TEST_001")
    logger.info(f"Статус для CA_TEST_001: {status1}")
    status_non_existent = get_coin_status("NON_EXISTENT_CA")
    logger.info(f"Статус для NON_EXISTENT_CA: {status_non_existent}")

    # Пример получения деталей
    details1 = get_coin_details("CA_TEST_001")
    logger.info(f"Детали для CA_TEST_001: {details1}")

    # Пример обновления статуса
    update_coin_status("CA_TEST_001", "Buy Success!")
    status1_updated = get_coin_status("CA_TEST_001")
    logger.info(f"Обновленный статус для CA_TEST_001: {status1_updated}")
    
    # Пример обновления с новым group_message_id (он должен сохраниться, если был)
    test_coin2_update = {
        "ca": "CA_TEST_002", "tic": "TEST2_UPD", "age_str": "1h5m", "mc": 260000, "l": 16000, "v": 7500,
        "ath_seconds": 3500, "status": "Processing", "group_message_id": None # Попытка обновить на None
    }
    upsert_coin(test_coin2_update)
    details2_updated = get_coin_details("CA_TEST_002")
    logger.info(f"Детали для CA_TEST_002 после обновления (group_message_id должен остаться 12345): {details2_updated}")

    test_coin2_update_with_id = {
        "ca": "CA_TEST_002", "tic": "TEST2_UPD2", "age_str": "1h10m", "mc": 270000, "l": 17000, "v": 8000,
        "ath_seconds": 3400, "status": "Processing", "group_message_id": 67890 # Обновляем group_message_id
    }
    upsert_coin(test_coin2_update_with_id)
    details2_updated_again = get_coin_details("CA_TEST_002")
    logger.info(f"Детали для CA_TEST_002 после обновления (group_message_id должен стать 67890): {details2_updated_again}")