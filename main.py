# main.py
import asyncio
import logging
import signal # Для обработки Ctrl+C
import sys
import re
from datetime import datetime
from typing import Dict, Any, Optional

# Планировщик задач
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Импортируем наши модули
import config
import database
import utils
from browser_parser import BrowserParser
from telegram_handler import TelegramHandler

# --- Глобальные переменные и объекты ---
logger: Optional[logging.Logger] = None
browser_parser: Optional[BrowserParser] = None
telegram_handler: Optional[TelegramHandler] = None
scheduler: Optional[AsyncIOScheduler] = None
# Словарь для хранения данных монет, ожидающих ответа ATH
# Ключ - CA (адрес контракта), Значение - словарь с данными монеты (из парсера)
pending_ath_checks: Dict[str, Dict[str, Any]] = {} 
# Событие для сигнализации о необходимости завершения работы
shutdown_event = asyncio.Event()

# --- Основная логика парсинга и обработки ---

async def run_parsing_cycle():
    """
    Выполняет один цикл парсинга страницы Axiom и отправки CA для проверки ATH.
    Запускается планировщиком APScheduler.
    Логика шагов Б.1 - Б.8.
    """
    if shutdown_event.is_set():
        logger.info("Завершение работы: Пропуск цикла парсинга.")
        return
        
    if not browser_parser:
        logger.error("Экземпляр BrowserParser не инициализирован.")
        return
    if not telegram_handler:
         logger.error("Экземпляр TelegramHandler не инициализирован.")
         return

    logger.info("="*10 + " Запуск цикла парсинга Axiom " + "="*10)
    
    # Шаг Б.3 - Б.5: Парсинг данных со страницы
    parsed_coins = await browser_parser.parse_discover_page()

    if parsed_coins is None:
        logger.error("Ошибка при парсинге страницы Axiom. Цикл прерван.")
        return
    if not parsed_coins:
        logger.info("На странице Axiom не найдено подходящих монет для обработки.")
        # --- УВЕДОМЛЕНИЕ АДМИНУ, ЧТО НЕТ МОНЕТ (если нужно) ---
        if telegram_handler:
            await telegram_handler.send_notification_to_admin("ℹ️ На Axiom не найдено монет для обработки в текущем цикле.")
        # -------------------------------------------------
        logger.info("="*10 + " Конец цикла парсинга Axiom " + "="*10)
        return
        
    logger.info(f"Спарсено {len(parsed_coins)} монет. Фильтрация и отправка на проверку ATH...")
    
    # --- ФОРМИРОВАНИЕ И ОТПРАВКА ПОДРОБНОГО ОТЧЕТА О ПАРСИНГЕ ---
    if telegram_handler:
        report_message = f"<b>🔎 Отчет о парсинге Axiom ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})</b>\n"
        report_message += f"Найдено монет: {len(parsed_coins)}\n\n"
        
        # Ограничим количество монет в отчете, чтобы сообщение не было слишком длинным
        coins_to_report = parsed_coins[:15] # Например, первые 15
        
        for idx, coin in enumerate(coins_to_report):
            tic = coin.get('tic', 'N/A')
            age = coin.get('age_str', '?')
            mc_val = coin.get('mc', 0)
            l_val = coin.get('l', 0)
            v_val = coin.get('v', 0)
            ca_short = coin.get('ca', 'NO_CA')[:6] + "..." if coin.get('ca') != 'N/A' else "NO_CA"

            report_message += (
                f"<b>{idx+1}. {tic}</b> (<code>{ca_short}</code>)\n"
                f"   Возраст: {age}, MC: ${mc_val:,}, Liq: ${l_val:,}, Vol: ${v_val:,}\n"
            )
        
        if len(parsed_coins) > len(coins_to_report):
            report_message += f"\n<i>... и еще {len(parsed_coins) - len(coins_to_report)} монет.</i>"
            
        await telegram_handler.send_notification_to_admin(report_message)
    # -----------------------------------------------------------
    
    processed_count = 0
    # Шаг Б.6 - Б.8: Итерация, фильтрация и отправка CA
    for coin_data in parsed_coins:
        if shutdown_event.is_set():
            logger.info("Завершение работы: Прерывание обработки монет.")
            break # Выходим из цикла обработки монет

        ca = coin_data.get('ca')
        age_str = coin_data.get('age_str')
        
        if not ca or not age_str:
            logger.warning(f"Отсутствует CA или Age в данных монеты: {coin_data}. Пропуск.")
            continue
            
        # Шаг Б.7 (Фильтр по возрасту)
        # Отфильтровываем монеты <= 8 минут ('s', '1m'...'8m')
        age_match = re.match(r"(\d+)([sm])", age_str) # Ищем секунды или минуты
        if age_match:
            value = int(age_match.group(1))
            unit = age_match.group(2)
            if unit == 's' or (unit == 'm' and value <= 8):
                 logger.debug(f"Монета {coin_data.get('tic', ca)} слишком молодая ({age_str}). Пропуск.")
                 continue

        # Добавляем монету в словарь ожидания ПЕРЕД отправкой запросов
        # Это гарантирует, что у нас будут данные, когда придет ответ
        pending_ath_checks[ca] = coin_data
        logger.debug(f"Монета {coin_data.get('tic', ca)} ({ca}) добавлена в ожидание ATH.")
        
        # Шаг Б.8 (Отправка CA ботам ATH с задержками)
        logger.info(f"Отправка CA {ca} ({coin_data.get('tic')}) на проверку ATH...")
        
        # Отправка первому боту
        sent1 = await telegram_handler.send_message_to_user_bot(config.BOT_ATH1_USERNAME, ca)
        await asyncio.sleep(1.1) # Пауза > 1 сек
        
        # Отправка второму боту
        sent2 = await telegram_handler.send_message_to_user_bot(config.BOT_ATH2_USERNAME, ca)
        await asyncio.sleep(1.1) # Пауза > 1 сек

        if not sent1 and not sent2:
             logger.error(f"Не удалось отправить CA {ca} ни одному ATH боту.")
             # Удаляем из ожидания, если не удалось отправить
             pending_ath_checks.pop(ca, None) 
        else:
             processed_count += 1

    logger.info(f"Отправлено {processed_count} CA на проверку ATH.")
    logger.info("="*10 + " Конец цикла парсинга Axiom " + "="*10)


