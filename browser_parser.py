# browser_parser.py (Исправленная функция launch_browser)

import logging
import re # Для извлечения CA из URL
from typing import List, Dict, Any, Optional
from playwright.async_api import (
    async_playwright,
    Browser,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
    Locator
)

# Импортируем конфигурацию и утилиты
import config
import utils

logger = logging.getLogger(__name__)

class BrowserParser:
    def __init__(self): # <-- Убедитесь, что self.browser здесь есть!
        self.playwright: Optional[Playwright] = None
        self.browser: Optional[Browser] = None 
        self.page: Optional[Page] = None
        self.context = None

    async def launch_browser(self) -> bool:
        """Запускает браузер Chrome с указанным профилем и пытается скрыть автоматизацию."""
        # --- ПРОВЕРКА СУЩЕСТВОВАНИЯ АТРИБУТА (на всякий случай) ---
        browser_exists_and_connected = False
        if hasattr(self, 'browser') and self.browser is not None: 
             # Только если атрибут есть и он не None, проверяем соединение
             try:
                 if self.browser.is_connected():
                     browser_exists_and_connected = True
             except Exception as e: 
                 # На случай если is_connected() вызовет ошибку на "мертвом" объекте
                 logger.warning(f"Ошибка при проверке self.browser.is_connected(): {e}")
                 browser_exists_and_connected = False # Считаем, что не подключен

        if browser_exists_and_connected:
        # --- КОНЕЦ ДОПОЛНИТЕЛЬНОЙ ПРОВЕРКИ ---
            logger.warning("Браузер уже запущен и подключен.")
            if self.page and not self.page.is_closed():
                 logger.debug("Страница существует и не закрыта.")
                 return True
            else:
                 logger.warning("Браузер запущен, но страница отсутствует или закрыта. Попытка создать новую.")
                 try:
                     if not self.context: 
                         logger.error("Браузер есть, но контекст потерян. Требуется перезапуск.")
                         self.browser = None 
                         self.context = None
                         self.page = None
                         return False 
                     self.page = await self.context.new_page()
                     self.page.set_default_timeout(config.PLAYWRIGHT_TIMEOUT)
                     self.page.set_default_navigation_timeout(config.PLAYWRIGHT_TIMEOUT)
                     await self.page.add_init_script("""
                         if (navigator.webdriver) {
                             Object.defineProperty(navigator, 'webdriver', { get: () => false });
                         }
                     """)
                     logger.info("Новая страница успешно создана.")
                     return True
                 except Exception as e:
                     logger.error(f"Не удалось создать новую страницу в уже запущенном браузере: {e}", exc_info=True)
                     self.browser = None
                     self.context = None
                     self.page = None
                     return False
        
        # Если браузер не запущен или не подключен, продолжаем запуск
        logger.info("Запуск нового экземпляра браузера...")
            
        try:
            # Закрываем старые ресурсы, если они есть
            if hasattr(self, 'context') and self.context: 
                try: await self.context.close() 
                except Exception: pass # Игнорируем ошибки при закрытии старого контекста
                self.context = None
                self.browser = None # Браузер закрывается вместе с контекстом
                self.page = None
            if hasattr(self, 'playwright') and self.playwright: 
                try: await self.playwright.stop() 
                except Exception: pass # Игнорируем ошибки при остановке старого playwright
                self.playwright = None

            logger.debug("Запуск Playwright...")
            self.playwright = await async_playwright().start()
            
            logger.info(f"Запуск Chrome с профилем: {config.CHROME_PROFILE_PATH}")
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=config.CHROME_PROFILE_PATH,
                headless=False, 

                args=[
                    '--no-first-run',
                    '--no-default-browser-check',
                    '--disable-blink-features=AutomationControlled' 
                ],
                ignore_default_args=["--enable-automation"], 
                user_agent=config.USER_AGENT,
            )
            # Присваиваем self.browser ПОСЛЕ получения контекста
            self.browser = self.context.browser 

            if not self.context.pages:
                 self.page = await self.context.new_page()
                 logger.debug("Создана новая страница (контекст был без страниц).")
            else:
                 # Берем первую страницу из уже существующих в контексте
                 self.page = self.context.pages[0]
                 logger.debug("Используется существующая страница из контекста.")
            
            await self.page.add_init_script("""
                if (navigator.webdriver) {
                    Object.defineProperty(navigator, 'webdriver', {
                        get: () => false,
                    });
                }
            """)
            
            self.page.set_default_timeout(config.PLAYWRIGHT_TIMEOUT)
            self.page.set_default_navigation_timeout(config.PLAYWRIGHT_TIMEOUT)

            logger.info("Браузер успешно запущен (с попыткой маскировки).")
            return True
        except PlaywrightError as e:
            logger.error(f"Ошибка Playwright при запуске браузера: {e}", exc_info=True)
            await self.close_browser() # close_browser должен безопасно обрабатывать None
            return False
        except Exception as e:
             logger.error(f"Непредвиденная ошибка при запуске браузера: {e}", exc_info=True)
             await self.close_browser() # close_browser должен безопасно обрабатывать None
             return False
                
    
    # ... (остальные методы класса BrowserParser остаются без изменений) ...

    async def navigate_to_axiom(self) -> bool:
        # ... (без изменений) ...
        if not self.page:
            logger.error("Страница браузера не инициализирована.")
            return False
        try:
            logger.info(f"Переход на URL: {config.AXIOM_URL}")
            await self.page.goto(config.AXIOM_URL, wait_until='domcontentloaded') # Ждем загрузки DOM
            logger.info("Успешно перешли на страницу Axiom.")
            await self.page.wait_for_timeout(3000) # 3 секунды
            return True
        except PlaywrightTimeoutError:
            logger.error(f"Таймаут при загрузке страницы {config.AXIOM_URL}")
            return False
        except PlaywrightError as e:
            logger.error(f"Ошибка Playwright при навигации: {e}", exc_info=True)
            return False
        except Exception as e:
             logger.error(f"Непредвиденная ошибка при навигации: {e}", exc_info=True)
             return False

    async def wait_for_login_confirmation(self) -> bool:
        # ... (без изменений) ...
        if not self.page:
            logger.error("Страница браузера не инициализирована.")
            return False
        
        login_indicator_selector = 'button:has-text("PRESET 1")' 

        try:
            logger.info("Проверка входа в аккаунт (поиск 'PRESET 1')...")
            await self.page.locator(login_indicator_selector).wait_for(state='visible', timeout=5000) # Ждем 5 секунд
            logger.info("Индикатор 'PRESET 1' найден. Вход подтвержден.")
            return True
        except PlaywrightTimeoutError:
            logger.warning("Индикатор 'PRESET 1' не найден за 5 секунд.")
            return False
        except PlaywrightError as e:
            logger.error(f"Ошибка Playwright при поиске 'PRESET 1': {e}")
            return False
        except Exception as e:
             logger.error(f"Непредвиденная ошибка при проверке входа: {e}", exc_info=True)
             return False


    async def _extract_ca_from_image(self, row_locator: Locator) -> Optional[str]:
        # ... (без изменений) ...
        try:
            img_locator = row_locator.locator('img[src*="cdn.digitaloceanspaces.com/"]')
            if await img_locator.count() > 0:
                img_src = await img_locator.first.get_attribute('src')
                if img_src:
                    match = re.search(r'/([^/]+)\.webp$', img_src)
                    if match:
                        ca = match.group(1)
                        if utils.is_valid_solana_address(ca):
                            logger.debug(f"CA извлечен из URL картинки: {ca}")
                            return ca
                        else:
                            logger.warning(f"Извлеченная строка из URL '{ca}' не является валидным CA.")
        except PlaywrightError as e:
            logger.warning(f"Ошибка при поиске картинки для CA: {e}")
        return None
        
    async def _extract_ca_from_clipboard(self, row_locator: Locator) -> Optional[str]:
        # ... (без изменений) ...
        if not self.page: return None
        try:
            copy_button_locator = row_locator.locator('button:has(i.ri-file-copy-fill)')
            if await copy_button_locator.count() > 0:
                logger.debug("Найдена кнопка копирования CA, попытка клика и чтения буфера...")
                await copy_button_locator.first.click(timeout=1000) 
                await self.page.wait_for_timeout(200) 
                
                clipboard_text = await self.page.evaluate('navigator.clipboard.readText()')
                
                if utils.is_valid_solana_address(clipboard_text):
                    logger.debug(f"CA извлечен из буфера обмена: {clipboard_text}")
                    return clipboard_text
                else:
                     logger.warning(f"Текст из буфера обмена '{clipboard_text}' не является валидным CA.")
            else:
                 logger.debug("Кнопка копирования CA не найдена в строке.")

        except PlaywrightTimeoutError:
             logger.warning("Таймаут при клике на кнопку копирования CA.")
        except PlaywrightError as e:
            logger.warning(f"Ошибка Playwright при работе с буфером обмена для CA: {e}")
        except Exception as e:
            logger.error(f"Непредвиденная ошибка при извлечении CA из буфера: {e}", exc_info=True)
            
        return None

    async def parse_discover_page(self) -> Optional[List[Dict[str, Any]]]:
        """Парсит данные монет со страницы Axiom Discover."""
        # ... (начало функции без изменений: проверка self.page, поиск контейнера и строк) ...
        if not self.page:
            logger.error("Страница браузера не инициализирована.")
            return None

        logger.info("Начало парсинга страницы Discover...")
        
        rows_container_selector = 'div[style*="overflow-y: auto"] >> div[style*="visibility: visible"]'
        row_selector = f'{rows_container_selector} > div[class*="border-primaryStroke/50"][class*="h-\\["]'
        
        try:
            rows = self.page.locator(row_selector)
            count = await rows.count()
            if count == 0:
                logger.info("Строки с монетами не найдены на странице.")
                return [] 
            logger.info(f"Найдено {count} строк с монетами для парсинга.")

        except PlaywrightError as e:
             logger.error(f"Ошибка при поиске строк монет: {e}", exc_info=True)
             return None

        parsed_coins = []
        # --- ДОБАВЛЕНИЕ СЛУЧАЙНЫХ ЗАДЕРЖЕК ---
        import random
        import asyncio
        # -----------------------------------

        for i in range(count):
            # --- ДОБАВЛЕНИЕ СЛУЧАЙНЫХ ЗАДЕРЖЕК ---
            # Небольшая пауза перед обработкой каждой строки (имитация чтения)
            delay = random.uniform(0.1, 0.5) # от 100 до 500 мс
            logger.debug(f"Пауза перед строкой {i+1}: {delay:.2f} сек")
            await asyncio.sleep(delay)
            # -----------------------------------

            row_locator = rows.nth(i)
            coin_data = {}
            # ... (остальная логика парсинга строки остается без изменений) ...
            # ... (извлечение CA, Tic, Age, MC, L, V) ...
            try:
                logger.debug(f"Парсинг строки {i+1}...")
                ca = await self._extract_ca_from_image(row_locator)
                if not ca:
                    if not ca:
                         logger.debug("Попытка извлечь CA через буфер обмена...")
                         ca = await self._extract_ca_from_clipboard(row_locator)

                if not ca:
                    logger.warning(f"Не удалось извлечь CA для строки {i+1}. Пропуск монеты.")
                    continue 

                coin_data['ca'] = ca

                pair_info_block = row_locator.locator('div[class*="w-\\["]').first 
                coin_data['tic'] = await pair_info_block.locator('span[class*="text-textPrimary"]').first.text_content(timeout=1000) or "N/A"
                coin_data['tic'] = coin_data['tic'].strip()

                coin_data['age_str'] = await pair_info_block.locator('span[class*="text-primaryGreen"]').first.text_content(timeout=1000) or "N/A"
                coin_data['age_str'] = coin_data['age_str'].strip()

                columns = row_locator.locator('div.min-w-0.flex-1.flex-row.px-\\[12px\\]')
                
                mc_str = await columns.nth(0).locator('span.text-textPrimary').first.text_content(timeout=1000) or "0"
                l_str = await columns.nth(1).locator('span.text-textPrimary').first.text_content(timeout=1000) or "0"
                v_str = await columns.nth(2).locator('span.text-textPrimary').first.text_content(timeout=1000) or "0"
                
                coin_data['mc'] = utils.parse_monetary_value(mc_str) or 0
                coin_data['l'] = utils.parse_monetary_value(l_str) or 0
                coin_data['v'] = utils.parse_monetary_value(v_str) or 0
                
                logger.debug(f"Спарсены данные: CA={coin_data['ca']}, Tic={coin_data['tic']}, Age={coin_data['age_str']}, MC={coin_data['mc']}, L={coin_data['l']}, V={coin_data['v']}")
                parsed_coins.append(coin_data)

            except PlaywrightTimeoutError:
                 logger.warning(f"Таймаут при парсинге данных в строке {i+1}. Пропуск монеты.")
            except PlaywrightError as e:
                 logger.error(f"Ошибка Playwright при парсинге строки {i+1}: {e}")
            except Exception as e:
                 logger.error(f"Непредвиденная ошибка при парсинге строки {i+1}: {e}", exc_info=True)


        logger.info(f"Парсинг завершен. Успешно обработано {len(parsed_coins)} монет.")
        
        if len(parsed_coins) < count * 0.8: 
             logger.warning(f"Спарсено значительно меньше монет ({len(parsed_coins)}), чем найдено строк ({count}). Возможны проблемы с парсингом.")

        return parsed_coins

    async def close_browser(self):
        """Закрывает браузер и Playwright."""
        logger.info("Закрытие браузера...")
        # Закрываем контекст (он закроет и браузер)
        if hasattr(self, 'context') and self.context:
            try:
                # --- ДОБАВЛЯЕМ ОБРАБОТКУ ОШИБКИ ЗДЕСЬ ---
                await self.context.close()
                logger.info("Контекст браузера успешно закрыт.")
            except PlaywrightError as e:
                # Ловим ошибку "Connection closed", если она возникает при закрытии
                if "Connection closed" in str(e):
                    logger.warning(f"Ошибка при закрытии контекста (соединение уже было закрыто): {e}")
                else:
                    logger.error(f"Ошибка Playwright при закрытии контекста браузера: {e}", exc_info=True)
            except Exception as e: 
                logger.error(f"Неожиданная ошибка при закрытии контекста: {e}", exc_info=True)
            # Сбрасываем переменные в любом случае
        self.context = None
        self.browser = None 
        self.page = None
            
        # Останавливаем Playwright
        if hasattr(self, 'playwright') and self.playwright:
            try:
                await self.playwright.stop()
                logger.info("Playwright остановлен.")
            except PlaywrightError as e:
                logger.error(f"Ошибка при остановке Playwright: {e}", exc_info=True)
            except Exception as e: 
                logger.error(f"Неожиданная ошибка при остановке Playwright: {e}", exc_info=True)
        self.playwright = None
        logger.info("Процедура закрытия браузера завершена.")
 

