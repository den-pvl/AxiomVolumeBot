# main.py
import asyncio
import logging
import signal # Для обработки Ctrl+C
import sys
import re
from datetime import datetime, timedelta
from typing import Dict, Any, Optional
import html
# ... другие импорты ...
from telegram import Update
from telegram.ext import Application
from telegram.error import TelegramError

# Планировщик задач
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import STATE_PAUSED, STATE_STOPPED, STATE_RUNNING # Импортируем состояния APScheduler
# Импортируем наши модули
import config # config будет использоваться для других настроек
import database
import utils
from browser_parser import BrowserParser
from telegram_handler import TelegramHandler


# --- Глобальные переменные и объекты ---
logger: Optional[logging.Logger] = None
browser_parser: Optional[BrowserParser] = None
telegram_handler: Optional[TelegramHandler] = None
scheduler: Optional[AsyncIOScheduler] = None
pending_ath_checks: Dict[str, Dict[str, Any]] = {}

# --- События для управления состоянием ---
shutdown_event = asyncio.Event() # Для полной остановки
pause_event = asyncio.Event()    # Для постановки на паузу (set = paused, clear = running)

# --- Статус бота ---
# Возможные значения: "running", "pausing", "paused", "resuming", "stopping"
bot_status: str = "initializing" # Начальный статус

ATH_BOT_MODE_RUNTIME: str = "BOTH" # Значение по умолчанию, будет перезаписано из input()

# --- Новая вспомогательная функция для очистки очередей ---
async def _clear_async_queue(q: asyncio.Queue, queue_name: str):
    """Асинхронно очищает указанную очередь asyncio.Queue."""
    items_cleared = 0
    while not q.empty():
        try:
            item = q.get_nowait() 
            q.task_done() # Сообщаем очереди, что элемент обработан (важно, если где-то используется q.join())
            items_cleared += 1
            logger.debug(f"MAIN: Из очереди {queue_name} удален элемент: {item}")
        except asyncio.QueueEmpty:
            break # Очередь стала пустой во время итерации
    if items_cleared > 0:
        logger.info(f"MAIN: Очередь {queue_name} очищена. Удалено {items_cleared} элементов.")

# --- Новая функция для активации режима ПАУЗЫ ---
async def trigger_pause(auto_triggered: bool = False, reason: Optional[str] = None):
    global bot_status # Используем nonlocal для изменения глобальной переменной в main() или global если на уровне модуля
    
    if bot_status == "paused":
        msg = "MAIN: Команда PAUSE получена, но бот уже на паузе."
        if auto_triggered and reason: 
            msg = f"MAIN: Попытка авто-паузы ({reason}), но бот уже на паузе."
        logger.info(msg)
        if telegram_handler and not auto_triggered: # Уведомляем только при ручной команде, если уже на паузе
            await telegram_handler.send_notification_to_admin("ℹ️ Бот уже находится в режиме паузы.")
        return

    log_prefix = "MAIN (AUTO-PAUSE)" if auto_triggered else "MAIN (CMD PAUSE)"
    reason_str = f" (Причина: {reason})" if reason else ""
    
    logger.info(f"{log_prefix}: Активация режима паузы{reason_str}...")
    if telegram_handler:
        if auto_triggered:
            await telegram_handler.send_notification_to_admin(
                f"⚠️ Автоматическая пауза бота!\n<b>Причина:</b> {reason or 'Не указана'}\n⏳ Переход в режим паузы..."
            )
        else:
            await telegram_handler.send_notification_to_admin("⏳ Бот переходит в режим паузы по команде...")
    
    bot_status = "pausing"

    # 1. Приостановка планировщика
    if scheduler and scheduler.running and scheduler.state != STATE_PAUSED:
        try:
            scheduler.pause()
            logger.info(f"{log_prefix}: Планировщик APScheduler поставлен на паузу.")
        except Exception as e:
            logger.error(f"{log_prefix}: Ошибка при постановке планировщика на паузу: {e}")
    
    # 2. Установка события паузы (влияет на run_parsing_cycle и обработчики очередей)
    pause_event.set()
    
    # 3. Очистка очередей (согласно требованию)
    if telegram_handler:
        log_prefix_local = "MAIN (AUTO-PAUSE)" if auto_triggered else "MAIN (CMD PAUSE)"
        logger.info(f"{log_prefix_local}: Очистка очереди ATH результатов...")
        await _clear_async_queue(telegram_handler.ath_results_queue, "ATH results")
        logger.info(f"{log_prefix_local}: Очередь Buy/Sell результатов НЕ очищается при постановке на паузу.")
        
    
    bot_status = "paused"
    logger.info(f"{log_prefix}: Бот успешно поставлен на паузу{reason_str}.")
    if telegram_handler:
        await telegram_handler.send_notification_to_admin(f"✅ Бот успешно приостановлен{reason_str}.\n Ожидание команд /play или /stop.")

# --- Новая функция для ВОЗОБНОВЛЕНИЯ работы ---
async def trigger_play():
    global bot_status
    
    if bot_status != "paused":
        logger.info(f"MAIN: Команда PLAY получена, но бот не на паузе (текущий статус: {bot_status}).")
        if telegram_handler:
            await telegram_handler.send_notification_to_admin(
                f"ℹ️ Бот не на паузе (статус: {bot_status}). Команда /play не выполнена."
            )
        return

    logger.info("MAIN: Возобновление работы бота по команде /play...")
    

    if telegram_handler: await telegram_handler.send_notification_to_admin("▶️ Возобновление работы бота...")
    bot_status = "resuming"

    # 1. Сначала снимаем событие паузы, чтобы циклы могли продолжить
    pause_event.clear() 

    # 2. Возобновление планировщика
    if scheduler:
        if scheduler.state == STATE_PAUSED:
            try:
                scheduler.resume()
                logger.info("MAIN: Планировщик APScheduler возобновлен.")
            except Exception as e:
                logger.error(f"MAIN: Ошибка при возобновлении планировщика: {e}", exc_info=True)
        elif scheduler.state == STATE_STOPPED:
             logger.warning("MAIN: Планировщик был остановлен (не на паузе). Попытка запустить заново...")
             try:
                 scheduler.start(paused=False) # APScheduler 4.x+ use start(paused=False) or just start()
                 logger.info("MAIN: Планировщик APScheduler запущен заново (был ранее остановлен).")
             except Exception as e:
                 logger.error(f"MAIN: Ошибка при перезапуске остановленного планировщика: {e}", exc_info=True)
        elif scheduler.state == STATE_RUNNING:
            logger.info("MAIN: Планировщик APScheduler уже запущен.")
        else:
            logger.warning(f"MAIN: Неизвестное состояние планировщика ({scheduler.state}) при попытке возобновить работу.")
            
    bot_status = "running"
    logger.info("MAIN: Бот возобновил работу.")
    if telegram_handler: await telegram_handler.send_notification_to_admin("✅ Бот возобновил работу.")


# --- Логика ПОЛНОГО завершения работы ---