async def process_ath_results():
    """
    Асинхронно обрабатывает результаты проверки ATH из очереди telegram_handler.
    Реализует логику шагов В.3 - В.5 (обработка ответа ATH, фильтры, отправка в Buy/Sell боты, запись в БД).
    """
    if not telegram_handler or not database:
        logger.error("TelegramHandler или Database не инициализированы. Не могу обрабатывать ATH результаты.")
        return
        
    logger.info("Запуск обработчика результатов ATH...")
    while not shutdown_event.is_set():
        try:
            # Ожидаем результат из очереди
            result = await asyncio.wait_for(telegram_handler.ath_results_queue.get(), timeout=1.0)
            
            ca = result.get('ca')
            error = result.get('error')
            source_bot = result.get('source_bot', 'N/A')
            
            if not ca:
                logger.warning(f"Получен ATH результат без CA: {result}")
                telegram_handler.ath_results_queue.task_done()
                continue

            # Получаем полные данные монеты из словаря ожидания
            original_coin_data = pending_ath_checks.get(ca)
            
            # Обрабатываем результат только если он еще ожидается 
            # (Например, если от одного бота пришел ответ, а от другого ошибка позже)
            if not original_coin_data:
                 logger.debug(f"Получен ATH результат для {ca}, но он уже не ожидается (возможно, обработан ранее или была ошибка отправки). Игнорируем.")
                 telegram_handler.ath_results_queue.task_done()
                 continue

            # Если пришла ошибка от бота
            if error:
                logger.warning(f"Ошибка при проверке ATH для {ca} от {source_bot}: {error}. Проверяем, есть ли ответ от другого бота...")
                logger.info(f"ATH_PROCESSOR: {ca} отброшен из-за ошибки от бота.")
                # Здесь можно реализовать логику ожидания ответа от второго бота, если нужно
                # Например, пометить, что от одного пришла ошибка, и ждать таймаут второго.
                # Пока для простоты: если пришла ошибка, считаем проверку ATH неудачной.
                logger.warning(f"Проверка ATH для {ca} ({original_coin_data.get('tic')}) не удалась.")
                pending_ath_checks.pop(ca, None) # Удаляем из ожидания
                telegram_handler.ath_results_queue.task_done()
                continue
                
            # Если ошибки нет, значит пришел ath_seconds
            ath_seconds = result.get('ath_seconds')
            if ath_seconds is None: # На всякий случай
                logger.error(f"Получен успешный ATH результат для {ca}, но без ath_seconds: {result}")
                pending_ath_checks.pop(ca, None) 
                telegram_handler.ath_results_queue.task_done()
                continue
                
            # --- Логика после успешного получения ATH ---
            logger.info(f"Успешно получен ATH ({ath_seconds}s) для {ca} ({original_coin_data.get('tic')}) от {source_bot}. Обработка...")
            
            # Шаг В.5.2 (Фильтр по возрасту ATH)
            if ath_seconds > config.THRESHOLD_ATH_SECONDS:
                logger.info(f"ATH {ath_seconds}s для {ca} ({original_coin_data.get('tic')}) > порога {config.THRESHOLD_ATH_SECONDS}s. Пропуск.")
                logger.info(f"ATH_PROCESSOR: {ca} отброшен из-за возраста ATH.")
                pending_ath_checks.pop(ca, None) 
                telegram_handler.ath_results_queue.task_done()
                continue
                
            # Шаг В.5.3 (Проверка статуса в БД)
            # Используем run_in_executor для синхронного вызова БД
            loop = asyncio.get_running_loop()
            current_status = await loop.run_in_executor(None, database.get_coin_status, ca)
            
            if current_status in ["Processing", "Buy Success!"]:
                # ---------------------------
                logger.info(f"Монета {ca} ({original_coin_data.get('tic')}) уже в процессе покупки или успешно куплена (статус: {current_status}). Пропуск.")
                logger.info(f"ATH_PROCESSOR: {ca} отброшен из-за статуса в БД.") 
                pending_ath_checks.pop(ca, None) 
                telegram_handler.ath_results_queue.task_done()
                continue
                
            # --- Монета прошла все проверки - отправляем на покупку ---
            logger.info(f"Монета {ca} ({original_coin_data.get('tic')}) ПРОШЛА все проверки. Отправка на покупку...")
            
            volume = original_coin_data.get('v', 0)
            target_bot_username = None
            volume_text = ""

            # Шаг В.5.4 (Выбор бота Buy/Sell)
            if volume < config.THRESHOLD_VOLUME:
                target_bot_username = config.BOT_BUYSELL1_USERNAME
                volume_text = f"МЕНЬШЕ {config.THRESHOLD_VOLUME}"
                group_emoji = "🚀"
            else:
                target_bot_username = config.BOT_BUYSELL2_USERNAME
                volume_text = f"БОЛЬШЕ или РАВНО {config.THRESHOLD_VOLUME}"
                group_emoji = "💥"

            # Отправка CA боту Buy/Sell
            sent_to_buy_bot = await telegram_handler.send_message_to_user_bot(target_bot_username, ca)
            
            if not sent_to_buy_bot:
                logger.error(f"Не удалось отправить CA {ca} боту {target_bot_username}.")
                # Не удаляем из pending, возможно, стоит повторить позже? Пока оставляем как есть.
                telegram_handler.ath_results_queue.task_done()
                continue # Не отправляем уведомления и не пишем в БД, если не ушло боту

            # Формирование сообщений
            current_time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            notify_admin_msg = (
                f"<b>✅ Подходящая монета!</b>\n"
                f"<i>({current_time_str})</i>\n\n"
                f"<b>Тикер:</b> {original_coin_data.get('tic', 'N/A')}\n"
                f"<b>CA:</b> <code>{ca}</code>\n"
                f"<b>Возраст:</b> {original_coin_data.get('age_str', 'N/A')}\n"
                f"<b>MC:</b> ${original_coin_data.get('mc', 0):,}\n"
                f"<b>Liq:</b> ${original_coin_data.get('l', 0):,}\n"
                f"<b>Volume:</b> ${original_coin_data.get('v', 0):,}\n"
                f"<b>ATH Age:</b> {ath_seconds}s\n\n"
                f"<b>Условие:</b> Объем {volume_text}\n"
                f"<b>Отправлено в:</b> {target_bot_username}"
            )
            
            notify_group_msg = (
                 f"{group_emoji} <b>Новый сигнал | {original_coin_data.get('tic', 'N/A')}</b>\n"
                 f"<i>({current_time_str})</i>\n\n"
                 f"<b>CA:</b> <code>{ca}</code>\n"
                 f"<b>Возраст:</b> {original_coin_data.get('age_str', 'N/A')}\n"
                 f"<b>MC при сигнале:</b> ${original_coin_data.get('mc', 0):,}\n"
                 f"<b>Объем при сигнале:</b> ${original_coin_data.get('v', 0):,}\n"
                 f"<b>Возраст ATH:</b> {ath_seconds}s"
            )

            # Отправка уведомлений
            await telegram_handler.send_notification_to_admin(notify_admin_msg)
            group_message_id = await telegram_handler.send_notification_to_group(notify_group_msg)

            # Шаг В.5.5 (Запись/Обновление в БД)
            db_data = {
                **original_coin_data, # Копируем все исходные данные
                'ath_seconds': ath_seconds,
                'status': "Processing", # Устанавливаем статус
                'group_message_id': group_message_id # Сохраняем ID сообщения группы
            }
            await loop.run_in_executor(None, database.upsert_coin, db_data)
            
            # Удаляем из ожидания после успешной обработки
            pending_ath_checks.pop(ca, None)
            
            telegram_handler.ath_results_queue.task_done()

        except asyncio.TimeoutError:
            # Очередь пуста, просто продолжаем ждать
            pass
        except Exception as e:
            logger.error(f"Ошибка в цикле обработки ATH результатов: {e}", exc_info=True)
            # Помечаем задачу как выполненную, чтобы не заблокировать очередь при ошибке
            if 'result' in locals() and telegram_handler:
                 telegram_handler.ath_results_queue.task_done()
            await asyncio.sleep(1) # Небольшая пауза перед следующей попыткой

