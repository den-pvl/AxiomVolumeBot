# telegram_handler.py
import asyncio
import logging
import re
from typing import Optional, Callable, Any, Coroutine, Dict
from datetime import datetime

# Библиотеки Telegram
from telethon import TelegramClient, events, errors
from telethon.tl.types import PeerUser

from telegram import Bot, Update, ReplyParameters
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.constants import ParseMode
from telegram.error import TelegramError

import config # Нужен для доступа к config.BOT_ATH1_USERNAME и т.д.
import utils

logger = logging.getLogger(__name__)

# --- РЕГУЛЯРНЫЕ ВЫРАЖЕНИЯ ---
# 1. Для извлечения CA из ссылок или обратных апострофов
AXIOM_CA_IN_URL_OR_BACKTICKS_REGEX = re.compile(
    r"(?:axiom\.trade/t/|pump\.fun/|dexscreener\.com/solana/|dextools\.io/app(?:/[a-z]{2})?/solana/pair-explorer/|birdeye\.so/(?:[a-zA-Z0-9_]+/)?token/)"
    r"([1-9A-HJ-NP-Za-km-z]{32,44})"  # Группа 1: CA в URL
    r"|`([1-9A-HJ-NP-Za-km-z]{32,44})`"  # Группа 2: CA в обратных апострофах
)


UNIVERSAL_ATH_REGEX = re.compile(
    r'ATH:[^\n\r]*?[\(\[]([^\)\]]+)[\)\]]',
    re.IGNORECASE
)

# 3. Регексы для Buy/Sell
BUY_SELL_CA_REGEX = re.compile(r"([1-9A-HJ-NP-Za-km-z]{32,44})")
BUY_SELL_PER_REGEX = re.compile(r"([+-]?\d+(?:\.\d+)?%)")
INSUFFICIENT_BALANCE_REGEX = re.compile(r"Insufficient balance", re.IGNORECASE)
# --- КОНЕЦ РЕГУЛЯРНЫХ ВЫРАЖЕНИЙ ---