async def trigger_shutdown():
    global bot_status # Убедись, что logger и telegram_handler доступны (глобальные или переданы)
    if not shutdown_event.is_set():
        logger.info("MAIN: Получен сигнал trigger_shutdown.")
        
        # Сначала пытаемся отправить уведомление
        if telegram_handler and hasattr(telegram_handler, 'send_notification_to_admin'):
            try:
                logger.info("MAIN: Попытка отправить уведомление \"🛑 Бот Axiom Volume останавливается...\" из trigger_shutdown.")
                await telegram_handler.send_notification_to_admin("🛑 Бот Axiom Volume останавливается...")
                logger.info("MAIN: Уведомление об остановке (из trigger_shutdown) отправлено или попытка сделана.")
            except Exception as e_notify_stop:
                logger.error(f"MAIN: Ошибка при отправке уведомления об остановке в trigger_shutdown: {e_notify_stop}", exc_info=True)
        else:
            logger.warning("MAIN: telegram_handler не доступен или не имеет send_notification_to_admin, уведомление об остановке не отправлено из trigger_shutdown.")

        bot_status = "stopping" # Статус меняем после попытки уведомления
        logger.info("MAIN: Установка события ЗАВЕРШЕНИЯ РАБОТЫ (shutdown_event)...")
        shutdown_event.set()
        logger.info("MAIN: Очистка события паузы (pause_event)...")
        pause_event.clear() 
    else:
        logger.info("MAIN: Событие shutdown_event уже было установлено. Повторный вызов trigger_shutdown проигнорирован.")


def signal_handler(sig, frame):
    sig_name = getattr(signal, f"SIG{signal.Signals(sig).name}", str(sig))
    logger.info(f"MAIN: Получен сигнал ОС {sig_name} ({sig}). Запускаем процедуру мягкого завершения...")
    # ...
    try:
        loop = asyncio.get_running_loop()
        loop.call_soon_threadsafe(lambda: asyncio.create_task(trigger_shutdown()))
    except RuntimeError:
        logger.warning("MAIN: Нет активного цикла событий asyncio в текущем потоке для signal_handler. Попытка get_event_loop().")
        asyncio.get_event_loop().call_soon_threadsafe(lambda: asyncio.create_task(trigger_shutdown()))



# --- Основная логика парсинга и обработки ---