async def process_buy_sell_results():
    """
    Асинхронно обрабатывает результаты покупки/продажи/ошибки из очереди telegram_handler.
    Обновляет статус в БД и отправляет уведомления.
    """
    if not telegram_handler or not database:
         logger.error("TelegramHandler или Database не инициализированы. Не могу обрабатывать Buy/Sell результаты.")
         return

    logger.info("Запуск обработчика результатов Buy/Sell...")
    while not shutdown_event.is_set():
        try:
            result = await asyncio.wait_for(telegram_handler.buy_sell_results_queue.get(), timeout=1.0)
            
            ca = result.get('ca')
            result_type = result.get('type') 
            percentage = result.get('per') 
            error_type = result.get('error_type') 
            source_bot = result.get('source_bot', 'N/A')

            # Обработка остановки по ошибке баланса
            if error_type == 'balance':
                 logger.critical(f"Обработчик Buy/Sell получил сигнал остановки из-за ошибки баланса (CA: {ca}).")
                 telegram_handler.buy_sell_results_queue.task_done()
                 continue 

            # Если нет CA (кроме ошибки баланса), не можем обработать
            if not ca:
                logger.warning(f"Получен Buy/Sell результат без CA: {result}")
                telegram_handler.buy_sell_results_queue.task_done()
                continue
            
            # Получаем текущие детали монеты из БД
            loop = asyncio.get_running_loop()
            coin_details = await loop.run_in_executor(None, database.get_coin_details, ca)
            
            if not coin_details:
                 logger.warning(f"Получен Buy/Sell результат для CA {ca}, но он не найден в БД. Result: {result}")
                 telegram_handler.buy_sell_results_queue.task_done()
                 continue
                 
            tic = coin_details.get('tic', 'N/A')
            group_message_id = coin_details.get('group_message_id')

            # Обработка разных типов результатов
            if result_type == 'buy':
                logger.info(f"Обработка 'Buy Success!' для {ca} ({tic}).")
                await loop.run_in_executor(None, database.update_coin_status, ca, "Buy Success!")
                await telegram_handler.send_notification_to_admin(f"✅ Успешно купили: {tic} (<code>{ca}</code>) через {source_bot}")

            elif result_type == 'sell':
                logger.info(f"Обработка продажи для {ca} ({tic}) с результатом {percentage}.")
                await loop.run_in_executor(None, database.update_coin_status, ca, "Sell Success!")
                
                admin_msg = f"💰 Продали: {tic} (<code>{ca}</code>) с результатом {percentage} через {source_bot}"
                # Используем html.escape для тикера на всякий случай
                import html
                group_msg = f"💰 {html.escape(tic)} | Результат: {html.escape(str(percentage))}" 
                
                await telegram_handler.send_notification_to_admin(admin_msg)
                if group_message_id: 
                     await telegram_handler.send_notification_to_group(group_msg, reply_to_id=group_message_id)
                else:
                     logger.warning(f"Не найден group_message_id для CA {ca}, отправка в группу без реплая.")
                     await telegram_handler.send_notification_to_group(group_msg)
            
            # --- ДОБАВЛЕНА ОБРАБОТКА Transaction Failed ---
            elif error_type == 'failed':
                logger.warning(f"Обработка 'Transaction Failed' для CA {ca} от {source_bot}.")
                # Обновляем статус на "Buy Failed", чтобы разрешить повторную покупку
                await loop.run_in_executor(None, database.update_coin_status, ca, "Buy Failed") 
                await telegram_handler.send_notification_to_admin(f"⚠️ Транзакция покупки для {tic} (<code>{ca}</code>) не удалась (failed) через {source_bot}. Статус обновлен на Buy Failed.")
            # -----------------------------------------------
                
            # Помечаем задачу из очереди как выполненную
            telegram_handler.buy_sell_results_queue.task_done()

        except asyncio.TimeoutError:
            pass # Очередь пуста
        except Exception as e:
            logger.error(f"Ошибка в цикле обработки Buy/Sell результатов: {e}", exc_info=True)
            # Помечаем задачу как выполненную в случае ошибки, чтобы не блокировать очередь
            if 'result' in locals() and telegram_handler:
                 telegram_handler.buy_sell_results_queue.task_done()
            await asyncio.sleep(1) 
