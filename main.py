import os
import json
import time
import logging
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
CHECK_INTERVAL = 300  # 20 seconds

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
        """Handle the /start command."""
        chat_id = update.effective_chat.id
        chat_title = update.effective_chat.title or str(chat_id)
        
        # Add chat to active chats
        self.active_chats.add(chat_id)
        
        # Save chat to Google Sheets
        await self.save_chat(chat_id, chat_title)
        
        await update.message.reply_text(
            f"Привет! Я бот для мониторинга доступности приложений в App Store.\n"
            f"Теперь я буду отправлять уведомления об изменениях в этом чате."
        )

    async def save_chat(self, chat_id: int, chat_title: str):
        """Save chat ID and title to Google Sheets."""
        try:
            # Check if chat already exists
            result = self.sheet.values().get(
                spreadsheetId=CHATS_SPREADSHEET_ID,
                range=self.get_range(CHATS_SHEET_NAME, 'A:B')
            ).execute()
            
            values = result.get('values', [])
            chat_exists = any(str(chat_id) == row[0] for row in values)
            
            if not chat_exists:
                # Add new chat
                values = [[str(chat_id), chat_title]]
                self.sheet.values().append(
                    spreadsheetId=CHATS_SPREADSHEET_ID,
                    range=self.get_range(CHATS_SHEET_NAME, 'A:B'),
                    valueInputOption='RAW',
                    body={'values': values}
                ).execute()
        except HttpError as e:
            logger.error(f"Google Sheets API error while saving chat: {e}")
        except Exception as e:
            logger.error(f"Error saving chat: {e}")

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
        """Check if an app is available in the specified region."""
        url = self.get_app_store_link(app_id, geo)
        try:
            response = requests.get(url, timeout=10)
            return response.status_code != 404
        except requests.RequestException as e:
            logger.error(f"Error checking app {app_id} in {geo}: {e}")
            return False

    def read_sheet_data(self) -> List[Dict]:
        """Read data from Google Sheets."""
        try:
            result = self.sheet.values().get(
                spreadsheetId=APPS_SPREADSHEET_ID,
                range=self.get_range(APPS_SHEET_NAME, 'A:E')  # Добавлена колонка с названием
            ).execute()
            
            values = result.get('values', [])
            if not values:
                logger.warning('No data found in sheet')
                return []

            # Skip header row
            return [
                {
                    'app_id': row[0],
                    'app_name': row[1] if len(row) > 1 else "Unknown App",  # Название приложения
                    'is_available': row[2].lower() == 'true' if len(row) > 2 else False,
                    'last_update': row[3] if len(row) > 3 else None,
                    'geos': [geo.strip() for geo in row[4].split(',')] if len(row) > 4 else []
                }
                for row in values[1:]
            ]
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

        for chat_id in self.active_chats:
            try:
                await self.bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    parse_mode='HTML'  # Включаем поддержку HTML для ссылок
                )
            except Exception as e:
                logger.error(f"Error sending message to chat {chat_id}: {e}")
                # Remove chat from active chats if bot was removed
                if "bot was blocked by the user" in str(e) or "chat not found" in str(e):
                    self.active_chats.remove(chat_id)

    def check_apps(self):
        """Main function to check all apps."""
        logger.info("Starting apps check...")
        
        apps_data = self.read_sheet_data()
        for row_index, app_data in enumerate(apps_data, start=2):  # start=2 because of header row
            app_id = app_data['app_id']
            app_name = app_data['app_name']
            current_status = app_data['is_available']
            geos = app_data['geos']

            # Проверяем доступность во всех регионах
            is_available_in_any = False
            status_changed = False
            status_changes = []
            available_links = []

            for geo in geos:
                is_available = self.check_app_availability(app_id, geo)
                logger.info(f"App {app_id} in {geo} is {'available' if is_available else 'unavailable'}")
                
                if is_available:
                    is_available_in_any = True
                    available_links.append(f"<a href='{self.get_app_store_link(app_id, geo)}'>{geo}</a>")
                
                # Если статус изменился для этого региона
                if is_available != current_status:
                    status_changes.append(f"{geo}: {'доступен' if is_available else 'недоступен'}")
                    status_changed = True

            # Если общий статус изменился или изменился статус в каком-то регионе
            if is_available_in_any != current_status or status_changed:
                logger.info(f"Status changed for app {app_id}")
                self.update_sheet(row_index, is_available_in_any)
                
                # Формируем сообщение
                emoji = EMOJI_AVAILABLE if is_available_in_any else EMOJI_UNAVAILABLE
                if status_changes:
                    message = (
                        f"{emoji} <b>{app_name}</b> (ID: {app_id})\n"
                        f"Статус: {'доступен' if is_available_in_any else 'недоступен'}\n"
                        f"Изменения по регионам:\n" + 
                        "\n".join(status_changes)
                    )
                else:
                    message = (
                        f"{emoji} <b>{app_name}</b> (ID: {app_id})\n"
                        f"Статус: {'доступен' if is_available_in_any else 'недоступен'}"
                    )
                
                # Добавляем ссылки на доступные регионы
                if available_links:
                    message += "\n\nДоступен в регионах:\n" + "\n".join(available_links)
                
                asyncio.create_task(self.send_telegram_message(message))

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
                self.check_apps()
                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                await asyncio.sleep(60)  # Wait a minute before retrying on error

if __name__ == '__main__':
    import asyncio
    monitor = AppStoreMonitor()
    asyncio.run(monitor.run()) 