async def run_parsing_cycle():
    global ATH_BOT_MODE_RUNTIME, pending_ath_checks
    current_time_for_log = datetime.now().strftime('%H:%M:%S')
    
    if shutdown_event.is_set(): # Сначала проверяем полную остановку
        logger.info(f"RUN_CYCLE ({current_time_for_log}): Завершение работы: Пропуск цикла парсинга.")
        return

    if pause_event.is_set(): # Затем проверяем паузу
        logger.info(f"RUN_CYCLE ({current_time_for_log}): Бот на паузе. Пропуск цикла парсинга.")
        # Уведомление админу лучше делать из trigger_pause или один раз при постановке на паузу, чтобы не спамить
        return
    
    logger.info(f"RUN_CYCLE ({current_time_for_log}): Очистка pending_ath_checks и ath_results_queue перед новым циклом парсинга.")
    pending_ath_checks.clear() # Полностью очищаем словарь ожиданий
    if telegram_handler: 
        await _clear_async_queue(telegram_handler.ath_results_queue, "ATH results before new cycle")
    
    logger.info(f"RUN_CYCLE ({current_time_for_log}): {'='*10} Запуск цикла парсинга Axiom {'='*10}")
    # ... (остальная часть run_parsing_cycle без изменений)
    # ... (до конца функции)
    if not browser_parser:
        logger.error("RUN_CYCLE: Экземпляр BrowserParser не инициализирован.")
        return
    if not telegram_handler:
         logger.error("RUN_CYCLE: Экземпляр TelegramHandler не инициализирован.")
         return

    # main.py, внутри run_parsing_cycle

    # ... (после проверок browser_parser и telegram_handler)

    parsing_result_tuple = await browser_parser.parse_discover_page()

    if parsing_result_tuple is None:
        logger.error("RUN_CYCLE: Критическая ошибка при вызове parse_discover_page (вернул None). Цикл прерван.")
        if telegram_handler:
            await telegram_handler.send_notification_to_admin("🆘 <b>КРИТИЧЕСКАЯ ОШИБКА:</b> Не удалось выполнить парсинг страницы Axiom (внутренняя ошибка парсера).")
        return

    parsed_coins, rows_found_on_page = parsing_result_tuple

    # Формирование отчета для администратора
    if telegram_handler:
        report_message_parts = []
        report_header = f"<b>🔎 Отчет о парсинге Axiom ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')})</b>\n"
        report_message_parts.append(report_header)

        # Игнорируем специальный отчет о расхождении, если изначально не найдено строк
        if rows_found_on_page == 0:
            message = "На странице Axiom не найдено строк с монетами для обработки."
            report_message_parts.append(message)
            logger.info(f"RUN_CYCLE: {message}")
        else:
            successfully_parsed_count = len(parsed_coins)
            report_message_parts.append(f"Найдено строк для парсинга: {rows_found_on_page}")
            report_message_parts.append(f"Успешно спарсено монет: {successfully_parsed_count}")

            if successfully_parsed_count != rows_found_on_page:
                mismatch_text = "⚠️ <b>#MismatchInParsing Внимание!</b> Обнаружено расхождение в количестве найденных и спарсенных монет. HTML страницы был сохранен для анализа."
                report_message_parts.append(mismatch_text)
                logger.warning(f"RUN_CYCLE: {mismatch_text} Найдено строк: {rows_found_on_page}, Спарсено: {successfully_parsed_count}")

            report_message_parts.append("\n<b>Первые монеты из спарсенного списка (если есть):</b>")

            if not parsed_coins:
                report_message_parts.append("<i>Нет успешно спарсенных монет для отображения.</i>")
            else:
                coins_to_report = parsed_coins[:15]
                for idx, coin in enumerate(coins_to_report):
                    tic = coin.get('tic', 'N/A')
                    age = coin.get('age_str', '?')
                    mc_val = coin.get('mc', 0)
                    l_val = coin.get('l', 0)
                    v_val = coin.get('v', 0)
                    ca_val = coin.get('ca', 'NO_CA')
                    ca_short = ca_val[:6] + "..." if ca_val and ca_val != 'NO_CA' else "NO_CA"
                    report_message_parts.append(
                        f"<b>{idx+1}. {tic}</b> (<code>{ca_short}</code>)\n"
                        f"  Возраст: {age}, MC: ${mc_val:,}, Liq: ${l_val:,}, Vol: ${v_val:,}"
                    )
                if len(parsed_coins) > len(coins_to_report):
                    report_message_parts.append(f"\n<i>... и еще {len(parsed_coins) - len(coins_to_report)} монет.</i>")

        final_report_message = "\n".join(report_message_parts)
        await telegram_handler.send_notification_to_admin(final_report_message)

    if not parsed_coins: # Если в итоге нет монет для дальнейшей обработки
        logger.info("RUN_CYCLE: Нет успешно спарсенных монет для дальнейшей обработки в этом цикле.")
        logger.info(f"RUN_CYCLE ({current_time_for_log}): {'='*10} Конец цикла парсинга Axiom {'='*10}")
        return

    logger.info(f"RUN_CYCLE: Успешно спарсено {len(parsed_coins)} монет. Фильтрация и отправка на проверку ATH...")
    # ... (остальной код функции run_parsing_cycle)

    processed_count = 0
    for coin_data in parsed_coins:
        if shutdown_event.is_set():
            logger.info("RUN_CYCLE: Завершение работы: Прерывание обработки монет.")
            break
        if pause_event.is_set():
            logger.info("RUN_CYCLE: Пауза активирована во время перебора спарсенных монет. Прерывание отправки дальнейших ATH запросов из этого цикла.")
            break # Выходим из цикла for, если бот на паузе

        ca = coin_data.get('ca')
        age_str = coin_data.get('age_str')

        if not ca or not utils.is_valid_solana_address(ca):
            logger.warning(f"RUN_CYCLE: Отсутствует или невалидный CA в данных монеты: {coin_data}. Пропуск.")
            continue
        if not age_str:
            logger.warning(f"RUN_CYCLE: Отсутствует Age в данных монеты для CA {ca}: {coin_data}. Пропуск.")
            continue

        age_seconds = utils.time_ago_to_seconds(age_str)
        if age_seconds is not None:
            min_age_seconds_threshold = config.MIN_COIN_AGE_MINUTES_THRESHOLD * 60
            if age_seconds <= min_age_seconds_threshold:
                 logger.debug(f"RUN_CYCLE: Монета {coin_data.get('tic', ca)} ({ca}) слишком молодая ({age_str} ~{age_seconds}s). "
                              f"Порог: >{config.MIN_COIN_AGE_MINUTES_THRESHOLD} мин (~{min_age_seconds_threshold}s). Пропуск.")
                 continue
            logger.debug(f"RUN_CYCLE: Монета {coin_data.get('tic', ca)} ({ca}) прошла фильтр по возрасту ({age_str}).")
        else:
            logger.warning(f"RUN_CYCLE: Не удалось определить возраст для монеты {coin_data.get('tic', ca)} ({ca}): '{age_str}'. Пропуск.")
            continue

        if ca not in pending_ath_checks: # Это условие остается
            pending_ath_checks[ca] = {
                'data': coin_data, 
                'timestamp_requested': datetime.now(), # Время первого запроса в этом цикле
                'ath1_status': 'not_sent', # Статусы: not_sent, pending, received_ok, received_na_error, timeout
                'ath1_data': None,         # Данные: {'seconds': X} или None
                'ath2_status': 'not_sent',
                'ath2_data': None,
                'decision_made': False     # Флаг, что решение по CA принято
            }
            logger.debug(f"RUN_CYCLE: Монета {tic} ({ca}) добавлена в pending_ath_checks со статусом 'not_sent'.")
        else: # Этого не должно произойти, если мы чистим pending_ath_checks в начале цикла
            logger.warning(f"RUN_CYCLE: Монета {tic} ({ca}) уже в pending_ath_checks. Это неожиданно при очистке в начале цикла.")
            # Можно обновить timestamp_requested, если мы хотим "перезапросить" в этом цикле
            # pending_ath_checks[ca]['timestamp_requested'] = datetime.now()
            # pending_ath_checks[ca]['decision_made'] = False # Сбрасываем флаг решения
            # pending_ath_checks[ca]['ath1_status'] = 'not_sent' # И статусы
            # pending_ath_checks[ca]['ath2_status'] = 'not_sent'
            # pending_ath_checks[ca]['ath1_data'] = None
            # pending_ath_checks[ca]['ath2_data'] = None


        logger.info(f"RUN_CYCLE: Отправка CA {ca} ({coin_data.get('tic')}) на проверку ATH (Режим: {ATH_BOT_MODE_RUNTIME})...")
        sent_to_at_least_one_bot_in_this_iteration = False # Переименовал для ясности


        if ATH_BOT_MODE_RUNTIME in ["ATH1", "BOTH"]:
            if config.BOT_ATH1_USERNAME:
                logger.debug(f"RUN_CYCLE: Отправка CA {ca} боту ATH1: {config.BOT_ATH1_USERNAME}")
                sent1 = await telegram_handler.send_message_to_user_bot(config.BOT_ATH1_USERNAME, ca)
                if sent1: 
                    pending_ath_checks[ca]['ath1_status'] = 'pending' # Обновляем статус
                    sent_to_at_least_one_bot_in_this_iteration = True
                await asyncio.sleep(1.1)
            else:
                if ATH_BOT_MODE_RUNTIME == "ATH1": logger.warning("RUN_CYCLE: ATH_BOT_MODE='ATH1', но BOT_ATH1_USERNAME не задан в config.")
                elif ATH_BOT_MODE_RUNTIME == "BOTH": logger.warning("RUN_CYCLE: ATH_BOT_MODE='BOTH', но BOT_ATH1_USERNAME не задан, пропуск отправки ему.")

        if ATH_BOT_MODE_RUNTIME in ["ATH2", "BOTH"]:
            if config.BOT_ATH2_USERNAME:
                logger.debug(f"RUN_CYCLE: Отправка CA {ca} боту ATH2: {config.BOT_ATH2_USERNAME}")
                sent2 = await telegram_handler.send_message_to_user_bot(config.BOT_ATH2_USERNAME, ca)
                if sent2:
                    pending_ath_checks[ca]['ath2_status'] = 'pending' # Обновляем статус
                    sent_to_at_least_one_bot_in_this_iteration = True
                await asyncio.sleep(1.1)
            else:
                if ATH_BOT_MODE_RUNTIME == "ATH2": logger.warning("RUN_CYCLE: ATH_BOT_MODE='ATH2', но BOT_ATH2_USERNAME не задан в config.")
                elif ATH_BOT_MODE_RUNTIME == "BOTH": logger.warning("RUN_CYCLE: ATH_BOT_MODE='BOTH', но BOT_ATH2_USERNAME не задан, пропуск отправки ему.")

        # Проверяем, были ли сконфигурированы боты для выбранного режима
        active_bots_configured = False
        if ATH_BOT_MODE_RUNTIME == "ATH1" and config.BOT_ATH1_USERNAME: active_bots_configured = True
        if ATH_BOT_MODE_RUNTIME == "ATH2" and config.BOT_ATH2_USERNAME: active_bots_configured = True
        if ATH_BOT_MODE_RUNTIME == "BOTH" and (config.BOT_ATH1_USERNAME or config.BOT_ATH2_USERNAME): active_bots_configured = True
        
        if active_bots_configured and not sent_to_at_least_one_bot_in_this_iteration:
            # Если не удалось отправить ни одному, то эта запись в pending_ath_checks бесполезна
            pending_ath_checks.pop(ca, None) 
            logger.error(f"RUN_CYCLE: Не удалось отправить CA {ca} ни одному ATH боту. Удален из pending_ath_checks.")
        elif sent_to_at_least_one_bot_in_this_iteration:
            processed_count += 1
        elif not active_bots_configured:
            pending_ath_checks.pop(ca, None)
            logger.warning(f"RUN_CYCLE: CA {ca} не был отправлен ATH ботам (не сконфигурированы для режима {ATH_BOT_MODE_RUNTIME}). Удален из pending_ath_checks.")

    logger.info(f"RUN_CYCLE: Отправлено {processed_count} CA на проверку ATH.")
    logger.info(f"RUN_CYCLE ({current_time_for_log}): {'='*10} Конец цикла парсинга Axiom {'='*10}")