# Пример использования (для тестирования модуля)
# ... (блок if __name__ == '__main__': остается без изменений) ...

# Пример использования (для тестирования модуля)
if __name__ == '__main__':
    import asyncio
    import sys

    async def run_test():
        # Настройка логирования
        from config import setup_logging
        setup_logging()

        parser = BrowserParser()
        if not await parser.launch_browser():
            logger.error("Не удалось запустить браузер. Выход.")
            return

        if not await parser.navigate_to_axiom():
             logger.error("Не удалось перейти на страницу Axiom. Выход.")
             await parser.close_browser()
             return

        # --- Шаг ожидания входа ---
        print("\n" + "="*30)
        print("ПОЖАЛУЙСТА, ВОЙДИТЕ В АККАУНТ AXIOM В ОТКРЫВШЕМСЯ ОКНЕ БРАУЗЕРА.")
        print("Настройте нужные фильтры (если требуется).")
        input("После входа и настройки фильтров, нажмите Enter здесь для продолжения...")
        print("="*30 + "\n")

        if not await parser.wait_for_login_confirmation():
            logger.error("Вход не подтвержден (не найден 'PRESET 1'). Проверьте страницу.")
            # Не выходим, попробуем спарсить что есть
            # await parser.close_browser()
            # return
        else:
             logger.info("Вход подтвержден.")
             
        # --- Парсинг ---
        parsed_data = await parser.parse_discover_page()

        if parsed_data is not None:
            logger.info(f"\n--- Результаты парсинга ({len(parsed_data)} монет) ---")
            for i, coin in enumerate(parsed_data):
                print(f"{i+1}. CA: {coin.get('ca')}, Tic: {coin.get('tic')}, Age: {coin.get('age_str')}, "
                      f"MC: {coin.get('mc')}, L: {coin.get('l')}, V: {coin.get('v')}")
            logger.info("--- Конец результатов ---")
        else:
            logger.error("Парсинг не удался.")

        await parser.close_browser()

    # Запуск асинхронного теста
    try:
        asyncio.run(run_test())
    except KeyboardInterrupt:
         logger.info("Тестирование прервано пользователем.")