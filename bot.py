import os
import re
import json
import asyncio
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any

import requests
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ConfigurationError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ContextTypes, CallbackQueryHandler
)
from telegram.constants import ParseMode
from dotenv import load_dotenv

# ============================================
# LOAD ENVIRONMENT VARIABLES
# ============================================
load_dotenv()

# ============================================
# CONFIGURATION CLASS
# ============================================
class Config:
    # Bot
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    
    # MongoDB
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "instagram_bot")
    
    # Channel Subscription
    CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
    CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")
    
    # APIs
    INSTAGRAM_API_URL = os.getenv("INSTAGRAM_API_URL", "https://prexzyapis.com/download/aiov2")

# ============================================
# DATABASE MANAGER
# ============================================
class Database:
    def __init__(self):
        self.client = None
        self.db = None
        self.users = None
        self.stats = None
        self.connected = False
        
        try:
            self.client = MongoClient(
                Config.MONGODB_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=5000,
                socketTimeoutMS=5000
            )
            
            self.client.admin.command('ping')
            self.connected = True
            
            self.db = self.client[Config.DATABASE_NAME]
            self.users = self.db.users
            self.stats = self.db.stats
            
            self.users.create_index("user_id", unique=True)
            self.users.create_index("username")
            
            if not self.stats.find_one({"_id": "bot_stats"}):
                self.stats.insert_one({
                    "_id": "bot_stats",
                    "total_users": 0,
                    "total_downloads": 0,
                    "created_at": datetime.now()
                })
            
            logging.info("✅ MongoDB Connected Successfully!")
            
        except (ConnectionFailure, ConfigurationError) as e:
            logging.error(f"❌ MongoDB Connection Error: {e}")
            logging.warning("⚠️ Bot will run without database features!")
            self.connected = False
        except Exception as e:
            logging.error(f"❌ MongoDB Error: {e}")
            self.connected = False

    def _check_connection(self):
        if not self.connected:
            logging.warning("⚠️ Database not connected, operation skipped")
            return False
        return True

    def add_user(self, user_id: int, username: str = None, first_name: str = None):
        if not self._check_connection():
            return False
        try:
            result = self.users.update_one(
                {"user_id": user_id},
                {
                    "$set": {
                        "username": username,
                        "first_name": first_name,
                        "last_active": datetime.now(),
                        "is_blocked": False
                    },
                    "$setOnInsert": {
                        "joined_at": datetime.now(),
                        "total_downloads": 0
                    }
                },
                upsert=True
            )
            if result.upserted_id:
                self.stats.update_one(
                    {"_id": "bot_stats"},
                    {"$inc": {"total_users": 1}}
                )
            return True
        except Exception as e:
            logging.error(f"Error adding user: {e}")
            return False

    def remove_user(self, user_id: int):
        if not self._check_connection():
            return False
        try:
            result = self.users.delete_one({"user_id": user_id})
            if result.deleted_count > 0:
                self.stats.update_one(
                    {"_id": "bot_stats"},
                    {"$inc": {"total_users": -1}}
                )
                return True
            return False
        except Exception as e:
            logging.error(f"Error removing user: {e}")
            return False

    def get_user(self, user_id: int):
        if not self._check_connection():
            return None
        try:
            return self.users.find_one({"user_id": user_id})
        except Exception as e:
            logging.error(f"Error getting user: {e}")
            return None

    def get_all_users(self, skip_blocked: bool = True):
        if not self._check_connection():
            return []
        try:
            query = {"is_blocked": {"$ne": True}} if skip_blocked else {}
            return list(self.users.find(query))
        except Exception as e:
            logging.error(f"Error getting users: {e}")
            return []

    def get_total_users(self, skip_blocked: bool = True):
        if not self._check_connection():
            return 0
        try:
            query = {"is_blocked": {"$ne": True}} if skip_blocked else {}
            return self.users.count_documents(query)
        except Exception as e:
            logging.error(f"Error counting users: {e}")
            return 0

    def mark_user_blocked(self, user_id: int):
        if not self._check_connection():
            return False
        try:
            self.users.update_one(
                {"user_id": user_id},
                {"$set": {"is_blocked": True, "blocked_at": datetime.now()}}
            )
            return True
        except Exception as e:
            logging.error(f"Error marking user blocked: {e}")
            return False

    def increment_downloads(self, user_id: int):
        if not self._check_connection():
            return False
        try:
            self.users.update_one(
                {"user_id": user_id},
                {"$inc": {"total_downloads": 1}}
            )
            self.stats.update_one(
                {"_id": "bot_stats"},
                {"$inc": {"total_downloads": 1}}
            )
            return True
        except Exception as e:
            logging.error(f"Error incrementing downloads: {e}")
            return False

    def get_stats(self):
        if not self._check_connection():
            return {"total_users": 0, "total_downloads": 0, "created_at": datetime.now()}
        try:
            stats = self.stats.find_one({"_id": "bot_stats"})
            if stats:
                del stats["_id"]
            return stats or {}
        except Exception as e:
            logging.error(f"Error getting stats: {e}")
            return {}