async def process_ath_results():
    global ATH_BOT_MODE_RUNTIME
    logger_name = "ATH_PROCESSOR" # Для удобства логирования
    if not telegram_handler or not database:
        logger.error(f"{logger_name}: TelegramHandler или Database не инициализированы.")
        return

    logger.info(f"{logger_name}: Запуск обработчика результатов ATH...")
    while not shutdown_event.is_set():
        try:
            if pause_event.is_set(): # True, если бот должен быть на паузе
                logger.info(f"{logger_name}: Бот на паузе (pause_event установлен). Ожидание команды /play для снятия паузы...")
                while pause_event.is_set(): # Цикл, пока событие паузы установлено
                    if shutdown_event.is_set():
                        logger.info(f"{logger_name}: Обнаружен сигнал завершения во время ожидания снятия паузы.")
                        break # Выходим из цикла while pause_event.is_set()
                    await asyncio.sleep(0.5) # Небольшая задержка, чтобы не блокировать цикл и проверять shutdown_event

                if shutdown_event.is_set(): # Если вышли из-за shutdown_event
                    logger.info(f"{logger_name}: Выход из обработчика ATH из-за сигнала завершения после паузы.")
                    break # Выходим из основного цикла while not shutdown_event.is_set() всего обработчика

                # Если мы здесь, значит pause_event.is_set() стал False (пауза снята командой /play)
                if not pause_event.is_set():
                    logger.info(f"{logger_name}: Пауза снята (pause_event очищен). Возобновление обработки очереди ATH.")
                else:
                    # Этого не должно произойти, если вышли из while не по shutdown_event
                    logger.warning(f"{logger_name}: Вышли из ожидания паузы, но pause_event все еще установлен и shutdown_event не установлен. Пропускаем итерацию.")
                    telegram_handler.ath_results_queue.task_done() # Отмечаем, что не будем обрабатывать элемент, если он был взят до этой логики (маловероятно здесь)
                    continue # Пропускаем текущую итерацию основного цикла while, чтобы снова проверить состояние
                        
            # Если не на паузе или пауза только что была снята, пытаемся получить результат
            result = await asyncio.wait_for(telegram_handler.ath_results_queue.get(), timeout=1.0)
            
            # ... (остальная часть process_ath_results без изменений)
            # ... (до конца функции)
            logger.info(f"ATH_PROCESSOR: Получен результат из очереди: {result}")

            ca_from_result = result.get('ca')
            error = result.get('error')
            ath_seconds = result.get('ath_seconds')
            source_bot = result.get('source_bot', 'N/A')

            if not ca_from_result or ath_seconds is None: # Базовая проверка, хотя не должна срабатывать
                logger.error(f"ATH_PROCESSOR: Получен НЕКОРРЕКТНЫЙ результат из очереди (ожидался CA и ath_seconds): {result}. Пропуск.")
                telegram_handler.ath_results_queue.task_done()
                continue

            # Получаем оригинальные данные монеты
            # pending_ath_checks может быть уже очищен, если это очень старый результат,
            # но evaluate_ath_decision должен был поставить 'decision_made = True'
            check_info = pending_ath_checks.get(ca_from_result) 
            if not check_info: # Если вдруг нет (хотя не должно быть, если decision_made правильно ставится)
                logger.warning(f"ATH_PROCESSOR: Данные для CA '{ca_from_result}' не найдены в pending_ath_checks при финальной обработке. Возможно, результат устарел. Пропуск.")
                telegram_handler.ath_results_queue.task_done()
                continue
            # Если decision_made, но элемент как-то снова попал в очередь - игнорируем
            if check_info.get('decision_made_and_queued'): # Добавим новый флаг? Или просто удалять из pending после постановки в очередь?
                                                          # Лучше, если evaluate_ath_decision удаляет из pending или ставит флаг.
                                                          # Сейчас pending_ath_checks очищается в начале run_parsing_cycle.
                                                          # Если решение принято, то 'decision_made' = True.
                pass # Просто обрабатываем

            original_coin_data = check_info.get('data')
            if not original_coin_data:
                 logger.warning(f"ATH_PROCESSOR: 'data' отсутствует в check_info для CA '{ca_from_result}'. Пропуск.")
                 telegram_handler.ath_results_queue.task_done()
                 continue

            tic = original_coin_data.get('tic', ca_from_result[:6])
            logger.info(f"ATH_PROCESSOR: Успешно получен ATH ({ath_seconds}s) для CA '{ca_from_result}' ({tic}) от {source_bot}. Проверка фильтров...")


            telegram_handler.ath_results_queue.task_done()

            logger.debug(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}). Фильтр 1: Возраст ATH. ATH={ath_seconds}s, Порог={config.THRESHOLD_ATH_SECONDS}s.")
            if ath_seconds > config.THRESHOLD_ATH_SECONDS:
                logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) НЕ ПРОШЕЛ фильтр ATH ({ath_seconds}s > {config.THRESHOLD_ATH_SECONDS}s).")
                if ca_from_result in pending_ath_checks: pending_ath_checks.pop(ca_from_result, None)
                logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) удален из ожидания (не прошел фильтр ATH).")
                telegram_handler.ath_results_queue.task_done()
                continue
            logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) ПРОШЕЛ фильтр по возрасту ATH.")

            loop = asyncio.get_running_loop()
            logger.debug(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}). Фильтр 2: Статус в БД...")
            current_status = await loop.run_in_executor(None, database.get_coin_status, ca_from_result)
            logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}). Текущий статус в БД: '{current_status}'.")

            if current_status in ["Processing", "Buy Success!"]:
                logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) НЕ ПРОШЕЛ фильтр статуса БД (статус: '{current_status}').")
                if ca_from_result in pending_ath_checks: pending_ath_checks.pop(ca_from_result, None)
                logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) удален из ожидания (статус БД).")
                telegram_handler.ath_results_queue.task_done()
                continue
            logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) ПРОШЕЛ фильтр статуса БД.")

            logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) ПРОШЛА ВСЕ ФИЛЬТРЫ. Отправка на покупку...")
            volume = original_coin_data.get('v', 0)
            target_bot_username = config.BOT_BUYSELL1_USERNAME if volume < config.THRESHOLD_VOLUME else config.BOT_BUYSELL2_USERNAME
            volume_text = f"МЕНЬШЕ {config.THRESHOLD_VOLUME:,}" if volume < config.THRESHOLD_VOLUME else f"БОЛЬШЕ или РАВНО {config.THRESHOLD_VOLUME:,}"
            group_emoji = "🚀" if volume < config.THRESHOLD_VOLUME else "💥"

            logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}). Объем V=${volume:,}. Выбран бот: {target_bot_username}.")

            if not target_bot_username: # Проверка, что target_bot_username определен
                logger.error(f"ATH_PROCESSOR: Не удалось определить target_bot_username для CA '{ca_from_result}' ({tic}). Проверьте THRESHOLD_VOLUME и имена BUYSELL ботов в config.")
                if ca_from_result in pending_ath_checks: pending_ath_checks.pop(ca_from_result, None)
                telegram_handler.ath_results_queue.task_done()
                continue

            sent_to_buy_bot = await telegram_handler.send_message_to_user_bot(target_bot_username, ca_from_result)
            if not sent_to_buy_bot:
                logger.error(f"ATH_PROCESSOR: НЕ УДАЛОСЬ отправить CA '{ca_from_result}' ({tic}) боту {target_bot_username}.")
                telegram_handler.ath_results_queue.task_done()
                continue
            logger.info(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) успешно отправлен боту {target_bot_username}.")

            current_time_str = datetime.now().strftime("%H:%M:%S %d-%m-%Y")
            # Данные для сообщения в группу
            safe_tic = html.escape(tic)
            safe_ca_code = f"<code>{html.escape(ca_from_result)}</code>"
            safe_age_str = html.escape(original_coin_data.get('age_str', 'N/A'))
            
            
            liq_value_group = original_coin_data.get('l', 0) # Ликвидность
            axiom_link = f"https://axiom.trade/t/{ca_from_result}"
            dex_link = f"https://dexscreener.com/solana/{ca_from_result}"
            safe_dex_link = html.escape(dex_link)
            safe_axiom_link = html.escape(axiom_link)

            admin_msg = (f"<b>✅ Подходящая монета!</b>\n"
                         f"({current_time_str})\n\n"
                         f"<b>Тикер:</b> {tic}\n<b>CA:</b> <code>{ca_from_result}</code>\n"
                         f"<b>Возраст:</b> {original_coin_data.get('age_str', 'N/A')}\n"
                         f"<b>MC:</b> ${original_coin_data.get('mc', 0):,}, <b>Liq:</b> ${liq_value_group:,}\n"
                         f"<b>1 min Vol:</b> ${volume:,}\n"
                         f"<b>ATH Age:</b> {ath_seconds}s ago\n\n"
                         f"<b>Условие:</b> Объем {volume_text}$\n<b>Отправлено в:</b> {target_bot_username}\n"
                         f"<b>Axiom:</b> <a href='{axiom_link}'>Ссылка на токен</a>")
            await telegram_handler.send_notification_to_admin(admin_msg)

            group_msg = (f"{group_emoji} <b>New Signal 🆕</b>\n" 
                         f"🕒 ({html.escape(current_time_str)})\n\n"
                         f"<b>💊 {safe_tic}</b>\n"
                         f"<b>🧭 CA:</b> {safe_ca_code}\n\n"
                         f"<b>📅 Created:</b> {safe_age_str} ago\n"
                         f"<b>💰 MC:</b> ${original_coin_data.get('mc', 0):,}\n"
                         f"<b>💧 LQ:</b> ${liq_value_group:,}\n"
                         f"<b>📊 1m Vol:</b> ${volume:,}\n"
                         f"<b>⏳ ATH age:</b> {ath_seconds}s ago\n\n"
                         f"<b>💹 Charts:</b>| <a href='{safe_axiom_link}'>AXIOM</a> | <a href='{safe_dex_link}'>DEXS</a>")
            group_message_id = await telegram_handler.send_notification_to_group(group_msg)
            
            db_data = {**original_coin_data, 'ca': ca_from_result, 'ath_seconds': ath_seconds, 'status': "Processing", 'group_message_id': group_message_id}
            await loop.run_in_executor(None, database.upsert_coin, db_data)
            logger.info(f"ATH_PROCESSOR: Данные для CA '{ca_from_result}' ({tic}) записаны в БД со статусом 'Processing'.")

            if ca_from_result in pending_ath_checks: pending_ath_checks.pop(ca_from_result, None)
            logger.debug(f"ATH_PROCESSOR: CA '{ca_from_result}' ({tic}) удален из pending_ath_checks.")
            telegram_handler.ath_results_queue.task_done()

        except asyncio.TimeoutError:
            pass # Это нормально, если очередь пуста
        except Exception as e:
            logger.error(f"{logger_name}: Непредвиденная ошибка в цикле: {e}", exc_info=True)
            if 'result' in locals() and telegram_handler and hasattr(telegram_handler, 'ath_results_queue'):
                try:
                    telegram_handler.ath_results_queue.task_done()
                except ValueError: # Может возникнуть, если task_done() вызывается для элемента, который не был взят
                    logger.warning(f"{logger_name}: Ошибка при task_done в блоке исключения (возможно, элемент уже был отмечен).")
            await asyncio.sleep(1) # Небольшая задержка перед следующей попыткой в случае ошибки