# ------------------------------------------------------

# --- Управление Завершением Работы ---

async def trigger_shutdown():
    """Устанавливает событие завершения работы."""
    if not shutdown_event.is_set():
        logger.info("Получен сигнал завершения работы...")
        shutdown_event.set()

def signal_handler(sig, frame):
    """Обработчик сигналов ОС (SIGINT, SIGTERM)."""
    logger.info(f"Получен сигнал {sig}. Запускаем процедуру завершения...")
    # Используем call_soon_threadsafe, так как обработчик сигнала может быть вызван из другого потока
    asyncio.get_running_loop().call_soon_threadsafe(lambda: asyncio.create_task(trigger_shutdown()))

async def main():
    """Основная асинхронная функция приложения."""
    global logger, browser_parser, telegram_handler, scheduler
    
    # 1. Настройка
    logger = config.setup_logging()
    logger.info("="*20 + " ЗАПУСК AXIOM VOLUME BOT " + "="*20)
    database.create_tables() # Убедимся, что таблица существует
    
    browser_parser = BrowserParser()
    # Передаем текущий цикл событий в TelegramHandler
    loop = asyncio.get_running_loop()
    # main.py (если используете НОВЫЙ telegram_handler.py)
    telegram_handler = TelegramHandler(loop)
    scheduler = AsyncIOScheduler(event_loop=loop)

    # 2. Обработка сигналов завершения
    for sig_name in ('SIGINT', 'SIGTERM'):
        if hasattr(signal, sig_name):
             try:
                 # Устанавливаем обработчик только если основной поток - тот, где работает asyncio
                 signal.signal(getattr(signal, sig_name), signal_handler)
             except ValueError: # Может возникнуть, если запуск не из основного потока
                 logger.warning(f"Не удалось установить обработчик для сигнала {sig_name}")
    
    # 3. Предстартовая логика
    
    # Очистка БД
    clear_db = input("Очистить базу данных монет перед запуском? (Да/Нет): ").strip().lower()
    if clear_db == 'да':
        database.clear_all_coin_data()
        
    # Запуск браузера
    if not await browser_parser.launch_browser():
         logger.critical("Не удалось запустить браузер. Завершение работы.")
         return # Выход, если браузер не запустился
         
    # Переход на страницу
    if not await browser_parser.navigate_to_axiom():
         logger.critical("Не удалось перейти на страницу Axiom. Завершение работы.")
         await browser_parser.close_browser()
         return
         
    # Уведомление админу о запуске
    #await telegram_handler.send_notification_to_admin("🤖 Бот Axiom Volume запущен. Ожидание входа пользователя...") # Временно так, THandler еще не подключен
    
    # Ожидание входа пользователя
    print("\n" + "="*30)
    print("ПОЖАЛУЙСТА, ВОЙДИТЕ В АККАУНТ AXIOM В ОТКРЫВШЕМСЯ ОКНЕ БРАУЗЕРА.")
    print("Настройте нужные фильтры (если требуется).")
    # Используем run_in_executor для input, чтобы не блокировать asyncio
    await loop.run_in_executor(None, input, "После входа и настройки фильтров, нажмите Enter здесь для продолжения...")
    print("="*30 + "\n")

    # Проверка входа
    if not await browser_parser.wait_for_login_confirmation():
        logger.critical("Вход не подтвержден (не найден 'PRESET 1'). Завершение работы.")
        await telegram_handler.send_notification_to_admin("⚠️ Вход в Axiom не подтвержден. Бот остановлен.") # Временно
        await browser_parser.close_browser()
        return
        
    logger.info("Вход подтвержден. Запуск основных компонентов...")

    # 4. Подключение Telegram и запуск задач
    
    # Подключаем Telegram, передавая колбэк для остановки
    if not await telegram_handler.connect_clients(stop_callback=trigger_shutdown):
         logger.critical("Не удалось подключить Telegram клиенты. Завершение работы.")
         await browser_parser.close_browser()
         return

    # Уведомление админу о запуске
    await telegram_handler.send_notification_to_admin("🤖 Бот Axiom Volume запущен. Ожидание входа пользователя...") # Временно так, THandler еще не подключен
    
      
    # Теперь можно отправлять уведомления через подключенный Telegram
    await telegram_handler.send_notification_to_admin("🚀 Основные компоненты запущены. Начинаю работу...")
    
    # Запускаем задачи обработки очередей
    ath_processor_task = loop.create_task(process_ath_results(), name="ATH_Processor")
    buy_sell_processor_task = loop.create_task(process_buy_sell_results(), name="BuySell_Processor")
    
    # Добавляем задачу парсинга в планировщик
    try:
        scheduler.add_job(
            run_parsing_cycle, 
            trigger='cron', 
            second=config.CHTIME, 
            id='axiom_parsing_job', 
            replace_existing=True,
            misfire_grace_time=15 # Разрешаем запуск с опозданием до 15 сек
        )
        scheduler.start()
        logger.info(f"Планировщик запущен. Парсинг будет выполняться каждую минуту в {config.CHTIME} секунд.")
    except Exception as e:
         logger.critical(f"Не удалось запустить планировщик: {e}", exc_info=True)
         await trigger_shutdown() # Запускаем завершение, если планировщик упал

    # 5. Ожидание сигнала завершения
    logger.info("Приложение работает. Ожидание сигнала завершения (Ctrl+C)...")
    await shutdown_event.wait() # Блокируемся здесь, пока не будет вызван trigger_shutdown

    # 6. Завершение работы
    logger.info("Начало процедуры завершения работы...")
    
    # Остановка планировщика
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("Планировщик остановлен.")
        
    # Отмена задач обработки очередей
    logger.info("Отмена задач обработки очередей...")
    ath_processor_task.cancel()
    buy_sell_processor_task.cancel()
    try:
        # Даем задачам шанс завершиться после отмены
        await asyncio.gather(ath_processor_task, buy_sell_processor_task, return_exceptions=True)
        logger.info("Задачи обработки очередей завершены.")
    except asyncio.CancelledError:
         logger.info("Задачи обработки очередей были отменены.")
         
    # Отключение Telegram
    if telegram_handler:
        await telegram_handler.disconnect_clients()
    
    # --- ДОБАВИТЬ ЗАДЕРЖКУ ---
    logger.info("Небольшая пауза перед закрытием браузера...")
    await asyncio.sleep(2) # Пауза в 2 секунды
    # --------------------------
    
    # Закрытие браузера
    if browser_parser:
        await browser_parser.close_browser()
        
    # Сообщение админу о завершении
    # Попытка отправить через уже отключенный клиент может не сработать,
    # но можно попробовать или отправить перед отключением
    # await telegram_handler.send_notification_to_admin("끄 Бот Axiom Volume остановлен.")

    logger.info("="*20 + " ЗАВЕРШЕНИЕ РАБОТЫ AXIOM VOLUME BOT " + "="*20)


if __name__ == "__main__":
    # Устанавливаем политику цикла событий для Windows, если необходимо
    #if sys.platform == "win32":
    #    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Приложение прервано пользователем (KeyboardInterrupt).")
    except Exception as e:
         # Логируем критические ошибки, которые могли произойти до настройки логгера
         print(f"Критическая ошибка при запуске main: {e}", file=sys.stderr)
         if logger:
             logger.critical(f"Критическая ошибка в main: {e}", exc_info=True)
         else: # Если логгер не успел настроиться
             logging.basicConfig(level=logging.ERROR)
             logging.critical(f"Критическая ошибка в main до настройки логгера: {e}", exc_info=True)