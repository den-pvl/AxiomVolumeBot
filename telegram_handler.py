# telegram_handler.py
import asyncio
import logging
import re
from typing import Optional, Callable, Any, Coroutine

# Библиотеки Telegram
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserIsBotError
from telegram import Bot, Update, ReplyParameters 
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

# Импортируем конфиг и утилиты
import config
import utils

logger = logging.getLogger(__name__)

# Регулярные выражения для парсинга ответов ботов
ATH1_REGEX = re.compile(r"ATH: \$[\d.,KMGTPEZY]+(?:K|M|B|T)?\s*\(([^)]+ago)\)", re.IGNORECASE)
ATH2_REGEX = re.compile(r"ATH: \$[\d.,KMGTPEZY]+(?:K|M|B|T)?\s*(?:\[([^\]]+)\]|\((now!)\))", re.IGNORECASE)
BUY_SELL_CA_REGEX = re.compile(r"([1-9A-HJ-NP-Za-km-z]{32,44})")
BUY_SELL_PER_REGEX = re.compile(r"([+-]?\d+(?:\.\d+)?%)")
INSUFFICIENT_BALANCE_REGEX = re.compile(r"Insufficient balance", re.IGNORECASE)


class TelegramHandler:
    """Класс для управления клиентами Telethon (User API) и python-telegram-bot (Bot API)."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self.telethon_client: Optional[TelegramClient] = None
        self.ptb_app: Optional[Application] = None
        self.ptb_bot: Optional[Bot] = None 

        self.ath_results_queue = asyncio.Queue() 
        self.buy_sell_results_queue = asyncio.Queue() 
        
        self._stop_callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None
        self._entity_cache = {}

    async def _get_entity(self, username: str):
        """Кэширует и возвращает Telethon Entity для username."""
        if not self.telethon_client or not self.telethon_client.is_connected():
            logger.error("Telethon клиент не подключен для получения entity.")
            return None
        
        # Нормализуем username, если он содержит '@' или разный регистр
        lookup_key = username.lstrip('@').lower()

        if lookup_key not in self._entity_cache: # Используем нормализованный ключ для кэша
            try:
                logger.debug(f"Получение entity для {username} (ключ кэша: {lookup_key})...")
                # Telethon ожидает username без '@' или числовой ID
                actual_identifier = username.lstrip('@')
                entity = await self.telethon_client.get_entity(actual_identifier)
                self._entity_cache[lookup_key] = entity
                logger.debug(f"Entity для {username} получено и закэшировано как {lookup_key}.")
            except FloodWaitError as e:
                 logger.warning(f"Flood wait при получении entity для {username}: {e.seconds} секунд.")
                 await asyncio.sleep(e.seconds + 1)
                 return await self._get_entity(username) # Повторная попытка с исходным username
            except (ValueError, TypeError) as e: # ValueError если юзернейм не найден или невалиден
                 logger.error(f"Не удалось найти entity для username: {username}. Ошибка: {e}. Проверьте правильность имени.")
                 self._entity_cache[lookup_key] = None # Кэшируем None, чтобы не пытаться снова сразу
                 return None
            except Exception as e:
                logger.error(f"Непредвиденная ошибка при получении entity для {username}: {e}", exc_info=True)
                return None
        return self._entity_cache.get(lookup_key)


    async def connect_clients(self, stop_callback: Callable[[], Coroutine[Any, Any, None]]):
        """Инициализирует и подключает оба Telegram клиента."""
        self._stop_callback = stop_callback
        logger.info("Подключение Telegram клиентов...")
        
        # --- Подключение Telethon (User API) ---
        try:
            logger.info("Инициализация Telethon клиента...")
            # Убедимся, что API_ID - это число
            api_id_int = int(config.API_ID) if config.API_ID else None
            if not api_id_int or not config.API_HASH or not config.SESSION_NAME:
                logger.error("API_ID, API_HASH или SESSION_NAME не заданы в конфигурации для Telethon.")
                return False

            self.telethon_client = TelegramClient(
                config.SESSION_NAME, 
                api_id_int, 
                config.API_HASH,
                loop=self.loop
            )
            logger.info(f"Попытка подключения Telethon как {config.SESSION_NAME}...")
            await self.telethon_client.connect()
            
            if not await self.telethon_client.is_user_authorized():
                logger.info("Требуется авторизация Telethon.")
                
                phone_number = await self.loop.run_in_executor(
                    None, 
                    input, 
                    "Введите ваш номер телефона (в международном формате, +...): "
                )
                await self.telethon_client.send_code_request(phone_number)
                
                try:
                    code = await self.loop.run_in_executor(
                        None, 
                        input, 
                        "Введите код авторизации, полученный в Telegram: "
                    )
                    await self.telethon_client.sign_in(phone_number, code=code) # Явно указываем code=code
                except SessionPasswordNeededError:
                    password = await self.loop.run_in_executor(
                        None,
                        input,
                        "Введите ваш пароль двухфакторной аутентификации (2FA): "
                    )
                    await self.telethon_client.sign_in(password=password)
                except EOFError: 
                     logger.warning("Ввод данных для авторизации Telethon прерван.")
                     raise 
                logger.info("Telethon успешно авторизован.")
            else:
                logger.info("Telethon уже авторизован.")

            self._register_telethon_handlers()
            logger.info("Обработчики событий Telethon зарегистрированы.")

        except EOFError:
             logger.info("Подключение Telethon прервано пользователем во время ввода.")
             if self.telethon_client and self.telethon_client.is_connected(): await self.telethon_client.disconnect()
             self.telethon_client = None 
             return False 
        except Exception as e:
            logger.error(f"Критическая ошибка при подключении/авторизации Telethon: {e}", exc_info=True)
            if self.telethon_client and self.telethon_client.is_connected(): await self.telethon_client.disconnect()
            self.telethon_client = None 
            return False 
        
        # --- Подключение python-telegram-bot (Bot API) ---
        try:
            if not config.BOT_TOKEN:
                logger.error("BOT_TOKEN не задан в конфигурации для python-telegram-bot.")
                if self.telethon_client and self.telethon_client.is_connected(): await self.telethon_client.disconnect()
                return False

            logger.info("Инициализация python-telegram-bot приложения...")
            self.ptb_app = Application.builder().token(config.BOT_TOKEN).build()
            self.ptb_bot = self.ptb_app.bot 
            
            self._register_ptb_handlers()
            logger.info("Обработчики команд PTB зарегистрированы.")

            await self.ptb_app.initialize() 
            await self.ptb_app.start()      

            logger.info("Приложение python-telegram-bot инициализировано и запущено.")

        except Exception as e:
            logger.error(f"Критическая ошибка при инициализации/запуске python-telegram-bot: {e}", exc_info=True)
            self.ptb_app = None
            self.ptb_bot = None
            if self.telethon_client and self.telethon_client.is_connected(): await self.telethon_client.disconnect()
            return False 

        logger.info("Оба Telegram клиента успешно инициализированы и запущены.")
        # Кэшируем entity после успешного запуска обоих
        await self._get_entity(config.BOT_ATH1_USERNAME)
        await self._get_entity(config.BOT_ATH2_USERNAME)
        await self._get_entity(config.BOT_BUYSELL1_USERNAME)
        await self._get_entity(config.BOT_BUYSELL2_USERNAME)
        
        return True

    async def disconnect_clients(self):
        """Отключает оба Telegram клиента."""
        logger.info("Отключение Telegram клиентов...")
        if self.ptb_app:
            try:
                 logger.debug("Остановка PTB приложения...")
                 await self.ptb_app.stop()      
                 await self.ptb_app.shutdown() 
                 logger.info("Приложение python-telegram-bot остановлено и очищено.")
            except Exception as e:
                 logger.error(f"Ошибка при остановке/очистке PTB: {e}", exc_info=True)
            self.ptb_app = None
            self.ptb_bot = None

        if self.telethon_client and self.telethon_client.is_connected():
            try:
                await self.telethon_client.disconnect()
                logger.info("Telethon клиент отключен.")
            except Exception as e:
                 logger.error(f"Ошибка при отключении Telethon: {e}", exc_info=True)
        self.telethon_client = None
        logger.info("Telegram клиенты отключены.")

    async def send_message_to_user_bot(self, username: str, message: str) -> bool:
        """Отправляет сообщение боту через Telethon (User API)."""
        if not self.telethon_client or not self.telethon_client.is_connected():
            logger.error(f"Telethon не подключен. Не могу отправить сообщение '{message}' боту {username}")
            return False
        
        if not username: # Проверка, что username не пустой
            logger.error("Имя пользователя для отправки сообщения не указано.")
            return False
            
        entity = await self._get_entity(username)
        if not entity:
            logger.error(f"Не удалось получить entity для {username}. Сообщение '{message}' не отправлено.")
            return False
            
        try:
            logger.info(f"Отправка сообщения '{message}' боту {username} через Telethon...")
            await self.telethon_client.send_message(entity, message)
            logger.debug(f"Сообщение '{message}' успешно отправлено боту {username}.")
            return True
        except UserIsBotError:
             logger.error(f"Попытка отправить сообщение самому себе (боту) через User API ({username}) не поддерживается Telethon.")
             return False
        except FloodWaitError as e:
             logger.warning(f"Flood wait при отправке сообщения боту {username}: {e.seconds} секунд. Повторная попытка позже.")
             await asyncio.sleep(e.seconds + 1) 
             return False 
        except Exception as e:
            logger.error(f"Ошибка при отправке сообщения '{message}' боту {username}: {e}", exc_info=True)
            return False

    async def send_notification_to_admin(self, message: str):
        """Отправляет уведомление админу через Bot API."""
        if not self.ptb_bot or not config.USER_ADMIN_ID:
            logger.error(f"PTB бот не инициализирован или USER_ADMIN_ID не задан. Уведомление админу не отправлено: {message[:100]}")
            return
        try:
            logger.debug(f"Отправка уведомления админу ({config.USER_ADMIN_ID}): {message[:100]}...") 
            await self.ptb_bot.send_message(
                chat_id=config.USER_ADMIN_ID, 
                text=message,
                parse_mode=ParseMode.HTML 
                )
        except TelegramError as e:
            logger.error(f"Ошибка Telegram при отправке уведомления админу: {e}")
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления админу: {e}", exc_info=True)
            
    async def send_notification_to_group(self, message: str, reply_to_id: Optional[int] = None) -> Optional[int]:
        """Отправляет уведомление в группу через Bot API. Возвращает ID отправленного сообщения."""
        if not self.ptb_bot or not config.GROUP_NOTIFY_ID:
            logger.error(f"PTB бот не инициализирован или GROUP_NOTIFY_ID не задан. Уведомление в группу не отправлено: {message[:100]}")
            return None
        
        reply_params = None
        if reply_to_id:
            reply_params = ReplyParameters(message_id=reply_to_id)
            
        try:
            logger.debug(f"Отправка уведомления в группу ({config.GROUP_NOTIFY_ID}), reply_to={reply_to_id}: {message[:100]}...")
            sent_message = await self.ptb_bot.send_message(
                chat_id=config.GROUP_NOTIFY_ID, 
                text=message,
                parse_mode=ParseMode.HTML, 
                reply_parameters=reply_params,
                disable_web_page_preview=True 
            )
            logger.debug(f"Сообщение успешно отправлено в группу, message_id={sent_message.message_id}")
            return sent_message.message_id
        except TelegramError as e:
            logger.error(f"Ошибка Telegram при отправке уведомления в группу {config.GROUP_NOTIFY_ID}: {e}")
        except Exception as e:
            logger.error(f"Ошибка при отправке уведомления в группу: {e}", exc_info=True)
        return None
        
    def _register_telethon_handlers(self):
        """Регистрирует единый обработчик для новых и измененных сообщений Telethon."""
        if not self.telethon_client: 
            logger.error("Попытка зарегистрировать обработчики Telethon без активного клиента.")
            return

        # Универсальный обработчик для NewMessage и MessageEdited
        # Используем events.Raw для получения и новых, и измененных событий с текстом
        @self.telethon_client.on(events.Raw(types=[events.NewMessage, events.MessageEdited]))
        async def universal_message_handler(event):
            # Проверяем, есть ли текстовое сообщение в событии
            # Используем event.raw_text, так как event.message.message может отсутствовать в MessageEdited
            message_text = getattr(event, 'raw_text', None) 
            if not message_text: 
                 # Логируем только если это не сервисное сообщение об удалении/очистке истории
                 if not isinstance(event, (events.MessageDeleted.Event, events.ChatAction.Event)):
                      logger.debug(f"Telethon: Получено событие ({type(event).__name__}) без текстового сообщения, игнорируем.")
                 return

            # Получаем отправителя 
            sender = await event.get_sender()
            if not sender: 
                 logger.debug("Telethon: Не удалось получить отправителя для события, игнорируем.")
                 return
                 
            sender_username_raw = getattr(sender, 'username', None)
            sender_username_norm = None
            if sender_username_raw:
                sender_username_norm = sender_username_raw.lstrip('@').lower()

            log_prefix = "Telethon (New):" if isinstance(event, events.NewMessage.Event) else "Telethon (Edit):"
            # Получаем chat_id из event.message, если он есть
            chat_id = getattr(event.message, 'chat_id', 'N/A') 

            logger.debug(f"{log_prefix} от user='{sender_username_raw}' (norm: '{sender_username_norm}', ID: {chat_id}): '{message_text[:100]}...'")

            # Нормализуем имена ботов из конфига для сравнения
            # (Убедимся, что переменные существуют в config перед использованием)
            bot_ath1_norm = config.BOT_ATH1_USERNAME.lstrip('@').lower() if hasattr(config, 'BOT_ATH1_USERNAME') and config.BOT_ATH1_USERNAME else None
            bot_ath2_norm = config.BOT_ATH2_USERNAME.lstrip('@').lower() if hasattr(config, 'BOT_ATH2_USERNAME') and config.BOT_ATH2_USERNAME else None
            bot_bs1_norm = config.BOT_BUYSELL1_USERNAME.lstrip('@').lower() if hasattr(config, 'BOT_BUYSELL1_USERNAME') and config.BOT_BUYSELL1_USERNAME else None
            bot_bs2_norm = config.BOT_BUYSELL2_USERNAME.lstrip('@').lower() if hasattr(config, 'BOT_BUYSELL2_USERNAME') and config.BOT_BUYSELL2_USERNAME else None

            # Диспетчеризация по отправителю
            if sender_username_norm == bot_ath1_norm and bot_ath1_norm:
                await self._handle_ath_response(message_text, is_ath1=True)
            elif sender_username_norm == bot_ath2_norm and bot_ath2_norm:
                await self._handle_ath_response(message_text, is_ath1=False)
            elif sender_username_norm == bot_bs1_norm and bot_bs1_norm:
                await self._handle_buy_sell_response(message_text, is_bot1=True)
            elif sender_username_norm == bot_bs2_norm and bot_bs2_norm:
                await self._handle_buy_sell_response(message_text, is_bot1=False)
            else:
                if sender_username_raw:
                    logger.debug(f"Сообщение от неизвестного/нецелевого отправителя @{sender_username_raw} (ID: {chat_id}), игнорируем.")
        
        logger.info("Универсальный обработчик для NewMessage и MessageEdited зарегистрирован в Telethon.")
    # ------------------------------------------------------
    
    
    async def _handle_ath_response(self, text: str, is_ath1: bool):
        """Обрабатывает ответ от ATH ботов и кладет результат в очередь."""
        bot_name = config.BOT_ATH1_USERNAME if is_ath1 else config.BOT_ATH2_USERNAME
        logger.debug(f"Обработка ответа от {bot_name}: {text[:100]}...")
        
        ca_match = BUY_SELL_CA_REGEX.search(text)
        if not ca_match:
            logger.warning(f"Не удалось извлечь CA из ответа {bot_name}: {text[:100]}...")
            # Возможно, стоит положить ошибку в очередь, если CA не найден, чтобы pending_ath_checks очистился
            # Для этого нужно знать, какой CA ожидался (сложно без цитирования)
            return 
        ca = ca_match.group(1)
        
        ath_str = None
        if is_ath1:
            match = ATH1_REGEX.search(text)
            if match:
                ath_str = match.group(1).strip()
        else: 
             match = ATH2_REGEX.search(text)
             if match:
                 ath_str = match.group(1) or match.group(2) 
                 if ath_str: ath_str = ath_str.strip()

        if ath_str:
            ath_seconds = utils.time_ago_to_seconds(ath_str)
            if ath_seconds is not None:
                logger.info(f"ATH результат для {ca} от {bot_name}: {ath_seconds} секунд ('{ath_str}')")
                await self.ath_results_queue.put({'ca': ca, 'ath_seconds': ath_seconds, 'source_bot': bot_name})
            else:
                logger.warning(f"Не удалось конвертировать ATH строку '{ath_str}' от {bot_name} для CA {ca}")
                await self.ath_results_queue.put({'ca': ca, 'error': f'invalid_ath_format: {ath_str}', 'source_bot': bot_name})
        else:
            logger.warning(f"Не удалось извлечь строку ATH из ответа {bot_name} для CA {ca}")
            await self.ath_results_queue.put({'ca': ca, 'error': 'ath_not_found', 'source_bot': bot_name})

    async def _handle_buy_sell_response(self, text: str, is_bot1: bool):
        """Обрабатывает ответ от Buy/Sell ботов и кладет результат в очередь."""
        bot_name = config.BOT_BUYSELL1_USERNAME if is_bot1 else config.BOT_BUYSELL2_USERNAME
        marker_value = config.SELL_MARKER1 if is_bot1 else config.SELL_MARKER2
        logger.debug(f"Обработка ответа от {bot_name}: {text[:100]}...")
        
        result_data = {'ca': None, 'type': None, 'per': None, 'error_type': None, 'source_bot': bot_name}
        ca_extracted = None 

        # Пытаемся извлечь CA сначала
        ca_match = BUY_SELL_CA_REGEX.search(text)
        if ca_match:
            ca_extracted = ca_match.group(1)
            result_data['ca'] = ca_extracted 

        # 1. Проверка на Insufficient balance (приоритет)
        if INSUFFICIENT_BALANCE_REGEX.search(text):
            logger.critical(f"Обнаружена ошибка 'Insufficient balance' от {bot_name}!")
            result_data['error_type'] = 'balance'
            await self.buy_sell_results_queue.put(result_data) 
            if self._stop_callback:
                logger.info("Вызов колбэка для остановки программы из-за ошибки баланса.")
                await self._stop_callback()
            return 

        # 2. Проверка на Buy Success!
        if "Buy Success!" in text:
            if ca_extracted:
                result_data['type'] = 'buy'
                logger.info(f"Обнаружен 'Buy Success!' от {bot_name} для CA {result_data['ca']}")
                await self.buy_sell_results_queue.put(result_data)
            else:
                 logger.warning(f"Обнаружен 'Buy Success!' от {bot_name}, но не удалось извлечь CA.")
            return 
        
        # 3. Проверка на Sell (по маркеру)
        if marker_value and ca_extracted: 
            try:
                escaped_marker = re.escape(marker_value)
                # Ищем маркер И CA В ОДНОМ СООБЩЕНИИ (в реф ссылке)
                # Убедимся, что регекс ищет маркер, а потом CA
                sell_marker_ca_regex = re.compile(rf"{escaped_marker}{re.escape(ca_extracted)}", re.IGNORECASE) 
                sell_match = sell_marker_ca_regex.search(text)
                if sell_match: # Убеждаемся, что маркер и CA найдены вместе
                    result_data['type'] = 'sell'
                    per_match = BUY_SELL_PER_REGEX.search(text)
                    if per_match:
                        result_data['per'] = per_match.group(1)
                    else:
                        logger.warning(f"Обнаружено сообщение о продаже от {bot_name} для CA {result_data['ca']}, но не найден процент.")
                    
                    logger.info(f"Обнаружена продажа от {bot_name} для CA {result_data['ca']} с результатом {result_data['per']}")
                    await self.buy_sell_results_queue.put(result_data)
                    return 
            except TypeError: 
                 logger.error(f"Значение маркера продажи для {bot_name} не установлено или некорректно в конфиге (SELL_MARKER1/2).")
            except re.error as re_err:
                 logger.error(f"Ошибка регулярного выражения для маркера продажи {marker_value}: {re_err}")

        # 4. Проверка на Transaction Failed! (ВАЖНО: после проверок Success/Sell)
        if "Transaction Failed!" in text:
            logger.warning(f"Обнаружена ошибка 'Transaction Failed!' от {bot_name}: {text[:150]}...")
            if ca_extracted:
                result_data['error_type'] = 'failed' # Указываем тип ошибки
                logger.info(f"Отправка 'Transaction Failed' для CA {result_data['ca']} в очередь.")
                await self.buy_sell_results_queue.put(result_data) # Отправляем результат в очередь
            else:
                logger.warning(f"Обнаружен 'Transaction Failed!' от {bot_name}, но не удалось извлечь CA.")
            return # Сообщение обработано

        # Если ни один паттерн не сработал
        logger.debug(f"Сообщение от {bot_name} (CA: {ca_extracted if ca_extracted else 'N/A'}) не соответствует известным паттернам: {text[:100]}...")
    # -----------------------------------------------------------
    
    
    def _register_ptb_handlers(self):
        """Регистрирует обработчики команд для python-telegram-bot."""
        if not self.ptb_app: return

        self.ptb_app.add_handler(CommandHandler("start", self._handle_start_command))
        
        stop_filter = (filters.COMMAND & filters.Chat(chat_id=config.USER_ADMIN_ID) & filters.Regex(r'^/stop$')) | \
                      (filters.TEXT & filters.Chat(chat_id=config.USER_ADMIN_ID) & filters.Regex(r'(?i)^Стоп парсер$'))
        
        self.ptb_app.add_handler(MessageHandler(stop_filter, self._handle_stop_command))

    async def _handle_start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Отвечает на команду /start."""
        user = update.effective_user
        logger.info(f"Получена команда /start от пользователя {user.id} ({getattr(user, 'username', 'N/A')})") 
        await update.message.reply_html(
            f"Привет, {user.mention_html()}! Я Axiom Volume Bot. Ожидаю запуска парсера.",
        )
        
    async def _handle_stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Обрабатывает команду остановки от админа."""
        logger.critical("!!! ВЫЗВАН ОБРАБОТЧИК КОМАНДЫ СТОП !!!") 
        user = update.effective_user
        logger.info(f"Получена команда СТОП от админа {user.id}")
        await update.message.reply_text("Получена команда на остановку. Завершаю работу...")
        if self._stop_callback:
             logger.info("Вызов колбэка для остановки программы из команды /stop.")
             await self._stop_callback()
        else:
             logger.warning("Колбэк для остановки не установлен!")