class TelegramHandler:
    def __init__(self, loop: asyncio.AbstractEventLoop, 
                 stop_callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None,
                 pause_callback: Optional[Callable[..., Coroutine[Any, Any, None]]] = None, # Принимает auto_triggered и reason
                 play_callback: Optional[Callable[[], Coroutine[Any, Any, None]]] = None,
                 pause_event_main: Optional[asyncio.Event] = None,
                 ath_bot_mode_runtime: Optional[str] = "BOTH"): 
        self.loop = loop
        self.telethon_client: Optional[TelegramClient] = None
        self.ptb_app: Optional[Application] = None
        self.ptb_bot: Optional[Bot] = None
        self.ath_results_queue = asyncio.Queue()
        self.buy_sell_results_queue = asyncio.Queue()
        
        # Колбэки для управления состоянием из main.py
        self._stop_callback = stop_callback
        self._pause_callback = pause_callback
        self._play_callback = play_callback
        self.pause_event = pause_event_main
        self.ATH_BOT_MODE_RUNTIME = ath_bot_mode_runtime
        self._entity_cache: Dict[Any, Any] = {}
        self.pending_ath_checks_ref: Dict[str, Any] = {}


    async def _get_entity(self, username_or_id: Any):
        if not self.telethon_client or not self.telethon_client.is_connected():
            logger.error("TG_HANDLER: Telethon клиент не подключен для получения entity.")
            return None
        lookup_key = str(username_or_id).lstrip('@').lower()
        if lookup_key not in self._entity_cache or self._entity_cache.get(lookup_key) is None:
            try:
                logger.debug(f"TG_HANDLER: Получение entity для '{username_or_id}' (ключ: '{lookup_key}')...")
                actual_identifier = username_or_id
                if isinstance(username_or_id, str) and username_or_id.startswith('@'):
                    actual_identifier = username_or_id.lstrip('@')
                entity = await self.telethon_client.get_entity(actual_identifier)
                self._entity_cache[lookup_key] = entity
                logger.debug(f"TG_HANDLER: Entity для '{username_or_id}' получено и закэшировано.")
            except errors.FloodWaitError as e:
                logger.warning(f"TG_HANDLER: Flood wait при получении entity для '{username_or_id}': {e.seconds} сек.")
                await asyncio.sleep(e.seconds + 1)
                return await self._get_entity(username_or_id)
            except (ValueError, TypeError, errors.UsernameInvalidError, errors.ChannelInvalidError, 
                      errors.UserIdInvalidError, errors.ChatIdInvalidError, errors.TypeNotFoundError) as e:
                logger.error(f"TG_HANDLER: Не удалось найти entity для '{username_or_id}'. Ошибка: {type(e).__name__} - {e}")
                self._entity_cache[lookup_key] = None
                return None
            except Exception as e:
                logger.error(f"TG_HANDLER: Непредвиденная ошибка при получении entity для '{username_or_id}': {e}", exc_info=True)
                self._entity_cache[lookup_key] = None
                return None
        return self._entity_cache.get(lookup_key)
        pass
    async def connect_clients(self):
        
        logger.info("TG_HANDLER: Подключение Telegram клиентов...")
        # Telethon
        try:
            logger.info("TG_HANDLER: Инициализация Telethon клиента...")
            api_id_int = int(config.API_ID) if config.API_ID and config.API_ID.isdigit() else None
            if not api_id_int or not config.API_HASH or not config.SESSION_NAME:
                logger.error("TG_HANDLER: API_ID/API_HASH/SESSION_NAME не заданы корректно для Telethon.")
                return False
            self.telethon_client = TelegramClient(config.SESSION_NAME, api_id_int, config.API_HASH, loop=self.loop)
            logger.info(f"TG_HANDLER: Попытка подключения Telethon как {config.SESSION_NAME}...")
            await self.telethon_client.connect()
            if not await self.telethon_client.is_user_authorized():
                logger.info("TG_HANDLER: Требуется авторизация Telethon.")
                phone = await self.loop.run_in_executor(None, input, "Введите номер телефона (межд. формат, +...): ")
                await self.telethon_client.send_code_request(phone)
                code = await self.loop.run_in_executor(None, input, "Введите код авторизации из Telegram: ")
                try:
                    await self.telethon_client.sign_in(phone=phone, code=code)
                except errors.SessionPasswordNeededError:
                    password = await self.loop.run_in_executor(None, input, "Введите пароль 2FA: ")
                    await self.telethon_client.sign_in(password=password)
                logger.info("TG_HANDLER: Telethon успешно авторизован.")
            else:
                logger.info("TG_HANDLER: Telethon уже авторизован.")
            self._register_telethon_handlers()
        except Exception as e:
            logger.critical(f"TG_HANDLER: Критическая ошибка при подключении Telethon: {e}", exc_info=True)
            if self.telethon_client and self.telethon_client.is_connected(): await self.telethon_client.disconnect()
            return False
        # PTB
        try:
            if not config.BOT_TOKEN:
                logger.error("TG_HANDLER: BOT_TOKEN не задан для PTB.")
                if self.telethon_client and self.telethon_client.is_connected(): await self.telethon_client.disconnect()
                return False
            logger.info("TG_HANDLER: Инициализация python-telegram-bot...")
            self.ptb_app = Application.builder().token(config.BOT_TOKEN).build()
            self.ptb_bot = self.ptb_app.bot
            self._register_ptb_handlers()
            #await self.ptb_app.initialize()
            #await self.ptb_app.start()
            logger.info("TG_HANDLER: python-telegram-bot Application создан, обработчики зарегистрированы. Polling запустит initialize/start, если требуется.")
            logger.info("TG_HANDLER: python-telegram-bot инициализирован и запущен.")
        except Exception as e:
            logger.critical(f"TG_HANDLER: Критическая ошибка при инициализации PTB: {e}", exc_info=True)
            if self.telethon_client and self.telethon_client.is_connected(): await self.telethon_client.disconnect()
            #if self.ptb_app and self.ptb_app.running: await self.ptb_app.stop()
            return False

        logger.info("TG_HANDLER: Telethon клиент и PTB Application успешно инициализированы (но PTB polling еще не запущен из main.py).")
        if self.telethon_client and self.telethon_client.is_connected():
            bots_to_cache = filter(None, [config.BOT_ATH1_USERNAME, config.BOT_ATH2_USERNAME, 
                                          config.BOT_BUYSELL1_USERNAME, config.BOT_BUYSELL2_USERNAME])
            for bot_name in bots_to_cache:
                logger.info(f"TG_HANDLER: Кэширование entity для: {bot_name}")
                await self._get_entity(bot_name)
        return True


    async def disconnect_clients(self):
        logger.info("TG_HANDLER: Отключение Telegram клиентов...")
        if self.ptb_app:
            try:
                # Сначала останавливаем updater, если он был запущен и работает
                if self.ptb_app.updater and self.ptb_app.updater.running:
                    logger.info("TG_HANDLER: Остановка PTB Updater...")
                    await self.ptb_app.updater.stop()
                    logger.info("TG_HANDLER: PTB Updater остановлен.")

                # Затем останавливаем само приложение, если оно "running"
                # (app.running становится False после app.stop())
                if self.ptb_app.running:
                    logger.info("TG_HANDLER: Остановка PTB Application (app.stop())...")
                    await self.ptb_app.stop()
                    logger.info("TG_HANDLER: PTB Application.stop() вызван.")

                # shutdown() для полного освобождения ресурсов PTB
                logger.info("TG_HANDLER: Вызов PTB Application.shutdown()...")
                await self.ptb_app.shutdown()
                logger.info("TG_HANDLER: PTB Application.shutdown() завершен.")

            except Exception as e:
                logger.error(f"TG_HANDLER: Ошибка при остановке/завершении PTB: {e}", exc_info=True)

        self.ptb_app = None # Явно обнуляем после всех операций
        self.ptb_bot = None

        if self.telethon_client and self.telethon_client.is_connected():
            try:
                logger.debug("TG_HANDLER: Отключение Telethon...")
                await self.telethon_client.disconnect()
                logger.info("TG_HANDLER: Telethon отключен.")
            except Exception as e:
                logger.error(f"TG_HANDLER: Ошибка при отключении Telethon: {e}", exc_info=True)
        self.telethon_client = None
        logger.info("TG_HANDLER: Telegram клиенты (PTB и Telethon) полностью отключены.")

    async def send_message_to_user_bot(self, username_or_id: Any, message: str) -> bool:
        # ... (код send_message_to_user_bot из предыдущего ответа)
        if not self.telethon_client or not self.telethon_client.is_connected():
            logger.error(f"TG_HANDLER: Telethon не подключен. Не могу отправить '{message}' -> {username_or_id}")
            return False
        if not username_or_id:
            logger.error("TG_HANDLER: Идентификатор для отправки не указан.")
            return False
        entity = await self._get_entity(username_or_id)
        if not entity:
            logger.error(f"TG_HANDLER: Не удалось получить entity для {username_or_id}. Сообщение '{message}' не отправлено.")
            return False
        try:
            await self.telethon_client.send_message(entity, message)
            logger.debug(f"TG_HANDLER: Сообщение '{message}' -> {username_or_id} отправлено.")
            return True
        # ... (обработка ошибок UserIsBotError, FloodWaitError, Exception)
        except errors.UserIsBotError:
             logger.error(f"TG_HANDLER: Попытка отправить сообщение ({message}) самому себе (боту) через User API ({username_or_id}) не поддерживается.")
             return False
        except errors.FloodWaitError as e:
             logger.warning(f"TG_HANDLER: Flood wait при отправке ({message}) -> {username_or_id}: {e.seconds} сек.")
             return False 
        except Exception as e:
            logger.error(f"TG_HANDLER: Ошибка при отправке ({message}) -> {username_or_id}: {e}", exc_info=True)
            return False
        pass

    def _register_telethon_handlers(self):
        if not self.telethon_client:
            logger.error("TELETHON_HANDLER: Клиент Telethon не инициализирован.")
            return

        @self.telethon_client.on(events.NewMessage())
        async def universal_message_handler(event):
            message_obj = event.message
            if not (message_obj and message_obj.text):
                return # Пропускаем, если нет сообщения или текста

            actual_sender_id = None
            sender_entity = None
            sender_username_raw = None
            is_bot_flag = False

            try:
                if message_obj.from_id and isinstance(message_obj.from_id, PeerUser):
                    actual_sender_id = message_obj.from_id.user_id
                elif message_obj.sender_id:
                    actual_sender_id = message_obj.sender_id
                
                if not actual_sender_id:
                    logger.debug(f"TG_HANDLER (UMH): Не удалось определить actual_sender_id. Текст: '{message_obj.text[:70]}'")
                    return
                
                my_self = await self.telethon_client.get_me(input_peer=False)
                if my_self and actual_sender_id == my_self.id:
                    return # Игнорируем свои исходящие сообщения

                try:
                    sender_entity = await self._get_entity(actual_sender_id) # Используем _get_entity для кэширования
                except ValueError as e_val: # Ловим ошибку, если entity не найдено
                    logger.warning(f"TG_HANDLER (UMH): ValueError при get_entity для actual_sender_id='{actual_sender_id}': {e_val}. Сообщение пропущено.")
                    return
                except errors.TypeNotFoundError:
                     logger.warning(f"TG_HANDLER (UMH): TypeNotFoundError для actual_sender_id='{actual_sender_id}'. Сообщение пропущено.")
                     return
                except Exception as e_get_entity_other: # Ловим другие ошибки get_entity
                    logger.error(f"TG_HANDLER (UMH): Ошибка при get_entity для actual_sender_id='{actual_sender_id}': {e_get_entity_other}", exc_info=True)
                    return

                if sender_entity:
                    sender_username_raw = getattr(sender_entity, 'username', None)
                    is_bot_flag = getattr(sender_entity, 'bot', False)
                
                logger.info(
                    f"TG_HANDLER (UMH): NewMessage от ID='{actual_sender_id}', username='{sender_username_raw}', "
                    f"is_bot='{is_bot_flag}'. Текст: '{message_obj.text[:100].replace('\n',' ')}...'"
                )

            except Exception as e_sender_info:
                logger.error(f"TG_HANDLER (UMH): Ошибка при получении инфо об отправителе: {e_sender_info}", exc_info=True)
                return

            if not sender_username_raw: # Если username не определился
                logger.debug(f"TG_HANDLER (UMH): Не удалось определить username для ID='{actual_sender_id}'. Пропуск.")
                return

            sender_username_norm = sender_username_raw.lstrip('@').lower()
            
            # Нормализация имен ботов из конфига
            bot_ath1_norm = config.BOT_ATH1_USERNAME.lstrip('@').lower() if config.BOT_ATH1_USERNAME else None
            bot_ath2_norm = config.BOT_ATH2_USERNAME.lstrip('@').lower() if config.BOT_ATH2_USERNAME else None
            bot_bs1_norm = config.BOT_BUYSELL1_USERNAME.lstrip('@').lower() if config.BOT_BUYSELL1_USERNAME else None
            bot_bs2_norm = config.BOT_BUYSELL2_USERNAME.lstrip('@').lower() if config.BOT_BUYSELL2_USERNAME else None

            if logger.isEnabledFor(logging.DEBUG): # Для экономии ресурсов, если DEBUG не включен
                logger.debug(f"TG_HANDLER (UMH): Сравнение '{sender_username_norm}' с: ATH1='{bot_ath1_norm}', ATH2='{bot_ath2_norm}', BS1='{bot_bs1_norm}', BS2='{bot_bs2_norm}'.")

            # Диспетчеризация
            if bot_ath1_norm and sender_username_norm == bot_ath1_norm:
                logger.info(f"TG_HANDLER (UMH): Сообщение от {config.BOT_ATH1_USERNAME}. Вызов _handle_ath_response.")
                await self._handle_ath_response(message_obj.text, sender_username_raw)
            elif bot_ath2_norm and sender_username_norm == bot_ath2_norm:
                logger.info(f"TG_HANDLER (UMH): Сообщение от {config.BOT_ATH2_USERNAME}. Вызов _handle_ath_response.")
                await self._handle_ath_response(message_obj.text, sender_username_raw)
            elif bot_bs1_norm and sender_username_norm == bot_bs1_norm:
                logger.info(f"TG_HANDLER (UMH): Сообщение от {config.BOT_BUYSELL1_USERNAME}. Вызов _handle_buy_sell_response.")
                await self._handle_buy_sell_response(message_obj.text, sender_username_raw)
            elif bot_bs2_norm and sender_username_norm == bot_bs2_norm:
                logger.info(f"TG_HANDLER (UMH): Сообщение от {config.BOT_BUYSELL2_USERNAME}. Вызов _handle_buy_sell_response.")
                await self._handle_buy_sell_response(message_obj.text, sender_username_raw)
            # else: Сообщения от других пользователей/ботов будут просто залогированы выше и проигнорированы здесь

        logger.info("TELETHON_HANDLER: Основной обработчик NewMessage зарегистрирован.")


        # --- НОВЫЙ обработчик для ОТРЕДАКТИРОВАННЫХ сообщений ---
        @self.telethon_client.on(events.MessageEdited())
        async def universal_message_edited_handler(event):
            message_obj = event.message
            if not (message_obj and message_obj.text):
                return # Пропускаем, если нет сообщения или текста

            actual_sender_id = None
            sender_entity = None # Для получения username
            sender_username_raw = None
            # is_bot_flag не так важен здесь, как сам username

            try:
                # Определение ID отправителя (аналогично NewMessage)
                if message_obj.from_id and isinstance(message_obj.from_id, PeerUser):
                    actual_sender_id = message_obj.from_id.user_id
                elif message_obj.sender_id: # У Telethon event.message содержит sender_id
                    actual_sender_id = message_obj.sender_id
                
                if not actual_sender_id:
                    logger.debug(f"TG_HANDLER (Edited): Не удалось определить actual_sender_id для отредактированного сообщения. Текст: '{message_obj.text[:70]}'")
                    return
                
                # Игнорируем редактирование своих собственных сообщений (если таковые будут)
                my_self = await self.telethon_client.get_me(input_peer=False)
                if my_self and actual_sender_id == my_self.id:
                    return

                # Получение username отправителя
                try:
                    sender_entity = await self._get_entity(actual_sender_id)
                except ValueError as e_val: 
                    logger.warning(f"TG_HANDLER (Edited): ValueError при get_entity для actual_sender_id='{actual_sender_id}': {e_val}. Сообщение пропущено.")
                    return
                except errors.TypeNotFoundError:
                    logger.warning(f"TG_HANDLER (Edited): TypeNotFoundError для actual_sender_id='{actual_sender_id}'. Сообщение пропущено.")
                    return
                except Exception as e_get_entity_other: 
                    logger.error(f"TG_HANDLER (Edited): Ошибка при get_entity для actual_sender_id='{actual_sender_id}': {e_get_entity_other}", exc_info=True)
                    return

                if sender_entity:
                    sender_username_raw = getattr(sender_entity, 'username', None)
                
                logger.info(
                    f"TG_HANDLER (Edited): MessageEdited от ID='{actual_sender_id}', username='{sender_username_raw}'. "
                    f"Текст: '{message_obj.text[:100].replace('\n',' ')}...'"
                )

            except Exception as e_sender_info:
                logger.error(f"TG_HANDLER (Edited): Ошибка при получении инфо об отправителе (отредактированное): {e_sender_info}", exc_info=True)
                return

            if not sender_username_raw:
                logger.debug(f"TG_HANDLER (Edited): Не удалось определить username для ID='{actual_sender_id}' (отредактированное). Пропуск.")
                return

            sender_username_norm = sender_username_raw.lstrip('@').lower()
            
            # Нам нужны только боты покупки/продажи для этой конкретной ошибки
            bot_bs1_norm = config.BOT_BUYSELL1_USERNAME.lstrip('@').lower() if config.BOT_BUYSELL1_USERNAME else None
            bot_bs2_norm = config.BOT_BUYSELL2_USERNAME.lstrip('@').lower() if config.BOT_BUYSELL2_USERNAME else None

            if logger.isEnabledFor(logging.DEBUG):
                 logger.debug(f"TG_HANDLER (Edited): Сравнение '{sender_username_norm}' с: BS1='{bot_bs1_norm}', BS2='{bot_bs2_norm}'.")

            # Если отредактированное сообщение от одного из ботов покупки/продажи, вызываем _handle_buy_sell_response
            if bot_bs1_norm and sender_username_norm == bot_bs1_norm:
                logger.info(f"TG_HANDLER (Edited): Отредактированное сообщение от {config.BOT_BUYSELL1_USERNAME}. Вызов _handle_buy_sell_response.")
                await self._handle_buy_sell_response(message_obj.text, sender_username_raw)
            elif bot_bs2_norm and sender_username_norm == bot_bs2_norm:
                logger.info(f"TG_HANDLER (Edited): Отредактированное сообщение от {config.BOT_BUYSELL2_USERNAME}. Вызов _handle_buy_sell_response.")
                await self._handle_buy_sell_response(message_obj.text, sender_username_raw)
            # Редактирования от ATH ботов или других пользователей в данном контексте игнорируются для обработки как buy/sell ответ

        logger.info("TELETHON_HANDLER: Обработчик MessageEdited зарегистрирован.")



    async def _handle_ath_response(self, text: str, source_bot_username: str):
        logger.debug(f"ATH_HANDLER ({source_bot_username}): Полный текст ответа для обработки:\n{text}")

        # Попытка извлечь CA для логирования, даже если бот на паузе или CA не найден в pending
        _ca_for_log_display = "CA_не_определен_из_текста_ответа"
        # Сначала ищем все CA в тексте
        potential_cas_in_text_for_log: List[str] = []
        for match_log in AXIOM_CA_IN_URL_OR_BACKTICKS_REGEX.finditer(text):
            ca_in_url_log = match_log.group(1)
            ca_in_backticks_log = match_log.group(2)
            if ca_in_url_log and utils.is_valid_solana_address(ca_in_url_log):
                potential_cas_in_text_for_log.append(ca_in_url_log)
            if ca_in_backticks_log and utils.is_valid_solana_address(ca_in_backticks_log):
                potential_cas_in_text_for_log.append(ca_in_backticks_log)
        
        # Пытаемся найти тот, что есть в pending_ath_checks_ref для более точного лога
        if self.pending_ath_checks_ref:
            for ca_cand_log in potential_cas_in_text_for_log:
                if ca_cand_log in self.pending_ath_checks_ref:
                    _ca_for_log_display = ca_cand_log
                    break
            if _ca_for_log_display == "CA_не_определен_из_текста_ответа" and potential_cas_in_text_for_log:
                _ca_for_log_display = potential_cas_in_text_for_log[0] # Берем первый попавшийся, если нет в pending
        elif potential_cas_in_text_for_log:
             _ca_for_log_display = potential_cas_in_text_for_log[0]


        # --- ПРОВЕРКА ПАУЗЫ ---
        if self.pause_event and self.pause_event.is_set():
            logger.info(f"ATH_HANDLER ({source_bot_username}): Бот на паузе. Ответ для CA (примерно) '{_ca_for_log_display}' не будет обработан.")
            return 
        # --- КОНЕЦ ПРОВЕРКИ ПАУЗЫ ---

        # Теперь основная логика извлечения CA, который есть в pending_ath_checks_ref
        target_ca: Optional[str] = None
        found_expected_ca_in_pending = False
        for ca_candidate in potential_cas_in_text_for_log: # Используем уже извлеченные CA
            if self.pending_ath_checks_ref and ca_candidate in self.pending_ath_checks_ref:
                target_ca = ca_candidate
                found_expected_ca_in_pending = True
                logger.info(f"ATH_HANDLER ({source_bot_username}): Ответ связан с ожидаемым CA '{target_ca}'.")
                break
        
        if not found_expected_ca_in_pending:
            pending_keys_str = list(self.pending_ath_checks_ref.keys()) if self.pending_ath_checks_ref else "пусто"
            logger.warning(f"ATH_HANDLER ({source_bot_username}): Извлеченные CA ({potential_cas_in_text_for_log}) "
                           f"не найдены в активных pending_ath_checks ({pending_keys_str}). Ответ игнорируется.")
            return

        if not target_ca: 
            logger.error(f"ATH_HANDLER ({source_bot_username}): target_ca не установлен после поиска в pending_ath_checks. Логическая ошибка.")
            return

        check_info = self.pending_ath_checks_ref.get(target_ca)
        if not check_info: # Если CA был удален другим потоком/задачей между проверкой и этим моментом
            logger.warning(f"ATH_HANDLER ({source_bot_username}): CA '{target_ca}' более не найден в pending_ath_checks. Ответ игнорируется.")
            return
            
        if check_info.get('decision_made'):
            logger.info(f"ATH_HANDLER ({source_bot_username}): Решение по CA '{target_ca}' уже принято. Ответ от {source_bot_username} игнорируется.")
            return

        current_ath_result_data: Optional[Dict[str, int]] = None 
        current_ath_status: str = 'received_na_error' 

        match_ath = UNIVERSAL_ATH_REGEX.search(text)
        if match_ath:
            ath_str_val = (match_ath.group(1) or "").strip()
            if ath_str_val:
                # logger.info(f"ATH_HANDLER ({source_bot_username}): Для CA '{target_ca}' извлечена строка ATH: '{ath_str_val}'.") # Уже есть выше
                if ath_str_val.upper() == "N/A":
                    current_ath_status = 'received_n/a'
                    logger.info(f"ATH_HANDLER ({source_bot_username}): ATH для CA '{target_ca}' = 'N/A'.")
                else:
                    ath_seconds = utils.time_ago_to_seconds(ath_str_val)
                    if ath_seconds is not None:
                        current_ath_result_data = {'seconds': ath_seconds}
                        current_ath_status = 'received_ok'
                        logger.info(f"ATH_HANDLER ({source_bot_username}): ATH для CA '{target_ca}' = {ath_seconds}s.")
                    else:
                        logger.warning(f"ATH_HANDLER ({source_bot_username}): Не удалось конвертировать ATH '{ath_str_val}' для CA '{target_ca}'.")
                        current_ath_status = 'received_error_format'
            else:
                logger.warning(f"ATH_HANDLER ({source_bot_username}): ATH_REGEX сработал для CA '{target_ca}', но группа времени пуста.")
                current_ath_status = 'received_error_empty'
        else:
            logger.info(f"ATH_HANDLER ({source_bot_username}): ATH не найден в тексте для CA '{target_ca}'.")
            current_ath_status = 'received_n/a' 

        # Обновляем информацию в pending_ath_checks_ref
        bot_key_status: Optional[str] = None
        bot_key_data: Optional[str] = None
        
        norm_source_bot_username = source_bot_username.lstrip('@').lower()
        norm_bot_ath1 = (config.BOT_ATH1_USERNAME or "").lstrip('@').lower()
        norm_bot_ath2 = (config.BOT_ATH2_USERNAME or "").lstrip('@').lower()

        if config.BOT_ATH1_USERNAME and norm_source_bot_username == norm_bot_ath1:
            bot_key_status = 'ath1_status'
            bot_key_data = 'ath1_data'
        elif config.BOT_ATH2_USERNAME and norm_source_bot_username == norm_bot_ath2:
            bot_key_status = 'ath2_status'
            bot_key_data = 'ath2_data'
        
        if bot_key_status and bot_key_data:
            # Обновляем только если статус еще 'pending' или 'not_sent'
            # Это предотвращает перезапись уже установленного статуса (например, 'timeout') новым 'received_na_error'
            if check_info.get(bot_key_status) in ['pending', 'not_sent']:
                check_info[bot_key_status] = current_ath_status
                check_info[bot_key_data] = current_ath_result_data 
                logger.debug(f"ATH_HANDLER: Обновлен pending_ath_checks для CA '{target_ca}' от {source_bot_username}: статус={current_ath_status}, данные={current_ath_result_data}")
            else:
                logger.info(f"ATH_HANDLER ({source_bot_username}): Статус для {bot_key_status} по CA '{target_ca}' уже был '{check_info.get(bot_key_status)}' (не 'pending'/'not_sent'). Новый ответ ({current_ath_status}) не перезаписан, но evaluate_ath_decision будет вызван.")
        else:
            logger.warning(f"ATH_HANDLER: Не удалось определить, для какого ключа (ath1/ath2) обновить данные от {source_bot_username} по CA {target_ca}")

        # Вызываем оценку решения ПОСЛЕ обновления check_info
        # Передаем from_monitor=False, так как это прямой ответ от бота
        await self.evaluate_ath_decision(target_ca, from_monitor=False)

    async def evaluate_ath_decision(self, ca: str, from_monitor: bool = False): # Добавлен флаг from_monitor
        if not self.pending_ath_checks_ref:
            logger.error("EVALUATE_ATH: pending_ath_checks_ref не инициализирован.")
            return

        check_info = self.pending_ath_checks_ref.get(ca)

        if not check_info:
            logger.debug(f"EVALUATE_ATH ({ca}): Запись не найдена в pending_ath_checks.")
            return

        if check_info.get('decision_made'):
            # logger.debug(f"EVALUATE_ATH ({ca}): Решение уже принято.") # Можно раскомментировать для отладки
            return

        logger.debug(f"EVALUATE_ATH ({ca}): Оценка для CA. Текущее состояние: {check_info}")

        # 1. Агрессивная отправка первого хорошего ответа
        # (только если вызов не от монитора таймаутов, чтобы не дублировать логику)
        if not from_monitor:
            if check_info.get('ath1_status') == 'received_ok' and check_info.get('ath1_data'):
                ath_s = check_info['ath1_data']['seconds']
                src_bot = config.BOT_ATH1_USERNAME
                logger.info(f"EVALUATE_ATH ({ca}): Первый хороший ответ от ATH1 ({ath_s}s). Отправка в очередь.")
                await self.ath_results_queue.put({'ca': ca, 'ath_seconds': ath_s, 'source_bot': src_bot})
                check_info['decision_made'] = True
                return # Решение принято

            if self.ATH_BOT_MODE_RUNTIME == "BOTH" and \
               check_info.get('ath2_status') == 'received_ok' and check_info.get('ath2_data'):
                ath_s = check_info['ath2_data']['seconds']
                src_bot = config.BOT_ATH2_USERNAME
                logger.info(f"EVALUATE_ATH ({ca}): Первый хороший ответ (или ATH1 был плох) от ATH2 ({ath_s}s). Отправка в очередь.")
                await self.ath_results_queue.put({'ca': ca, 'ath_seconds': ath_s, 'source_bot': src_bot})
                check_info['decision_made'] = True
                return # Решение принято

        # 2. Проверка таймаутов (сработает и при вызове от монитора, и при вызове от _handle_ath_response)
        now = datetime.now()
        timestamp_requested = check_info.get('timestamp_requested')
        made_change_by_timeout = False

        if not timestamp_requested:
            logger.warning(f"EVALUATE_ATH ({ca}): Отсутствует timestamp_requested. Таймаут не проверяется.")
        else:
            # Проверка для ATH1
            if check_info.get('ath1_status') == 'pending':
                if (now - timestamp_requested).total_seconds() > config.BOT_ATH_TIMEOUT_SECONDS:
                    logger.warning(f"EVALUATE_ATH ({ca}): Таймаут для ATH1 ({config.BOT_ATH_TIMEOUT_SECONDS}s). Статус изменен на 'timeout'.")
                    check_info['ath1_status'] = 'timeout'
                    made_change_by_timeout = True

            # Проверка для ATH2 (если он должен был быть запрошен)
            should_check_ath2 = (self.ATH_BOT_MODE_RUNTIME == "BOTH" and 
                                 check_info.get('ath2_status') != 'not_sent') # Убедимся, что запрос к ATH2 вообще был или планировался

            if should_check_ath2 and check_info.get('ath2_status') == 'pending':
                if (now - timestamp_requested).total_seconds() > config.BOT_ATH_TIMEOUT_SECONDS:
                    logger.warning(f"EVALUATE_ATH ({ca}): Таймаут для ATH2 ({config.BOT_ATH_TIMEOUT_SECONDS}s). Статус изменен на 'timeout'.")
                    check_info['ath2_status'] = 'timeout'
                    made_change_by_timeout = True

        # 3. Если после таймаутов все еще не принято решение, проверяем, все ли ответы пришли/таймаутнулись
        # Эта логика нужна, если агрессивная отправка не сработала (например, первый ответ был N/A)
        # и мы дождались таймаута или второго ответа.
        if not check_info.get('decision_made'): # Если еще не решили
            all_ath1_resolved = check_info.get('ath1_status') != 'pending' and check_info.get('ath1_status') != 'not_sent'

            all_ath2_resolved = True # Изначально считаем, что ATH2 разрешен, если он не используется
            if self.ATH_BOT_MODE_RUNTIME == "BOTH" and config.BOT_ATH2_USERNAME and check_info.get('ath2_status') != 'not_sent':
                all_ath2_resolved = check_info.get('ath2_status') != 'pending'

            if all_ath1_resolved and all_ath2_resolved:
                # Все ожидаемые ответы пришли (или таймаут). Ищем лучший.
                best_ath = float('inf')
                best_source = None

                if check_info.get('ath1_status') == 'received_ok' and check_info.get('ath1_data'):
                    if check_info['ath1_data']['seconds'] < best_ath:
                        best_ath = check_info['ath1_data']['seconds']
                        best_source = config.BOT_ATH1_USERNAME

                if self.ATH_BOT_MODE_RUNTIME == "BOTH" and \
                   check_info.get('ath2_status') == 'received_ok' and check_info.get('ath2_data'):
                    if check_info['ath2_data']['seconds'] < best_ath:
                        best_ath = check_info['ath2_data']['seconds']
                        best_source = config.BOT_ATH2_USERNAME

                if best_source: # Если нашли хотя бы один хороший ATH
                    logger.info(f"EVALUATE_ATH ({ca}): Финальное решение после всех ответов/таймаутов. Лучший ATH: {best_ath}s от {best_source}. Отправка в очередь.")
                    await self.ath_results_queue.put({
                        'ca': ca, 
                        'ath_seconds': best_ath, 
                        'source_bot': best_source
                    })
                    check_info['decision_made'] = True
                else:
                    logger.warning(f"EVALUATE_ATH ({ca}): Все ответы от ATH ботов получены/таймаут, но нет валидного ATH. Монета отбрасывается (decision_made=True).")
                    check_info['decision_made'] = True 
            elif made_change_by_timeout: # Если только что был таймаут, но еще есть pending (маловероятно при агрессивной стратегии)
                 logger.debug(f"EVALUATE_ATH ({ca}): Таймаут для одного из ботов, но решение еще не принято / не все ответы. Ожидание следующего вызова.")
            else:
                 logger.debug(f"EVALUATE_ATH ({ca}): Еще не все ответы/таймауты для принятия окончательного плохого решения. Ожидание.")

    async def _handle_buy_sell_response(self, text: str, source_bot_username: str):
        # Ваша текущая логика из GitHub для этого метода, но адаптированная для `source_bot_username`
        # Используйте AXIOM_CA_IN_URL_OR_BACKTICKS_REGEX и/или BUY_SELL_CA_REGEX для извлечения CA, если это необходимо
        logger.debug(f"BUYSELL_HANDLER ({source_bot_username}): Обработка ответа: {text[:100].replace('\n',' ')}...")
        # ... (дальнейшая логика парсинга Buy/Sell ответов) ...
        # Пример:
        result_data = {'ca': None, 'type': None, 'per': None, 'error_type': None, 'source_bot': source_bot_username}
        
        # Сначала ищем CA в известных паттернах
        ca_match_link = AXIOM_CA_IN_URL_OR_BACKTICKS_REGEX.search(text)
        if ca_match_link:
            ca_extracted = ca_match_link.group(1) or ca_match_link.group(2)
            if ca_extracted and utils.is_valid_solana_address(ca_extracted):
                 result_data['ca'] = ca_extracted
        
        # Если не нашли в ссылках/апострофах, ищем любой CA (старый метод как fallback)
        if not result_data['ca']:
            ca_match_general = BUY_SELL_CA_REGEX.search(text)
            if ca_match_general:
                ca_extracted_general = ca_match_general.group(1)
                if utils.is_valid_solana_address(ca_extracted_general):
                    result_data['ca'] = ca_extracted_general # Используем этот, если первый не сработал

        logger.debug(f"BUYSELL_HANDLER ({source_bot_username}): Извлеченный CA для обработки: {result_data['ca']}")

        # Далее ваша логика по проверке "Insufficient balance", "Buy Success!", маркеров продажи, "Transaction Failed!"
        # Важно: если ca не извлечен, некоторые проверки (кроме баланса) могут быть невозможны
        if INSUFFICIENT_BALANCE_REGEX.search(text):
            logger.critical(f"BUYSELL_HANDLER ({source_bot_username}): Ошибка 'Insufficient balance'!")
            result_data['error_type'] = 'balance'
            await self.buy_sell_results_queue.put(result_data)

            return # Завершаем обработку этого сообщения

        if not result_data['ca']:
            logger.warning(f"BUYSELL_HANDLER ({source_bot_username}): CA не извлечен, дальнейшая обработка ответа невозможна.")
            return # Не можем продолжить без CA

        if "Buy Success!" in text:
            result_data['type'] = 'buy'
            logger.info(f"BUYSELL_HANDLER ({source_bot_username}): 'Buy Success!' для CA {result_data['ca']}")
            await self.buy_sell_results_queue.put(result_data)
            return
        
        is_bot1_for_sell_marker = (config.BOT_BUYSELL1_USERNAME and source_bot_username.lstrip('@').lower() == config.BOT_BUYSELL1_USERNAME.lstrip('@').lower())
        current_sell_marker = config.SELL_MARKER1 if is_bot1_for_sell_marker else config.SELL_MARKER2

        if current_sell_marker and current_sell_marker in text: # Простая проверка вхождения маркера
            result_data['type'] = 'sell'
            per_match = BUY_SELL_PER_REGEX.search(text)
            if per_match:
                result_data['per'] = per_match.group(1)
            logger.info(f"BUYSELL_HANDLER ({source_bot_username}): Продажа для CA {result_data['ca']} с результатом {result_data['per']}")
            await self.buy_sell_results_queue.put(result_data)
            return

        if "Transaction Failed!" in text:
            logger.warning(f"BUYSELL_HANDLER ({source_bot_username}): 'Transaction Failed!' для CA {result_data['ca']}")
            result_data['error_type'] = 'failed'
            await self.buy_sell_results_queue.put(result_data)
            return
        
        logger.debug(f"BUYSELL_HANDLER ({source_bot_username}): Сообщение для CA {result_data['ca']} не опознано: {text[:100]}")



    async def _log_all_ptb_messages(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.message and update.message.text:
            user_id = update.effective_user.id if update.effective_user else "Unknown User"
            logger.critical(f"PTB_DEBUG_ALL_MSG: Получено сообщение от user_id={user_id}: '{update.message.text}'")
        elif update.callback_query:
            user_id = update.effective_user.id if update.effective_user else "Unknown User"
            logger.critical(f"PTB_DEBUG_ALL_MSG: Получен callback_query от user_id={user_id}: data='{update.callback_query.data}'")
        else:
            logger.critical(f"PTB_DEBUG_ALL_MSG: Получено обновление неизвестного типа: {update}")
        return

    def _register_ptb_handlers(self):
        if not self.ptb_app: 
            logger.error("PTB_HANDLER: PTB Application не инициализирован.")
            return
        
        self.ptb_app.add_handler(CommandHandler("start", self._handle_start_command))

        # !!! ДОБАВЛЯЕМ ТЕСТОВЫЙ ОБРАБОТЧИК ЗДЕСЬ !!!
        # Он будет логировать все текстовые сообщения, которые получает бот.
        # Добавляем его с высоким приоритетом (в отдельной группе), чтобы он сработал до других, если есть пересечения.
        # Используем filters.TEXT, чтобы не конфликтовать с CommandHandler для /start.
        # Если хочешь ловить АБСОЛЮТНО все, можно использовать filters.ALL, но это может быть слишком шумно.
        self.ptb_app.add_handler(MessageHandler(filters.ALL, self._log_all_ptb_messages), group= -1) # group = -1 для высокого приоритета
        logger.info("PTB_HANDLER: Тестовый обработчик _log_all_ptb_messages зарегистрирован.")
        # !!! КОНЕЦ ТЕСТОВОГО ОБРАБОТЧИКА !!!

        if config.USER_ADMIN_ID:
            try:
                admin_id_int = int(config.USER_ADMIN_ID)
                logger.info(f"PTB_HANDLER: Регистрация команд управления для ADMIN ID: {admin_id_int}")
            except ValueError:
                logger.error(f"PTB: USER_ADMIN_ID ('{config.USER_ADMIN_ID}') не является числом. Команды управления не будут работать.")
                return 
            
            stop_filters = (filters.COMMAND & filters.User(user_id=admin_id_int) & filters.Regex(r'^/stop$')) | \
                           (filters.TEXT & filters.User(user_id=admin_id_int) & filters.Regex(r'(?i)^Стоп парсер$'))
            self.ptb_app.add_handler(MessageHandler(stop_filters, self._handle_stop_command))
            logger.info("PTB_HANDLER: Обработчик для /stop (и 'Стоп парсер') зарегистрирован.")

            pause_filters = (filters.COMMAND & filters.User(user_id=admin_id_int) & filters.Regex(r'^/pause$')) | \
                            (filters.TEXT & filters.User(user_id=admin_id_int) & filters.Regex(r'(?i)^Пауза$'))
            self.ptb_app.add_handler(MessageHandler(pause_filters, self._handle_pause_command))
            logger.info("PTB_HANDLER: Обработчик для /pause (и 'Пауза') зарегистрирован.")

            play_filters = (filters.COMMAND & filters.User(user_id=admin_id_int) & filters.Regex(r'^/play$')) | \
                           (filters.TEXT & filters.User(user_id=admin_id_int) & filters.Regex(r'(?i)^(?:Play|Старт|Продолжить)$'))
            self.ptb_app.add_handler(MessageHandler(play_filters, self._handle_play_command))
            logger.info("PTB_HANDLER: Обработчик для /play (и 'Play', 'Старт', 'Продолжить') зарегистрирован.")
        else:
            logger.warning("PTB_HANDLER: config.USER_ADMIN_ID не задан. Команды управления НЕ БУДУТ зарегистрированы.")

    async def _handle_start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if user:
            logger.info(f"PTB: /start от {user.id} ({user.username})")
            await update.message.reply_html(f"Привет, {user.mention_html()}! Я Axiom Volume Bot.")
        else:
            logger.info("PTB: /start получен, но user не определен.")
            await update.message.reply_text("Привет! Я Axiom Volume Bot.")
        pass

    async def _handle_stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user # Здесь user должен быть, т.к. фильтр по ID
        logger.critical(f"PTB: !!! КОМАНДА СТОП от админа {user.id} ({user.username}) !!!")
        await update.message.reply_text("Команда на остановку получена. Завершаю работу...")
        if self._stop_callback:
            logger.info("PTB: Вызов _stop_callback...")
            asyncio.create_task(self._stop_callback()) # Запускаем асинхронно
        else:
            logger.warning("PTB: _stop_callback не установлен!")

    # --- НОВЫЕ МЕТОДЫ ДЛЯ КОМАНД PAUSE/PLAY ---
    async def _handle_pause_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        # Добавляем более детальное логирование для диагностики
        logger.info(f"PTB: Вход в _handle_pause_command. Сообщение от user_id={user.id if user else 'N/A'}, username='{user.username if user else 'N/A'}'. Текст: '{update.message.text}'")
        if not (user and user.id == int(config.USER_ADMIN_ID)): # Дополнительная проверка, хотя фильтр должен работать
             logger.warning(f"PTB: _handle_pause_command вызван, но user ID {user.id if user else 'N/A'} не совпадает с USER_ADMIN_ID {config.USER_ADMIN_ID}. Игнорируется.")
             return

        await update.message.reply_text("Команда ПАУЗА получена. Приостановка бота...")
        if self._pause_callback:
            logger.info(f"PTB: Вызов _pause_callback для команды /pause от {user.id}.")
            asyncio.create_task(self._pause_callback(auto_triggered=False)) 
        else:
            logger.warning("PTB: _pause_callback не установлен! Не могу поставить на паузу.")
            await update.message.reply_text("⚠️ Ошибка: функция паузы не настроена.")

    async def _handle_play_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        logger.info(f"PTB: Вход в _handle_play_command. Сообщение от user_id={user.id if user else 'N/A'}, username='{user.username if user else 'N/A'}'. Текст: '{update.message.text}'")
        if not (user and user.id == int(config.USER_ADMIN_ID)):
             logger.warning(f"PTB: _handle_play_command вызван, но user ID {user.id if user else 'N/A'} не совпадает с USER_ADMIN_ID {config.USER_ADMIN_ID}. Игнорируется.")
             return

        await update.message.reply_text("Команда PLAY получена. Возобновление работы бота...")
        if self._play_callback:
            logger.info(f"PTB: Вызов _play_callback для команды /play от {user.id}.")
            asyncio.create_task(self._play_callback())
        else:
            logger.warning("PTB: _play_callback не установлен! Не могу возобновить работу.")
            await update.message.reply_text("⚠️ Ошибка: функция возобновления работы не настроена.")


    # --- КОНЕЦ НОВЫХ МЕТОДОВ ---

    async def send_notification_to_admin(self, message: str):
        if not (self.ptb_bot and config.USER_ADMIN_ID):
            logger.error(f"PTB: Уведомление админу не отправлено (бот/ID не настроен): {message[:70]}")
            return
        try:
            await self.ptb_bot.send_message(chat_id=config.USER_ADMIN_ID, text=message, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        except Exception as e: logger.error(f"PTB: Ошибка отправки админу: {e}", exc_info=True)
        pass
    async def send_notification_to_group(self, message: str, reply_to_id: Optional[int] = None) -> Optional[int]:
        if not (self.ptb_bot and config.GROUP_NOTIFY_ID):
            logger.error(f"PTB: Уведомление в группу не отправлено (бот/ID не настроен): {message[:70]}")
            return None
        try:
            reply_params = ReplyParameters(message_id=reply_to_id) if reply_to_id else None
            sent_msg = await self.ptb_bot.send_message(chat_id=config.GROUP_NOTIFY_ID, text=message, parse_mode=ParseMode.HTML, reply_parameters=reply_params, disable_web_page_preview=True)
            logger.debug(f"PTB: Сообщение в группу {config.GROUP_NOTIFY_ID} отправлено, ID: {sent_msg.message_id}")
            return sent_msg.message_id
        except Exception as e: logger.error(f"PTB: Ошибка отправки в группу: {e}", exc_info=True)
        return None
        pass