async def process_buy_sell_results():
    logger_name = "BUYSELL_PROCESSOR" # Для удобства логирования
    if not telegram_handler or not database:
        logger.error(f"{logger_name}: TelegramHandler или Database не инициализированы.")
        return
    logger.info(f"{logger_name}: Запуск обработчика результатов Buy/Sell...")
    while not shutdown_event.is_set():
        try:
            if pause_event.is_set(): # True, если бот должен быть на паузе
                logger.info(f"{logger_name}: Бот на паузе (pause_event установлен). Ожидание команды /play для снятия паузы...")
                while pause_event.is_set(): # Цикл, пока событие паузы установлено
                    if shutdown_event.is_set():
                        logger.info(f"{logger_name}: Обнаружен сигнал завершения во время ожидания снятия паузы.")
                        break # Выходим из цикла while pause_event.is_set()
                    await asyncio.sleep(0.5) # Небольшая задержка, чтобы не блокировать цикл и проверять shutdown_event

                if shutdown_event.is_set(): # Если вышли из-за shutdown_event
                    logger.info(f"{logger_name}: Выход из обработчика Buy/Sell из-за сигнала завершения после паузы.")
                    break # Выходим из основного цикла while not shutdown_event.is_set() всего обработчика

                # Если мы здесь, значит pause_event.is_set() стал False (пауза снята командой /play)
                if not pause_event.is_set():
                    logger.info(f"{logger_name}: Пауза снята (pause_event очищен). Возобновление обработки очереди Buy/Sell.")
                else:
                    # Этого не должно произойти, если вышли из while не по shutdown_event
                    logger.warning(f"{logger_name}: Вышли из ожидания паузы, но pause_event все еще установлен и shutdown_event не установлен. Пропускаем итерацию.")
                    telegram_handler.buy_sell_results_queue.task_done() # Отмечаем, что не будем обрабатывать элемент, если он был взят до этой логики (маловероятно здесь)
                    continue # Пропускаем текущую итерацию основного цикла while, чтобы снова проверить состояние
            
            # Если не на паузе или пауза только что была снята, пытаемся получить результат
            result = await asyncio.wait_for(telegram_handler.buy_sell_results_queue.get(), timeout=1.0)
            logger.info(f"{logger_name}: Получен результат: {result}")
            
            ca = result.get('ca')
            tic = result.get('tic')
            result_type = result.get('type')
            percentage = result.get('per')
            error_type = result.get('error_type')
            source_bot = result.get('source_bot', 'N/A')

            if error_type == 'balance':
                logger.critical(f"{logger_name}: Ошибка баланса от {source_bot} (CA: {ca}). Инициирую АВТО-ПАУЗУ.")
                # Уведомление админу и сама пауза теперь будут в trigger_pause
                await trigger_pause(auto_triggered=True, reason=f"Insufficient balance от {source_bot} для CA: {ca or 'Не определен'}\n<b>{tic}</b>\n")
                telegram_handler.buy_sell_results_queue.task_done()
                continue # После постановки на паузу, выходим из текущей итерации

            if not ca:
                logger.warning(f"{logger_name}: Получен результат без CA: {result}. Пропуск.")
                telegram_handler.buy_sell_results_queue.task_done()
                continue
            
            loop = asyncio.get_running_loop()
            coin_details = await loop.run_in_executor(None, database.get_coin_details, ca)
            
            if not coin_details:
                logger.warning(f"{logger_name}: Результат для CA '{ca}', но он не найден в БД. Result: {result}")
                telegram_handler.buy_sell_results_queue.task_done()
                continue
                
            tic = coin_details.get('tic', ca[:6])
            group_message_id = coin_details.get('group_message_id')

            if result_type == 'buy':
                logger.info(f"{logger_name}: 'Buy Success!' для {ca} ({tic}) от {source_bot}.")
                await loop.run_in_executor(None, database.update_coin_status, ca, "Buy Success!")
                await telegram_handler.send_notification_to_admin(f"✅ Успешно купили: {tic} (<code>{ca}</code>) через {source_bot}")

            elif result_type == 'sell':
                logger.info(f"{logger_name}: Продажа для {ca} ({tic}) от {source_bot} с результатом {percentage}.")
                await loop.run_in_executor(None, database.update_coin_status, ca, "Sell Success!")
                
                import html 
                admin_msg = f"💰 Продали: {tic} (<code>{ca}</code>) с результатом {percentage} через {source_bot}"
                group_msg = f"💰 {html.escape(tic)} | Результат: {html.escape(str(percentage))}"
                
                await telegram_handler.send_notification_to_admin(admin_msg)
                if group_message_id:
                    await telegram_handler.send_notification_to_group(group_msg, reply_to_id=group_message_id)
                else:
                    logger.warning(f"{logger_name}: Не найден group_message_id для CA {ca}, отправка в группу без реплая.")
                    await telegram_handler.send_notification_to_group(group_msg)
            
            elif error_type == 'failed':
                logger.warning(f"{logger_name}: 'Transaction Failed' для CA {ca} ({tic}) от {source_bot}.")
                await loop.run_in_executor(None, database.update_coin_status, ca, "Buy Failed")
                await telegram_handler.send_notification_to_admin(
                    f"⚠️ Транзакция покупки для {tic} (<code>{ca}</code>) не удалась (failed) через {source_bot}. Статус обновлен на Buy Failed."
                )
                # Активируем авто-паузу ПОСЛЕ обновления БД и уведомления
                await trigger_pause(auto_triggered=True, reason=f"Transaction Failed! для CA: {ca} ({tic}) от {source_bot}")
            
            telegram_handler.buy_sell_results_queue.task_done()

        except asyncio.TimeoutError:
            pass # Это нормально, если очередь пуста
        except Exception as e:
            logger.error(f"{logger_name}: Ошибка в цикле: {e}", exc_info=True)
            if 'result' in locals() and telegram_handler and hasattr(telegram_handler, 'buy_sell_results_queue'):
                try:
                    telegram_handler.buy_sell_results_queue.task_done()
                except ValueError:
                     logger.warning(f"{logger_name}: Ошибка при task_done в блоке исключения.")
            await asyncio.sleep(1)
            pass