# ============================================
# PLATFORM API MANAGER
# ============================================
class PlatformAPIManager:
    @staticmethod
    def detect_platform(url: str) -> str:
        url_lower = url.lower()
        if "instagram.com" in url_lower or "instagr.am" in url_lower:
            return "instagram"
        elif "youtube.com" in url_lower or "youtu.be" in url_lower:
            return "youtube"
        elif "tiktok.com" in url_lower or "vt.tiktok.com" in url_lower:
            return "tiktok"
        elif "facebook.com" in url_lower or "fb.watch" in url_lower:
            return "facebook"
        else:
            return "unknown"

    @staticmethod
    async def fetch_instagram(url: str) -> Dict:
        try:
            response = requests.get(
                Config.INSTAGRAM_API_URL,
                params={"url": url},
                timeout=30
            )
            data = response.json()
            
            if data.get("status") == True and data.get("result", {}).get("ins_bos"):
                media_list = data["result"]["ins_bos"]
                caption = data["result"].get("introduce", "No caption")
                
                return {
                    "success": True,
                    "platform": "instagram",
                    "media_list": media_list,
                    "caption": caption,
                    "author": media_list[0].get("author", "Unknown") if media_list else "Unknown"
                }
            return {"success": False, "error": "No media found"}
            
        except requests.exceptions.Timeout:
            return {"success": False, "error": "Timeout! Server took too long"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @staticmethod
    async def fetch_media(url: str) -> Dict:
        platform = PlatformAPIManager.detect_platform(url)
        
        if platform == "instagram":
            return await PlatformAPIManager.fetch_instagram(url)
        else:
            return {"success": False, "error": f"Unsupported platform. Currently supports: Instagram"}

# ============================================
# UTILITY FUNCTIONS
# ============================================
class Utils:
    @staticmethod
    def format_caption(original_caption: str, bot_username: str, platform: str = "Instagram") -> str:
        if not original_caption or original_caption.strip() == "":
            original_caption = "No caption"
        
        max_length = 900
        if len(original_caption) > max_length:
            original_caption = original_caption[:max_length - 3] + "..."
        
        return f"""<b>📥 Downloaded From {platform}</b>
<b>By @{bot_username}</b>

<blockquote expandable>{original_caption}</blockquote>

<b>🤖 Want this bot in your group too?</b>
Click the button below to add me!"""

    @staticmethod
    def get_add_button(bot_username: str) -> InlineKeyboardMarkup:
        keyboard = [[
            InlineKeyboardButton(
                "➕ Add Bot to Your Group",
                url=f"https://t.me/{bot_username}?startgroup=true"
            )
        ]]
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    async def check_subscription(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
        if not Config.CHANNEL_ID:
            return True
            
        try:
            member = await context.bot.get_chat_member(
                chat_id=Config.CHANNEL_ID,
                user_id=user_id
            )
            return member.status in [
                ChatMember.MEMBER,
                ChatMember.ADMINISTRATOR,
                ChatMember.CREATOR
            ]
        except Exception as e:
            logging.error(f"Subscription check error: {e}")
            return False

    @staticmethod
    def get_subscription_button() -> InlineKeyboardMarkup:
        keyboard = []
        
        if Config.CHANNEL_LINK:
            keyboard.append([
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=Config.CHANNEL_LINK
                )
            ])
        
        keyboard.append([
            InlineKeyboardButton(
                "✅ Check Subscription",
                callback_data="check_subscription"
            )
        ])
            
        return InlineKeyboardMarkup(keyboard)

    @staticmethod
    async def download_media_with_retry(url: str, max_retries: int = 3) -> Optional[bytes]:
        for attempt in range(max_retries):
            try:
                response = requests.get(
                    url,
                    timeout=60,
                    headers={
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                    }
                )
                if response.status_code == 200:
                    return response.content
            except Exception as e:
                logging.error(f"Download attempt {attempt+1} failed: {e}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 * (attempt + 1))
        return None

# ============================================
# DATABASE INSTANCE
# ============================================
db = Database()

# ============================================
# BOT COMMAND HANDLERS
# ============================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type
    is_group = chat_type in ["group", "supergroup"]
    
    db.add_user(
        user_id=user.id,
        username=user.username,
        first_name=user.first_name
    )
    
    welcome_msg = f"""👋 **Welcome {user.first_name}!**

I'm an **Instagram Media Downloader Bot**!

📌 **Features:**
• Download Instagram Reels
• Download Instagram Videos
• Download Instagram Images
• Carousel Posts Supported
• High Quality

🔗 **How to use:**
Just send me any Instagram URL and I'll download it for you!

💡 **Supported URLs:**
• https://www.instagram.com/reels/...
• https://www.instagram.com/p/...
• https://www.instagram.com/tv/..."""

    if is_group:
        welcome_msg += "\n\n📊 **Group Mode:** Active"
    else:
        if Config.CHANNEL_ID:
            is_subscribed = await Utils.check_subscription(context, user.id)
            if not is_subscribed:
                welcome_msg += f"\n\n⚠️ **Please join our channel to use this bot!**\n\nClick the button below to join and then check subscription."

    await update.message.reply_text(
        welcome_msg,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=Utils.get_add_button(context.bot.username)
    )

async def check_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = update.effective_user.id
    is_subscribed = await Utils.check_subscription(context, user_id)
    
    if is_subscribed:
        await query.edit_message_text(
            "✅ **You are subscribed!** 🎉\n\nNow you can use the bot.\nSend me any Instagram URL to download!",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await query.edit_message_text(
            "❌ **You are not subscribed yet!**\n\nPlease join the channel first and then click **Check Subscription** again.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=Utils.get_subscription_button()
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in Config.ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    stats = db.get_stats()
    total_users = db.get_total_users()
    blocked_users = db.get_total_users(skip_blocked=False) - db.get_total_users(skip_blocked=True)
    
    stats_msg = f"""📊 **Bot Statistics**

👥 **Total Users:** {total_users}
🚫 **Blocked Users:** {blocked_users}
📥 **Total Downloads:** {stats.get('total_downloads', 0)}
📅 **Bot Created:** {stats.get('created_at', 'N/A')}
"""
    
    await update.message.reply_text(stats_msg, parse_mode=ParseMode.MARKDOWN)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    
    if user_id not in Config.ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ Please reply to a message you want to broadcast.\n\n"
            "Usage: Reply to any message with `/broadcast`"
        )
        return
    
    users = db.get_all_users(skip_blocked=True)
    total_users = len(users)
    
    if total_users == 0:
        await update.message.reply_text("❌ No users found to broadcast to.")
        return
    
    confirm_msg = await update.message.reply_text(
        f"📢 **Broadcast Started!**\n\n"
        f"👥 Total Users: {total_users}\n"
        f"⏳ Sending messages...\n\n"
        f"_This might take a while._",
        parse_mode=ParseMode.MARKDOWN
    )
    
    reply_msg = update.message.reply_to_message
    success_count = 0
    failed_count = 0
    blocked_count = 0
    
    for user in users:
        try:
            user_id = user["user_id"]
            
            await reply_msg.copy(
                chat_id=user_id,
                reply_to_message_id=None
            )
            
            success_count += 1
            await asyncio.sleep(0.05)
            
        except Exception as e:
            error_str = str(e).lower()
            if "blocked" in error_str or "not found" in error_str:
                db.remove_user(user_id)
                blocked_count += 1
            else:
                failed_count += 1
                logging.error(f"Broadcast failed to {user_id}: {e}")
    
    await confirm_msg.edit_text(
        f"✅ **Broadcast Completed!**\n\n"
        f"📤 Total Users: {total_users}\n"
        f"✅ Success: {success_count}\n"
        f"❌ Failed: {failed_count}\n"
        f"🚫 Blocked/Removed: {blocked_count}"
    )

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type
    is_group = chat_type in ["group", "supergroup"]
    text = update.message.text or update.message.caption or ""
    
    url_pattern = r'(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s]+)'
    urls = re.findall(url_pattern, text)
    
    if not urls:
        return
    
    if not is_group and Config.CHANNEL_ID:
        is_subscribed = await Utils.check_subscription(context, user.id)
        if not is_subscribed:
            await update.message.reply_text(
                "⚠️ **You need to join our channel first!**\n\n"
                "Please join the channel and then try again.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=Utils.get_subscription_button()
            )
            return
    
    for url in urls:
        processing_msg = await update.message.reply_text(
            f"⏳ Processing: {url[:50]}...\nFetching media..."
        )
        
        try:
            result = await PlatformAPIManager.fetch_media(url)
            
            if not result["success"]:
                await processing_msg.edit_text(
                    f"❌ Error: {result.get('error', 'Unknown error')}"
                )
                continue
            
            media_list = result["media_list"]
            caption = result["caption"]
            platform = result["platform"]
            
            formatted_caption = Utils.format_caption(
                caption, 
                context.bot.username,
                platform
            )
            
            db.add_user(
                user_id=user.id,
                username=user.username,
                first_name=user.first_name
            )
            db.increment_downloads(user.id)
            
            add_button = Utils.get_add_button(context.bot.username)
            
            if len(media_list) > 1:
                await processing_msg.edit_text(
                    f"📸 Found {len(media_list)} media items. Downloading..."
                )
                
                media_group = []
                temp_files = []
                failed = 0
                
                for idx, media in enumerate(media_list):
                    media_url = media.get("url")
                    media_type = media.get("type", "mp4")
                    
                    if not media_url:
                        continue
                    
                    try:
                        await processing_msg.edit_text(
                            f"📥 Downloading {idx+1}/{len(media_list)}..."
                        )
                        
                        content = await Utils.download_media_with_retry(media_url)
                        if not content:
                            failed += 1
                            continue
                        
                        ext = "mp4" if media_type == "mp4" else "jpg"
                        file_path = f"/tmp/media_{user.id}_{idx}.{ext}"
                        
                        with open(file_path, "wb") as f:
                            f.write(content)
                        temp_files.append(file_path)
                        
                        if media_type == "mp4":
                            media_group.append(
                                InputMediaVideo(
                                    media=open(file_path, "rb"),
                                    caption=formatted_caption if idx == 0 else "",
                                    parse_mode=ParseMode.HTML
                                )
                            )
                        else:
                            media_group.append(
                                InputMediaPhoto(
                                    media=open(file_path, "rb"),
                                    caption=formatted_caption if idx == 0 else "",
                                    parse_mode=ParseMode.HTML
                                )
                            )
                            
                    except Exception as e:
                        logging.error(f"Error downloading media {idx+1}: {e}")
                        failed += 1
                
                if media_group:
                    await context.bot.send_media_group(
                        chat_id=update.effective_chat.id,
                        media=media_group,
                        reply_to_message_id=update.message.message_id
                    )
                    
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="🤖 **Want this bot in your group too?**\nClick the button below to add me!",
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=add_button,
                        reply_to_message_id=update.message.message_id
                    )
                
                for file_path in temp_files:
                    try:
                        os.remove(file_path)
                    except:
                        pass
                
                if failed > 0:
                    await processing_msg.edit_text(
                        f"⚠️ Downloaded {len(media_group)}/{len(media_list)} media files. {failed} failed."
                    )
                else:
                    await processing_msg.delete()
                    
            else:
                media = media_list[0]
                media_url = media.get("url")
                media_type = media.get("type", "mp4")
                
                await processing_msg.edit_text("📥 Downloading media...")
                
                content = await Utils.download_media_with_retry(media_url)
                
                if content:
                    if media_type == "mp4":
                        await update.message.reply_video(
                            video=content,
                            caption=formatted_caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=add_button,
                            supports_streaming=True
                        )
                    else:
                        await update.message.reply_photo(
                            photo=content,
                            caption=formatted_caption,
                            parse_mode=ParseMode.HTML,
                            reply_markup=add_button
                        )
                    await processing_msg.delete()
                else:
                    await processing_msg.edit_text("❌ Failed to download media. Please try again.")
                    
        except Exception as e:
            logging.error(f"Error processing URL: {e}")
            await processing_msg.edit_text(
                f"❌ Error: {str(e)}\nPlease try again later."
            )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logging.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ Something went wrong! Please try again later."
        )

# ============================================
# MAIN FUNCTION
# ============================================
def main():
    # Setup logging
    logging.basicConfig(
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        level=logging.INFO
    )
    
    # Create application
    application = Application.builder().token(Config.BOT_TOKEN).build()
    
    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(
        check_subscription_callback, 
        pattern="check_subscription"
    ))
    
    # Message handlers
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_media
    ))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    # ✅ FIX: Initialize bot before accessing properties
    async def bot_init():
        await application.initialize()
        bot_info = await application.bot.get_me()
        print("🤖 Bot Started!")
        print(f"📌 Bot Username: @{bot_info.username}")
        print("📌 Send any Instagram URL to test")
        
        if Config.CHANNEL_ID:
            print(f"📢 Force Subscribe Channel ID: {Config.CHANNEL_ID}")
            print("   (Only for Private Chats)")
    
    # Run the bot with initialization
    async def run():
        await bot_init()
        await application.start()
        await application.updater.start_polling()
        await asyncio.Event().wait()  # Keep running
    
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n🛑 Bot Stopped!")
    finally:
        asyncio.run(application.shutdown())

if __name__ == "__main__":
    main()
