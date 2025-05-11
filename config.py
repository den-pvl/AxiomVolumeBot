# config.py
import os
import logging
from dotenv import load_dotenv
from pathlib import Path

# Загружаем переменные окружения из .env файла
# Убедитесь, что .env файл находится в той же директории, что и config.py, или укажите путь
env_path = Path('.') / '.env'
load_dotenv(dotenv_path=env_path)

# Telegram User API Credentials
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")
SESSION_NAME = os.getenv("SESSION_NAME", "axiom_user_session_acc2") # Имя файла сессии по умолчанию

# Telegram Bot_Notify Token
BOT_TOKEN = os.getenv("BOT_TOKEN")
USER_ADMIN_ID = int(os.getenv("USER_ADMIN_ID")) if os.getenv("USER_ADMIN_ID") else None

# Target Bots and Group
BOT_ATH1_USERNAME = os.getenv("BOT_ATH1")
BOT_ATH2_USERNAME = os.getenv("BOT_ATH2")
BOT_BUYSELL1_USERNAME = os.getenv("BOT_BUYSELL1")
BOT_BUYSELL2_USERNAME = os.getenv("BOT_BUYSELL2")
# BOT_NOTIFY_USERNAME = os.getenv("BOT_NOTIFY") # Имя вашего бота не всегда нужно для отправки, токена достаточно
GROUP_NOTIFY_ID = int(os.getenv("GROUP_NOTIFY")) if os.getenv("GROUP_NOTIFY") else None

# Parsing Settings
AXIOM_URL = os.getenv("AXIOM_URL", "https://axiom.trade/discover")
CHTIME = int(os.getenv("CHTIME", 5)) # Секунда запуска парсинга (0-59)
USER_AGENT = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")

# Thresholds and Timeouts
BOT_ATH_TIMEOUT_SECONDS = int(os.getenv("BOT_ATH_TOUT", 3))
THRESHOLD_ATH_SECONDS = int(os.getenv("THRESHOLD_ATH_SECONDS", 60))
THRESHOLD_VOLUME = int(os.getenv("THRESHOLD_VOLUME", 150000))
PLAYWRIGHT_TIMEOUT = int(os.getenv("PLAYWRIGHT_TIMEOUT", 30000)) # мс

# Sell Markers
SELL_MARKER1 = os.getenv("SELL_MARKER1")
SELL_MARKER2 = os.getenv("SELL_MARKER2")

# Paths and DB
CHROME_PROFILE_PATH = os.getenv("CHROME_PROFILE_PATH")
DATABASE_NAME = os.getenv("DATABASE_NAME", "axiom_data.db")
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH", "axiom_parser.log")

# Проверка на наличие обязательных переменных
REQUIRED_VARS = [
    "API_ID", "API_HASH", "BOT_TOKEN", "USER_ADMIN_ID", "SESSION_NAME",
    "BOT_ATH1", "BOT_ATH2", "BOT_BUYSELL1", "BOT_BUYSELL2", "GROUP_NOTIFY",
    "CHTIME", "THRESHOLD_ATH_SECONDS", "THRESHOLD_VOLUME",
    "SELL_MARKER1", "SELL_MARKER2", "CHROME_PROFILE_PATH"
]

missing_vars = [var for var in REQUIRED_VARS if not os.getenv(var)]
if missing_vars:
    raise EnvironmentError(f"Отсутствуют обязательные переменные окружения: {', '.join(missing_vars)}")

def setup_logging():
    """Настраивает базовую конфигурацию логирования."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE_PATH, encoding='utf-8'),
            logging.StreamHandler() # Вывод в консоль
        ]
    )
    # Уменьшаем "шум" от некоторых библиотек
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("selenium").setLevel(logging.WARNING)
    logging.getLogger("playwright").setLevel(logging.WARNING) # Playwright может быть довольно "болтливым" на уровне INFO
    logging.getLogger("telethon").setLevel(logging.WARNING) # Telethon также может генерировать много INFO логов
    # logging.getLogger("apscheduler").setLevel(logging.WARNING)

    logger = logging.getLogger(__name__)
    logger.info("Логирование настроено.")
    return logger

# Вызовем настройку логирования один раз при импорте модуля,
# чтобы логгер был доступен сразу.
# logger = setup_logging() # Можно так, или вызывать setup_logging() в main.py

if __name__ == '__main__':
    # Пример использования и проверки, что все загрузилось
    print("API_ID:", API_ID)
    print("BOT_TOKEN:", BOT_TOKEN)
    print("CHROME_PROFILE_PATH:", CHROME_PROFILE_PATH)
    # Настройка и тест логгера
    test_logger = setup_logging()
    test_logger.info("Тестовое сообщение из config.py")
    test_logger.warning("Тестовое предупреждение из config.py")