async def ath_timeout_monitor_task():
    """Периодически проверяет pending_ath_checks на таймауты и вызывает оценку."""
    global pending_ath_checks # Убедись, что доступ к глобальной переменной корректен
    while not shutdown_event.is_set():
        # Частота проверки - можно сделать настраиваемой, например, каждую секунду
        await asyncio.sleep(1) 

        if pause_event.is_set(): # Не работаем во время общей паузы бота
            continue

        if not telegram_handler or not pending_ath_checks:
            continue

        #logger.debug(f"ATH_TIMEOUT_MONITOR: Проверка таймаутов (в pending: {len(pending_ath_checks)})...") # Можно раскомментировать для отладки

        # Создаем копию ключей для итерации, так как evaluate_ath_decision может изменить словарь (хотя сейчас он только ставит флаг)
        current_cas_to_check = list(pending_ath_checks.keys())

        for ca in current_cas_to_check:
            check_info = pending_ath_checks.get(ca) # Получаем свежую информацию, т.к. она могла измениться
            if check_info and not check_info.get('decision_made'):
                # Вызываем evaluate_ath_decision, который внутри себя проверит таймауты
                # и примет решение, если таймаут истек для всех ожидаемых ответов
                await telegram_handler.evaluate_ath_decision(ca, from_monitor=True) 
                # from_monitor=True - это флаг, чтобы evaluate_ath_decision знала, что ее вызвал монитор, 
                # а не ответ от бота (может быть полезно для логики таймаута)
            elif not check_info:
                logger.debug(f"ATH_TIMEOUT_MONITOR: CA {ca} уже отсутствует в pending_ath_checks (возможно, обработан).")


