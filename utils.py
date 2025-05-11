# utils.py
import re
import logging
from typing import Optional, Union

logger = logging.getLogger(__name__)

def time_ago_to_seconds(time_str: str) -> Optional[int]:
    """
    Конвертирует строку времени (например, "10m", "2h", "5s", "3d", "1w", "now!") в секунды.
    Поддерживает: s (секунды), m (минуты), h (часы), d (дни), w (недели).
    "now!" или строки без числового значения (например, "a minute ago") считаются как 0 секунд.
    """
    if not time_str or time_str.lower() == "now!" or "now" in time_str.lower():
        return 0
    
    time_str = time_str.lower().strip()

    # Ищем число и единицу измерения
    match = re.match(r"(\d+)\s*([smhdw])", time_str)
    if not match:
        # Если формат не стандартный "число + буква", но содержит время (например, "an hour ago", "a minute")
        # Попробуем извлечь из более общих паттернов
        if "second" in time_str or "sec" in time_str: # "a second ago"
            return 1 # Приблизительно
        if "minute" in time_str or "min" in time_str: # "a minute ago"
            return 60 # Приблизительно
        if "hour" in time_str: # "an hour ago"
            return 3600 # Приблизительно
        
        logger.warning(f"Не удалось распознать формат времени: '{time_str}'. Будет возвращено None.")
        return None

    value = int(match.group(1))
    unit = match.group(2)

    if unit == 's':
        return value
    elif unit == 'm':
        return value * 60
    elif unit == 'h':
        return value * 60 * 60
    elif unit == 'd':
        return value * 60 * 60 * 24
    elif unit == 'w':
        return value * 60 * 60 * 24 * 7
    # Можно добавить 'mo' для месяцев и 'y' для лет, если нужно, но с осторожностью (разная длина)
    # Для месяцев в среднем 30 дней, для года 365
    # elif unit == 'mo':
    #     return value * 60 * 60 * 24 * 30 
    # elif unit == 'y':
    #     return value * 60 * 60 * 24 * 365
    else:
        logger.warning(f"Неизвестная единица времени '{unit}' в строке '{time_str}'.")
        return None

def parse_monetary_value(value_str: str) -> Optional[Union[int, float]]:
    """
    Конвертирует строку с денежным значением и суффиксом (K, M, B, T) в число.
    Например: "$1.5M" -> 1500000, "250K" -> 250000, "10.2B" -> 10200000000.
    Удаляет символ '$' и запятые.
    """
    if not isinstance(value_str, str):
        logger.warning(f"parse_monetary_value ожидает строку, получено: {value_str} (тип: {type(value_str)})")
        # Попытка привести к строке, если это число
        if isinstance(value_str, (int, float)):
            value_str = str(value_str)
        else:
            return None

    cleaned_str = value_str.replace('$', '').replace(',', '').strip().upper()
    
    multiplier = 1
    if cleaned_str.endswith('K'):
        multiplier = 1000
        cleaned_str = cleaned_str[:-1]
    elif cleaned_str.endswith('M'):
        multiplier = 1000000
        cleaned_str = cleaned_str[:-1]
    elif cleaned_str.endswith('B'):
        multiplier = 1000000000
        cleaned_str = cleaned_str[:-1]
    elif cleaned_str.endswith('T'): # Trillion
        multiplier = 1000000000000
        cleaned_str = cleaned_str[:-1]

    try:
        number = float(cleaned_str)
        result = number * multiplier
        # Если результат - целое число, вернуть int для удобства
        if result.is_integer():
            return int(result)
        return result
    except ValueError:
        logger.warning(f"Не удалось конвертировать денежное значение: '{value_str}' (очищенное: '{cleaned_str}')")
        return None

def is_valid_solana_address(address: str) -> bool:
    """
    Проверяет, является ли строка потенциально валидным адресом Solana.
    Простая проверка длины и символов Base58. Не гарантирует существование адреса.
    """
    if not isinstance(address, str):
        return False
    # Адреса Solana - это строки в кодировке Base58, обычно длиной 32-44 символа.
    # Символы Base58: 123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz
    # Исключены: 0 (ноль), O (большая o), I (большая i), l (маленькая L)
    pattern = r"^[1-9A-HJ-NP-Za-km-z]{32,44}$"
    return bool(re.fullmatch(pattern, address))


if __name__ == '__main__':
    # Настройка логирования для теста
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    # Тестирование time_ago_to_seconds
    print("\n--- Тест time_ago_to_seconds ---")
    times = ["10s", "5m", "2h", "3d", "1w", "now!", " an hour ago ", "  2 S  ", "invalid", "10x", "a minute ago", "1 month ago"]
    for t_str in times:
        seconds = time_ago_to_seconds(t_str)
        print(f"'{t_str}' -> {seconds} секунд")
    
    # Тестирование parse_monetary_value
    print("\n--- Тест parse_monetary_value ---")
    values = ["$1.5M", "250K", "10.2B", "$500", "1,234.56K", "  $ 2.1 M  ", "1T", "invalid", 1000, 123.45]
    for v_str in values:
        num_value = parse_monetary_value(v_str)
        print(f"'{v_str}' -> {num_value}")

    # Тестирование is_valid_solana_address
    print("\n--- Тест is_valid_solana_address ---")
    addresses = [
        "SysvarRent111111111111111111111111111111111", # Valid
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # Valid
        "12345ThisIsNotAValidAddressLookingString12345", # Invalid (too short, wrong chars)
        "ThisIsTooLongToBeAValidSolanaAddressIndeedMyFriend", # Invalid (too long)
        "abcde", # Invalid
        None, # Invalid
        12345 # Invalid
    ]
    for addr in addresses:
        is_valid = is_valid_solana_address(addr)
        print(f"'{addr}' is valid: {is_valid}")