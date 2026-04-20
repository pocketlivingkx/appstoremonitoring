import os
import json
import time
import logging
import asyncio
from datetime import datetime
from typing import Dict, List, Optional

import requests
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from dotenv import load_dotenv

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()

# Constants
SCOPES = ['https://www.googleapis.com/auth/spreadsheets']
APPS_SPREADSHEET_ID = os.getenv('APPS_SPREADSHEET_ID')  # ID таблицы с данными приложений
CHATS_SPREADSHEET_ID = os.getenv('CHATS_SPREADSHEET_ID')  # ID таблицы с данными чатов
APPS_SHEET_NAME = 'Sheet1'  # Имя листа с данными приложений
CHATS_SHEET_NAME = 'Sheet1'  # Имя листа с данными чатов
CHECK_INTERVAL = 300  # 5 minutes
CONFIRMATION_CHECKS = 5  # Количество дополнительных проверок для подтверждения
CONFIRMATION_INTERVAL = 36  # Интервал между проверками подтверждения (3 минуты / 5 проверок = 36 секунд)

# Discord webhook
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_WEBHOOK_URL')

# Emojis
EMOJI_AVAILABLE = "🟢"
EMOJI_UNAVAILABLE = "🔴"

class AppStoreMonitor:
    def __init__(self):
        self.bot_token = os.getenv('BOT_TOKEN')
        self.bot = None
        self.application = None
        self.active_chats = set()
        
        # Initialize Google Sheets API
        try:
            credentials_json = os.getenv('SHEETS_CREDENTIALS')
            if credentials_json:
                try:
                    # Пробуем использовать JSON из переменной окружения
                    credentials = service_account.Credentials.from_service_account_info(
                        json.loads(credentials_json),
                        scopes=SCOPES
                    )
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON in SHEETS_CREDENTIALS, trying to use credentials.json file")
                    # Если JSON невалидный, используем файл
                    credentials = service_account.Credentials.from_service_account_file(
                        'credentials.json',
                        scopes=SCOPES
                    )
            else:
                # Если переменная окружения не установлена, используем файл
                credentials = service_account.Credentials.from_service_account_file(
                    'credentials.json',
                    scopes=SCOPES
                )
            
            self.service = build('sheets', 'v4', credentials=credentials)
            self.sheet = self.service.spreadsheets()
        except Exception as e:
            logger.error(f"Error initializing Google Sheets API: {e}")
            raise

    def get_range(self, sheet_name: str, range_spec: str) -> str:
        """Format range string for Google Sheets API."""
        return f'{sheet_name}!{range_spec}'

    def get_app_store_link(self, app_id: str, geo: str) -> str:
        """Generate App Store link for the app."""
        return f'https://apps.apple.com/{geo}/app/{app_id}'

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle the /start command. Only authorized chats (listed in the chats sheet) receive notifications."""
        chat_id = update.effective_chat.id

        # Refresh the authorized-chats whitelist from the sheet so manual additions take effect without restart
        self.load_active_chats()

        if chat_id not in self.active_chats:
            logger.info(f"Unauthorized /start from chat {chat_id} ({update.effective_chat.title!r})")
            await update.message.reply_text(
                f"Этот чат не авторизован для мониторинга.\n"
                f"Chat ID: {chat_id}"
            )
            return

        await update.message.reply_text(
            "Чат авторизован. Уведомления об изменениях будут приходить сюда."
        )

    def load_active_chats(self):
        """Load active chats from Google Sheets."""
        try:
            result = self.sheet.values().get(
                spreadsheetId=CHATS_SPREADSHEET_ID,
                range=self.get_range(CHATS_SHEET_NAME, 'A:B')
            ).execute()
            
            values = result.get('values', [])
            self.active_chats = {int(row[0]) for row in values}
        except HttpError as e:
            logger.error(f"Google Sheets API error while loading chats: {e}")
        except Exception as e:
            logger.error(f"Error loading chats: {e}")

    def check_app_availability(self, app_id: str, geo: str) -> bool:
        """Check if an app is available in the specified region by HTTP status code."""
        url = self.get_app_store_link(app_id, geo)
        
        for attempt in range(3): # Changed from RETRY_ATTEMPTS to 3
            try:
                # Используем более реалистичные заголовки
                headers = {
                    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                    'Accept-Encoding': 'gzip, deflate, br',
                    'Connection': 'keep-alive',
                    'Upgrade-Insecure-Requests': '1',
                }
                
                response = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
                
                # Простая проверка по статус коду
                if response.status_code == 404:
                    logger.info(f"App {app_id} in {geo}: 404 Not Found")
                    return False
                elif response.status_code == 200:
                    logger.info(f"App {app_id} in {geo}: Available (200 OK)")
                    return True
                elif response.status_code >= 500:
                    # Серверная ошибка - повторяем попытку
                    logger.warning(f"App {app_id} in {geo}: Server error {response.status_code}, attempt {attempt + 1}")
                    if attempt < 2: # Changed from RETRY_ATTEMPTS - 1 to 2
                        time.sleep(5) # Changed from RETRY_DELAY to 5
                        continue
                    return False
                else:
                    logger.warning(f"App {app_id} in {geo}: Unexpected status code {response.status_code}")
                    # Для неожиданных кодов считаем приложение недоступным
                    return False
                    
            except requests.Timeout:
                logger.warning(f"Timeout checking app {app_id} in {geo}, attempt {attempt + 1}")
                if attempt < 2: # Changed from RETRY_ATTEMPTS - 1 to 2
                    time.sleep(5) # Changed from RETRY_DELAY to 5
                    continue
                return False
            except requests.RequestException as e:
                logger.error(f"Network error checking app {app_id} in {geo}: {e}, attempt {attempt + 1}")
                if attempt < 2: # Changed from RETRY_ATTEMPTS - 1 to 2
                    time.sleep(5) # Changed from RETRY_DELAY to 5
                    continue
                return False
        
        # Если все попытки исчерпаны, считаем приложение недоступным
        logger.error(f"All retry attempts failed for app {app_id} in {geo}")
        return False

    async def confirm_status_change(self, app_id: str, geo: str, expected_status: bool) -> bool:
        """Confirm status change by performing additional checks over 3 minutes."""
        logger.info(f"Starting confirmation checks for {app_id} in {geo}, expected status: {expected_status}")
        
        confirmed_count = 0
        
        for check_num in range(CONFIRMATION_CHECKS):
            await asyncio.sleep(CONFIRMATION_INTERVAL)
            
            current_status = self.check_app_availability(app_id, geo)
            logger.info(f"Confirmation check {check_num + 1}/{CONFIRMATION_CHECKS} for {app_id} in {geo}: {current_status}")
            
            if current_status == expected_status:
                confirmed_count += 1
            
        # Требуем подтверждения в большинстве проверок (минимум 3 из 5)
        confirmation_threshold = (CONFIRMATION_CHECKS + 1) // 2  # 3 из 5
        is_confirmed = confirmed_count >= confirmation_threshold
        
        logger.info(f"Confirmation result for {app_id} in {geo}: {confirmed_count}/{CONFIRMATION_CHECKS} confirmations, "
                   f"threshold: {confirmation_threshold}, confirmed: {is_confirmed}")
        
        return is_confirmed

    def read_sheet_data(self) -> List[Dict]:
        """Read data from Google Sheets including custom fields from columns F onwards."""
        try:
            # Read all columns (A:Z to capture any custom fields)
            result = self.sheet.values().get(
                spreadsheetId=APPS_SPREADSHEET_ID,
                range=self.get_range(APPS_SHEET_NAME, 'A:Z')
            ).execute()
            
            values = result.get('values', [])
            if not values:
                logger.warning('No data found in sheet')
                return []

            # Get headers from first row (for custom fields)
            headers = values[0] if values else []
            
            apps = []
            for row in values[1:]:  # Skip header row
                app_data = {
                    'app_id': row[0] if len(row) > 0 else '',
                    'app_name': row[1] if len(row) > 1 else "Unknown App",
                    'is_available': row[2].lower() == 'true' if len(row) > 2 else False,
                    'last_update': row[3] if len(row) > 3 else None,
                    'geos': [geo.strip() for geo in row[4].split(',')] if len(row) > 4 else [],
                    'custom_fields': {}
                }
                
                # Read custom fields from columns F onwards (index 5+)
                for col_index in range(5, len(row)):
                    if col_index < len(headers) and row[col_index]:
                        field_name = headers[col_index]
                        field_value = row[col_index]
                        if field_name and field_value:
                            app_data['custom_fields'][field_name] = field_value
                
                apps.append(app_data)
            
            return apps
        except HttpError as e:
            logger.error(f"Google Sheets API error while reading sheet: {e}")
            return []
        except Exception as e:
            logger.error(f"Error reading sheet: {e}")
            return []

    def update_sheet(self, row_index: int, is_available: bool):
        """Update app availability status in Google Sheets."""
        try:
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            values = [[str(is_available).lower(), current_time]]
            
            self.sheet.values().update(
                spreadsheetId=APPS_SPREADSHEET_ID,
                range=self.get_range(APPS_SHEET_NAME, f'C{row_index}:D{row_index}'),  # Обновляем колонки C и D
                valueInputOption='RAW',
                body={'values': values}
            ).execute()
        except HttpError as e:
            logger.error(f"Google Sheets API error while updating sheet: {e}")
        except Exception as e:
            logger.error(f"Error updating sheet: {e}")

    async def send_telegram_message(self, message: str):
        """Send message to all active chats."""
        if not self.bot:
            logger.warning("Telegram bot not configured")
            return

        chats_to_remove = []
        for chat_id in list(self.active_chats):  # Итерируем по копии
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='HTML'  # Включаем поддержку HTML для ссылок
                )
            except Exception as e:
                logger.error(f"Error sending message to chat {chat_id}: {e}")
                # Mark chat for removal if bot was removed
                if "bot was blocked by the user" in str(e).lower() or "chat not found" in str(e).lower():
                    chats_to_remove.append(chat_id)
        
        # Remove chats after iteration
        for chat_id in chats_to_remove:
            self.active_chats.discard(chat_id)

    async def send_discord_message(self, message: str, custom_fields: Dict = None, max_retries: int = 5):
        """Send message to Discord via webhook with retry on rate limit."""
        if not DISCORD_WEBHOOK_URL:
            logger.warning("Discord webhook URL not configured")
            return
        
        import re
        
        # Convert HTML links to Discord markdown format
        # Replace <a href='URL'>TEXT</a> with [TEXT](URL)
        discord_message = re.sub(
            r"<a href='([^']+)'>([^<]+)</a>",
            r"[\2](\1)",
            message
        )
        # Replace <b>TEXT</b> with **TEXT**
        discord_message = re.sub(r"<b>([^<]+)</b>", r"**\1**", discord_message)
        
        payload = {
            "content": discord_message
        }
        
        for attempt in range(max_retries):
            try:
                response = requests.post(
                    DISCORD_WEBHOOK_URL,
                    json=payload,
                    timeout=10
                )
                
                if response.status_code in [200, 204]:
                    logger.info("Discord message sent successfully")
                    return
                elif response.status_code == 429:
                    # Rate limited - get retry delay from response
                    retry_after = 60  # Default 60 seconds
                    try:
                        data = response.json()
                        retry_after = data.get('retry_after', 60)
                        # Also check Retry-After header
                        if 'Retry-After' in response.headers:
                            retry_after = max(retry_after, float(response.headers['Retry-After']))
                    except:
                        pass
                    
                    # Cap at 5 minutes max wait
                    retry_after = min(retry_after, 300)
                    
                    logger.warning(f"Discord rate limited (attempt {attempt + 1}/{max_retries}), "
                                   f"retry_after={retry_after}s, headers={dict(response.headers)}, body={response.text}")
                    await asyncio.sleep(retry_after)
                else:
                    logger.error(f"Discord webhook error: {response.status_code} - {response.text}")
                    return  # Don't retry on other errors
                    
            except Exception as e:
                logger.error(f"Error sending Discord message: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(5)  # Brief wait before retry on network errors
                    
        logger.error("Discord message failed after all retries")

    async def check_apps(self):
        """Main function to check all apps with confirmation mechanism."""
        logger.info("Starting apps check...")

        # Refresh authorized chats on every cycle so manual edits to the sheet take effect without a restart
        self.load_active_chats()

        apps_data = self.read_sheet_data()
        for row_index, app_data in enumerate(apps_data, start=2):  # start=2 because of header row
            app_id = app_data['app_id']
            app_name = app_data['app_name']
            current_status = app_data['is_available']
            geos = app_data['geos']
            custom_fields = app_data.get('custom_fields', {})

            # Проверяем доступность во всех регионах
            new_status_by_geo = {}
            is_available_in_any = False
            available_links = []
            status_changes = []

            for geo in geos:
                is_available = self.check_app_availability(app_id, geo)
                new_status_by_geo[geo] = is_available
                logger.info(f"App {app_id} in {geo} is {'available' if is_available else 'unavailable'}")
                
                if is_available:
                    is_available_in_any = True
                    available_links.append(f"<a href='{self.get_app_store_link(app_id, geo)}'>{geo}</a>")
                
                # Проверяем, изменился ли статус для этого региона
                if is_available != current_status:
                    status_changes.append({
                        'geo': geo,
                        'old_status': current_status,
                        'new_status': is_available
                    })

            # Если есть изменения статуса, запускаем процедуру подтверждения
            if status_changes:
                logger.info(f"Status changes detected for app {app_id}, starting confirmation process...")
                
                confirmed_changes = []
                
                # Запускаем подтверждающие проверки для каждого региона с изменением
                for change in status_changes:
                    geo = change['geo']
                    expected_status = change['new_status']
                    
                    logger.info(f"Confirming status change for {app_id} in {geo}: {change['old_status']} -> {expected_status}")
                    
                    # Выполняем 5 дополнительных проверок за 3 минуты
                    is_confirmed = await self.confirm_status_change(app_id, geo, expected_status)
                    
                    if is_confirmed:
                        confirmed_changes.append(change)
                        logger.info(f"Status change confirmed for {app_id} in {geo}")
                    else:
                        logger.info(f"Status change NOT confirmed for {app_id} in {geo}")

                # Если есть подтвержденные изменения, отправляем уведомление
                if confirmed_changes:
                    # Пересчитываем финальный статус с учетом подтвержденных изменений
                    final_available_geos = []
                    for geo in geos:
                        # Проверяем, было ли подтверждено изменение для этого региона
                        confirmed_change = next((c for c in confirmed_changes if c['geo'] == geo), None)
                        if confirmed_change:
                            # Используем подтвержденный статус
                            if confirmed_change['new_status']:
                                final_available_geos.append(geo)
                        else:
                            # Используем текущий статус из таблицы
                            if current_status:
                                final_available_geos.append(geo)
                    
                    final_status = len(final_available_geos) > 0
                    
                    # Обновляем таблицу
                    self.update_sheet(row_index, final_status)
                    
                    # Формируем сообщение
                    emoji = EMOJI_AVAILABLE if final_status else EMOJI_UNAVAILABLE
                    
                    status_change_text = []
                    for change in confirmed_changes:
                        old_text = 'доступен' if change['old_status'] else 'недоступен'
                        new_text = 'доступен' if change['new_status'] else 'недоступен'
                        status_change_text.append(f"{change['geo']}: {old_text} → {new_text}")
                    
                    message = (
                        f"{emoji} <b>{app_name}</b> (ID: {app_id})\n"
                        f"Подтвержденные изменения статуса:\n" + 
                        "\n".join(status_change_text)
                    )
                    
                    # Обновляем ссылки на доступные регионы
                    final_available_links = [f"<a href='{self.get_app_store_link(app_id, geo)}'>{geo}</a>" 
                                           for geo in final_available_geos]
                    
                    if final_available_links:
                        message += "\n\nДоступен в регионах:\n" + "\n".join(final_available_links)
                    
                    # Добавляем кастомные поля, если они есть
                    if custom_fields:
                        message += "\n"
                        for field_name, field_value in custom_fields.items():
                            message += f"\n{field_name}: {field_value}"
                    
                    # Отправляем в Telegram и Discord
                    await self.send_telegram_message(message)
                    await self.send_discord_message(message, custom_fields)
                else:
                    logger.info(f"No status changes confirmed for app {app_id}, skipping notification")

    async def run(self):
        """Main loop to run the monitor."""
        # Initialize bot
        self.application = Application.builder().token(self.bot_token).build()
        self.bot = self.application.bot
        
        # Add command handler
        self.application.add_handler(CommandHandler("start", self.start_command))
        
        # Start the bot
        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling()
        
        # Load active chats
        self.load_active_chats()
        
        logger.info("Starting App Store Monitor...")
        while True:
            try:
                await self.check_apps()
                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(60)  # Wait a minute before retrying on error

if __name__ == '__main__':
    monitor = AppStoreMonitor()
    asyncio.run(monitor.run()) 