async def main():
    global logger, browser_parser, telegram_handler, scheduler, pending_ath_checks, ATH_BOT_MODE_RUNTIME, bot_status

    logger = config.setup_logging()
    bot_status = "running" # Устанавливаем начальный статус после инициализации логгера
    logger.info("="*20 + " ЗАПУСК AXIOM VOLUME BOT " + "="*20)
    
    loop = asyncio.get_running_loop()
    
    # --- Запрос режима работы ATH ботов ---
    # ... (код запроса ATH_BOT_MODE_RUNTIME остается без изменений) ...
    while True:
        prompt = ("Выберите режим работы ATH ботов:\n"
                  "1 - Только BOT_ATH1 (@DevsNightmareProbot)\n"
                  "2 - Только BOT_ATH2 (@RickBurpBot)\n"
                  "3 - Оба бота (BOT_ATH1 и BOT_ATH2) (по умолчанию)\n"
                  "Введите номер (1, 2 или 3): ")
        try:
            choice = await loop.run_in_executor(None, input, prompt)
            choice = choice.strip()
            if choice == "1":
                ATH_BOT_MODE_RUNTIME = "ATH1"
                if not config.BOT_ATH1_USERNAME:
                    logger.error("MAIN: Выбран режим ATH1, но BOT_ATH1_USERNAME не задан в .env! Пожалуйста, исправьте и перезапустите.")
                    return
                break
            elif choice == "2":
                ATH_BOT_MODE_RUNTIME = "ATH2"
                if not config.BOT_ATH2_USERNAME:
                    logger.error("MAIN: Выбран режим ATH2, но BOT_ATH2_USERNAME не задан в .env! Пожалуйста, исправьте и перезапустите.")
                    return
                break
            elif choice == "3" or not choice: # Пустой ввод - по умолчанию "Оба"
                ATH_BOT_MODE_RUNTIME = "BOTH"
                if not config.BOT_ATH1_USERNAME or not config.BOT_ATH2_USERNAME:
                    logger.error("MAIN: Выбран режим BOTH, но один или оба ATH бота (BOT_ATH1_USERNAME, BOT_ATH2_USERNAME) не заданы в .env! Пожалуйста, исправьте и перезапустите.")
                    # Можно добавить более конкретное сообщение, какой именно бот отсутствует
                    return
                break
            else:
                print("Некорректный выбор. Пожалуйста, введите 1, 2 или 3.")
        except EOFError: # Если input прерван (например, в Docker без tty)
            logger.warning("MAIN: Ввод для выбора режима ATH ботов прерван/недоступен. Используется режим по умолчанию 'BOTH'.")
            ATH_BOT_MODE_RUNTIME = "BOTH" # Устанавливаем значение по умолчанию
            if not config.BOT_ATH1_USERNAME or not config.BOT_ATH2_USERNAME:
                 logger.error("MAIN: Режим BOTH по умолчанию, но один или оба ATH бота не заданы в .env!")
                 return
            break # Выходим из цикла с режимом по умолчанию
    logger.info(f"MAIN: Режим работы ATH ботов установлен: {ATH_BOT_MODE_RUNTIME}")
    # --- Конец пункта 2 ---

    database.create_tables()
    
    clear_db_input = await loop.run_in_executor(None, input, "Очистить базу данных монет перед запуском? (Да/Нет): ")
    if clear_db_input.strip().lower() == 'да':
        database.clear_all_coin_data()
        logger.info("MAIN: База данных очищена.")

    browser_parser = BrowserParser()
    # --- Инициализация TelegramHandler с новыми колбэками ---
    telegram_handler = TelegramHandler(
        loop=loop,
        stop_callback=trigger_shutdown,  # Существующий колбэк
        pause_callback=trigger_pause,    # Новый колбэк для /pause
        play_callback=trigger_play,
        pause_event_main=pause_event
    )
    telegram_handler.pending_ath_checks_ref = pending_ath_checks

    scheduler = AsyncIOScheduler(event_loop=loop) # Динамическое определение таймзоны

    # Установка обработчиков сигналов
    for sig_name_str in ('SIGINT', 'SIGTERM'):
        sig_name_enum = getattr(signal, sig_name_str, None)
        if sig_name_enum:
            try:
                signal.signal(sig_name_enum, signal_handler)
                logger.debug(f"MAIN: Обработчик для сигнала {sig_name_str} установлен.")
            except (ValueError, OSError, RuntimeError) as e_signal:
                logger.warning(f"MAIN: Не удалось установить обработчик для сигнала {sig_name_str}: {e_signal}.")
    
    # Запуск браузера и навигация
    if not await browser_parser.launch_browser():
         logger.critical("MAIN: Не удалось запустить браузер. Завершение работы.")
         return
    if not await browser_parser.navigate_to_axiom():
         logger.critical("MAIN: Не удалось перейти на страницу Axiom. Завершение работы.")
         await browser_parser.close_browser()
         return

    print("\n" + "="*30)
    print("ПОЖАЛУЙСТА, ВОЙДИТЕ В АККАУНТ AXIOM В ОТКРЫВШЕМСЯ ОКНЕ БРАУЗЕРА.")
    print("Настройте нужные фильтры (если требуется).")
    await loop.run_in_executor(None, input, "После входа и настройки фильтров, нажмите Enter здесь для продолжения...")
    print("="*30 + "\n")

    if not await browser_parser.wait_for_login_confirmation():
        logger.critical("MAIN: Вход в Axiom не подтвержден. Завершение работы.")
        # Уведомление админу можно отправить только если telegram_handler уже подключен
        # await telegram_handler.send_notification_to_admin("⚠️ Вход в Axiom не подтвержден. Бот остановлен.")
        await browser_parser.close_browser()
        return
    logger.info("MAIN: Вход в Axiom подтвержден.")

    if not await telegram_handler.connect_clients(): # connect_clients теперь не принимает колбэки напрямую
        logger.critical("MAIN: Не удалось подключить Telegram клиенты. Завершение работы.")
        if browser_parser: await browser_parser.close_browser()
        return
    
    

    # !!! НОВЫЙ БЛОК ДЛЯ ЗАПУСКА PTB POLLING !!!
    ptb_polling_task = None
    if telegram_handler.ptb_app:
        logger.info("MAIN: Запуск PTB Application polling в фоновом режиме...")

        async def run_ptb_polling_wrapper(): # ИЗМЕНЕННАЯ ВЕРСИЯ
            app = telegram_handler.ptb_app
            try:
                if not app: # Дополнительная проверка
                    logger.error("MAIN: PTB_Polling_Wrapper: ptb_app is None, cannot start polling.")
                    return

                logger.info("MAIN: PTB: Вызов app.initialize() перед запуском updater polling...")
                await app.initialize() # Явно инициализируем приложение (создаст updater и dispatcher)
                logger.info("MAIN: PTB: Вызов app.start() (запуск Dispatcher, JobQueue и т.д.)...") # Новый лог
                await app.start() # <--- ДОБАВЛЕН ЭТОТ ВАЖНЫЙ ВЫЗОВ
                if not app.updater: # Проверка, что updater создан
                    logger.error("MAIN: PTB: app.updater is None after initialize(), cannot start polling.")
                    if not shutdown_event.is_set():
                        await trigger_shutdown()
                    return

                logger.info(f"MAIN: PTB: Вызов app.updater.start_polling() (Updater: {app.updater})...")
                await app.updater.start_polling(
                    allowed_updates=Update.ALL_TYPES,
                    timeout=30,
                    poll_interval=1.0,
                    drop_pending_updates=True # Рекомендуется для избежания обработки старых команд при перезапуске
                )
                logger.info("MAIN: PTB: app.updater.start_polling() запущен и работает в фоне.")

                # Теперь задача этой обертки - просто оставаться "живой", пока работает updater 
                # или пока не придет сигнал на завершение. Updater останавливается через app.updater.stop().
                while app.updater and app.updater.running and not shutdown_event.is_set():
                    await asyncio.sleep(0.5) # Проверяем состояние

                logger.info(f"MAIN: PTB: Цикл ожидания polling завершен (updater.running: {app.updater.running if app.updater else 'N/A'}, shutdown_event: {shutdown_event.is_set()}).")

            except asyncio.CancelledError:
                logger.info("MAIN: PTB polling task (run_ptb_polling_wrapper) был отменен.")
            except Exception as e:
                logger.error(f"MAIN: Ошибка в PTB polling task (run_ptb_polling_wrapper): {e}", exc_info=True)
                if not shutdown_event.is_set():
                    await trigger_shutdown() # Инициируем остановку, если polling упал сам
            finally:
                logger.info("MAIN: PTB polling task wrapper (run_ptb_polling_wrapper) входит в блок finally.")
                if app:
                    if app.updater and app.updater.running:
                        logger.info("MAIN: PTB (finally): updater все еще работает, попытка остановить.")
                        try:
                            await app.updater.stop()
                        except Exception as e_upd_stop:
                            logger.error(f"MAIN: PTB (finally): ошибка при остановке updater: {e_upd_stop}")
                    if hasattr(app, 'running') and app.running: # У Application есть свойство .running
                        logger.info("MAIN: PTB (finally): Application все еще работает, попытка остановить.")
                        try:
                            await app.stop()
                        except Exception as e_app_stop:
                            logger.error(f"MAIN: PTB (finally): ошибка при app.stop(): {e_app_stop}")
                # Остановка updater и приложения будет произведена в telegram_handler.disconnect_clients()
                # при штатном завершении. Если мы здесь из-за ошибки, disconnect_clients тоже будет вызван.

        ptb_polling_task = loop.create_task(run_ptb_polling_wrapper(), name="PTB_Polling")
    else:
        logger.error("MAIN: ptb_app не инициализирован в telegram_handler, PTB polling не будет запущен.")
    # !!! КОНЕЦ НОВОГО БЛОКА !!!

    # Далее идет существующий код:
    # await telegram_handler.send_notification_to_admin(...)
    # ath_processor_task = loop.create_task(...)
    # buy_sell_processor_task = loop.create_task(...)
    
    await telegram_handler.send_notification_to_admin(
        f"🚀 Бот Axiom Volume успешно запущен!\n"
        f"Режим работы ATH: <b>{ATH_BOT_MODE_RUNTIME}</b>\n"
        f"ATH1: {config.BOT_ATH1_USERNAME or 'Не задан'}\n"
        f"ATH2: {config.BOT_ATH2_USERNAME or 'Не задан'}\n"
        f"Пороговый возраст ATH: <b>{config.THRESHOLD_ATH_SECONDS}s</b>\n"
        f"Пороговый объем: <b>${config.THRESHOLD_VOLUME:,}</b>\n"
        f"Мин. возраст монеты: <b>{config.MIN_COIN_AGE_MINUTES_THRESHOLD} мин</b>"
    )

    ath_processor_task = loop.create_task(process_ath_results(), name="ATH_Processor")
    buy_sell_processor_task = loop.create_task(process_buy_sell_results(), name="BuySell_Processor")
    ath_timeout_monitor = loop.create_task(ath_timeout_monitor_task(), name="ATH_Timeout_Monitor")
      
    
    try:
        scheduler.add_job(run_parsing_cycle, 'cron', second=config.CHTIME, id='axiom_parsing_job', replace_existing=True, misfire_grace_time=30)
        scheduler.start()
        logger.info(f"MAIN: Планировщик запущен. Парсинг будет выполняться каждую минуту в ~{config.CHTIME} секунд.")
    except Exception as e:
         logger.critical(f"MAIN: Не удалось запустить планировщик: {e}", exc_info=True)
         await trigger_shutdown()
         # Завершаем задачи перед выходом
         for task in [ath_processor_task, buy_sell_processor_task]:
             if task and not task.done(): task.cancel()
         await asyncio.gather(ath_processor_task, buy_sell_processor_task, return_exceptions=True)
         if telegram_handler: await telegram_handler.disconnect_clients()
         if browser_parser: await browser_parser.close_browser()
         return

    logger.info("MAIN: Приложение работает. Ожидание сигнала завершения (Ctrl+C или команда /stop)...")
    await shutdown_event.wait()

    logger.info("MAIN: Начало процедуры корректного завершения работы...")
    bot_status = "stopping" # Устанавливаем финальный статус
    
    if scheduler and scheduler.running:
        try:
            scheduler.shutdown(wait=False)
            logger.info("MAIN: Планировщик остановлен.")
        except Exception as e_sched_shutdown:
            logger.error(f"MAIN: Ошибка при остановке планировщика: {e_sched_shutdown}")
        
    tasks_to_cancel = [ath_processor_task, buy_sell_processor_task]
    logger.info("MAIN: Отмена задач обработки очередей...")
    for task in tasks_to_cancel:
        if task and not task.done():
            task.cancel()
    try:
        await asyncio.gather(*[t for t in tasks_to_cancel if t], return_exceptions=True)
        logger.info("MAIN: Задачи обработки очередей завершены или были отменены.")
    except asyncio.CancelledError:
         logger.info("MAIN: Задачи обработки очередей были принудительно отменены при gather.")

    # ИЗМЕНИТЬ/ДОПОЛНИТЬ НА:
    tasks_to_await = []
    if ath_processor_task and not ath_processor_task.done():
        ath_processor_task.cancel()
        tasks_to_await.append(ath_processor_task)
    if buy_sell_processor_task and not buy_sell_processor_task.done():
        buy_sell_processor_task.cancel()
        tasks_to_await.append(buy_sell_processor_task)

    # Управление PTB polling task при завершении
    # Application.stop() (вызываемый в telegram_handler.disconnect_clients()) должен дать сигнал run_polling завершиться.
    # Но мы также можем попробовать отменить задачу, если она еще работает после вызова stop().
    if ptb_polling_task and not ptb_polling_task.done():
        logger.info("MAIN: PTB polling task все еще активен после сигнала shutdown, ожидаем его завершения или отменяем.")
        # Даем ему шанс завершиться самому после app.stop()
        try:
            await asyncio.wait_for(ptb_polling_task, timeout=5.0) 
        except asyncio.TimeoutError:
            logger.warning("MAIN: PTB polling task не завершился за 5 секунд, отменяем принудительно.")
            ptb_polling_task.cancel()
            tasks_to_await.append(ptb_polling_task) # Добавляем для gather, если отменили
        except asyncio.CancelledError:
            logger.info("MAIN: PTB polling task был отменен ранее.")

        if ptb_polling_task not in tasks_to_await and not ptb_polling_task.done(): # Если он не был отменен, но завершился
             tasks_to_await.append(ptb_polling_task)


    if 'ath_timeout_monitor' in locals() and ath_timeout_monitor and not ath_timeout_monitor.done():
        logger.info("MAIN: Отмена задачи монитора таймаутов ATH...")
        ath_timeout_monitor.cancel()
        tasks_to_await.append(ath_timeout_monitor)

    if tasks_to_await:
        logger.info(f"MAIN: Ожидание завершения {len(tasks_to_await)} фоновых задач...")
        try:
            await asyncio.gather(*tasks_to_await, return_exceptions=True)
            logger.info("MAIN: Все фоновые задачи успешно завершены или были отменены.")
        except asyncio.CancelledError: # Это может произойти, если gather сам отменяется
            logger.info("MAIN: Ожидание задач было отменено.")
    else:
        logger.info("MAIN: Нет активных задач для ожидания/отмены.")

    # Отправляем уведомление перед отключением PTB бота, если он еще работает
    #if telegram_handler and telegram_handler.ptb_bot and telegram_handler.ptb_app and telegram_handler.ptb_app.running:
    #    try:
    #        await telegram_handler.send_notification_to_admin("🛑 Бот Axiom Volume останавливается...")
    #    except Exception as e_notify_stop:
    #        logger.error(f"MAIN: Ошибка при отправке уведомления об остановке: {e_notify_stop}")
    logger.info("="*20 + " ЗАВЕРШЕНИЕ РАБОТЫ AXIOM VOLUME BOT " + "="*20)        
    if telegram_handler:
        await telegram_handler.disconnect_clients()

    logger.info("MAIN: Небольшая пауза перед закрытием браузера...")
    await asyncio.sleep(3)

    if browser_parser:
        await browser_parser.close_browser()
    
    # Финальное уведомление может не отправиться, если PTB уже отключен
    # Попробуем отправить его до disconnect_clients, но и там может быть состояние гонки
    # Это уведомление не критично
    # if telegram_handler:
    #     try:
    #         await telegram_handler.send_notification_to_admin("💤 Бот Axiom Volume полностью остановлен.")
    #     except Exception:
    #         pass # Игнорируем ошибки здесь

    logger.info("="*20 + " ЗАВЕРШЕНИЕ РАБОТЫ AXIOM VOLUME BOT " + "="*20)


if __name__ == "__main__":
    # Установка обработчиков сигналов до asyncio.run()
    # for sig_name_str_main in ('SIGINT', 'SIGTERM'): # Перенесено в main() для доступа к logger
    #     sig_name_enum_main = getattr(signal, sig_name_str_main, None)
    #     if sig_name_enum_main:
    #         try:
    #             signal.signal(sig_name_enum_main, signal_handler)
    #         except (ValueError, OSError, RuntimeError) as e_signal_main:
    #             print(f"ПРЕДУПРЕЖДЕНИЕ (__main__): Не удалось установить обработчик для сигнала {sig_name_str_main}: {e_signal_main}.", file=sys.stderr)
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("MAIN_RUN: Приложение прервано KeyboardInterrupt.", file=sys.stderr)
        if logger: logger.info("Приложение прервано KeyboardInterrupt в блоке __main__.")
    except SystemExit as e_exit:
        if logger: logger.info(f"Приложение завершено с кодом {e_exit.code}.")
        else: print(f"Приложение завершено с кодом {e_exit.code}.", file=sys.stderr)
    except Exception as e_global:
         print(f"КРИТИЧЕСКАЯ НЕПЕРЕХВАЧЕННАЯ ОШИБКА В MAIN_RUN: {type(e_global).__name__} - {e_global}", file=sys.stderr)
         # Выводим traceback, если есть логгер
         if logger: logger.critical(f"Критическая неперехваченная ошибка в блоке __main__: {e_global}", exc_info=True)
         else:
             import traceback
             traceback.print_exc() # Печатаем traceback, если логгер не настроен
    finally:
        # Здесь можно попытаться освободить ресурсы, если это абсолютно необходимо и не было сделано
        # Но основная логика очистки должна быть в main() после shutdown_event
        if logger: logger.info("MAIN_RUN: Блок finally выполнен. Программа завершает работу.")
        else: print("MAIN_RUN: Блок finally выполнен. Программа завершает работу.", file=sys.stderr)