import os
import re
import asyncio
import logging
from typing import Dict, Optional
from io import BytesIO

import aiohttp
from pymongo import MongoClient
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatMember, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, 
    ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import NetworkError, Conflict
from dotenv import load_dotenv

# ============================================
# LOAD ENVIRONMENT VARIABLES
# ============================================
load_dotenv()

# ============================================
# CONFIGURATION CLASS
# ============================================
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN")
    ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
    MONGODB_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    DATABASE_NAME = os.getenv("DATABASE_NAME", "instagram_bot")
    
    # Dual support for Channel or Group ID
    CHAT_ID = int(os.getenv("CHAT_ID", os.getenv("CHANNEL_ID", "0"))) if (os.getenv("CHAT_ID") or os.getenv("CHANNEL_ID")) else None
    CHAT_LINK = os.getenv("CHAT_LINK", os.getenv("CHANNEL_LINK", ""))
    CHAT_USERNAME = os.getenv("CHAT_USERNAME", os.getenv("CHANNEL_USERNAME", "")).replace("@", "")
    
    INSTAGRAM_API_URL = os.getenv("INSTAGRAM_API_URL", "https://prexzyapis.com/download/aiov2")
    WELCOME_IMAGE_URL = os.getenv("WELCOME_IMAGE_URL", "https://picsum.photos/800/400")

# ============================================
# MINIMAL DATABASE MANAGER (Only Numeric IDs)
# ============================================
class Database:
    def __init__(self):
        self.client = None
        self.db = None
        self.users = None
        self.connected = False
        
        try:
            self.client = MongoClient(
                Config.MONGODB_URI,
                serverSelectionTimeoutMS=5000
            )
            self.client.admin.command('ping')
            self.connected = True
            self.db = self.client[Config.DATABASE_NAME]
            self.users = self.db.users
            self.users.create_index("user_id", unique=True)
            logging.info("✅ MongoDB Connected Successfully!")
        except Exception as e:
            logging.error(f"❌ MongoDB Error: {e}")
            self.connected = False

    def add_user(self, user_id: int):
        if not self.connected:
            return False
        try:
            self.users.update_one(
                {"user_id": user_id},
                {"$setOnInsert": {"user_id": user_id}},
                upsert=True
            )
            return True
        except Exception as e:
            logging.error(f"Error adding user: {e}")
            return False

    def remove_user(self, user_id: int):
        if not self.connected:
            return False
        try:
            self.users.delete_one({"user_id": user_id})
            return True
        except Exception as e:
            logging.error(f"Error removing user: {e}")
            return False

    def get_all_users(self):
        if not self.connected:
            return []
        try:
            return list(self.users.find({}, {"_id": 0, "user_id": 1}))
        except Exception as e:
            logging.error(f"Error getting users: {e}")
            return []

    def get_total_users(self):
        if not self.connected:
            return 0
        try:
            return self.users.count_documents({})
        except Exception as e:
            logging.error(f"Error counting users: {e}")
            return 0

# ============================================
# FAST ASYNC API MANAGER
# ============================================
class PlatformAPIManager:
    @staticmethod
    async def fetch_instagram(session: aiohttp.ClientSession, url: str) -> Dict:
        try:
            async with session.get(Config.INSTAGRAM_API_URL, params={"url": url}, timeout=aiohttp.ClientTimeout(total=20)) as response:
                data = await response.json()
                
                if data.get("status") is True and data.get("result", {}).get("ins_bos"):
                    media_list = data["result"]["ins_bos"]
                    caption = data["result"].get("introduce", "No caption")
                    return {
                        "success": True,
                        "media_list": media_list,
                        "caption": caption
                    }
                return {"success": False, "error": "No media found or invalid link"}
        except asyncio.TimeoutError:
            return {"success": False, "error": "⏰ Timeout! API server took too long"}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ============================================
# UTILITY FUNCTIONS
# ============================================
class Utils:
    @staticmethod
    def format_caption(original_caption: str, bot_username: str) -> str:
        if not original_caption or original_caption.strip() == "":
            original_caption = "No caption"
        
        max_length = 800
        if len(original_caption) > max_length:
            original_caption = original_caption[:max_length - 3] + "..."
        
        return f"""<b>📥 Downloaded via @{bot_username}</b>

📝 <b>Caption:</b>
<blockquote expandable>{original_caption}</blockquote>

⚡ <b>Powered By: @VoidXDevs</b>"""

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
        if not Config.CHAT_ID:
            return True
            
        if user_id in Config.ADMIN_IDS:
            return True

        try:
            member = await context.bot.get_chat_member(
                chat_id=Config.CHAT_ID,
                user_id=user_id
            )
            return member.status in [
                ChatMember.MEMBER,
                ChatMember.ADMINISTRATOR,
                ChatMember.OWNER,
                ChatMember.RESTRICTED
            ]
        except Exception as e:
            logging.error(f"Force Sub Check Failed: {e}")
            return False

    @staticmethod
    def get_chat_button() -> InlineKeyboardMarkup:
        keyboard = []
        if Config.CHAT_LINK:
            keyboard.append([InlineKeyboardButton("📢 Join Channel/Group", url=Config.CHAT_LINK)])
        elif Config.CHAT_USERNAME:
            keyboard.append([InlineKeyboardButton("📢 Join Channel/Group", url=f"https://t.me/{Config.CHAT_USERNAME}")])
        elif Config.CHAT_ID:
            chat_id_str = str(Config.CHAT_ID)
            if chat_id_str.startswith("-100"):
                keyboard.append([InlineKeyboardButton("📢 Join Channel/Group", url=f"https://t.me/c/{chat_id_str[4:]}")])
        return InlineKeyboardMarkup(keyboard) if keyboard else None

    @staticmethod
    async def download_file_to_memory(session: aiohttp.ClientSession, url: str) -> Optional[BytesIO]:
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=45)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    bio = BytesIO(data)
                    bio.name = "media.mp4"
                    return bio
        except Exception as e:
            logging.error(f"Download to memory failed: {e}")
        return None

db = Database()

# ============================================
# BOT COMMAND HANDLERS
# ============================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type
    is_group = chat_type in ["group", "supergroup"]
    
    db.add_user(user_id=user.id)
    
    is_subscribed = True
    if not is_group and Config.CHAT_ID:
        is_subscribed = await Utils.check_subscription(context, user.id)
    
    caption = (
        f"Welcome <b>{user.full_name}</b>!\n\n"
        "I'm an Instagram Media Downloader Bot!\n\n"
        "<b>Features:</b>\n"
        "• Download Instagram Reels/Videos/Image\n"
        "• Carousel Posts Supported\n\n"
        "<b>How to use:</b>\n"
        "Just send me any Instagram URL and I'll send it for you!\n\n"
        "<b>Developed By: @VoidXDevs</b>"
    )

    buttons = []
    if not is_subscribed and not is_group:
        chat_btn = Utils.get_chat_button()
        if chat_btn:
            buttons.extend(chat_btn.inline_keyboard)
    
    buttons.append([InlineKeyboardButton("➕ Add Bot to Your Group", url=f"https://t.me/{context.bot.username}?startgroup=true")])
    reply_markup = InlineKeyboardMarkup(buttons)
    
    try:
        await update.message.reply_photo(
            photo=Config.WELCOME_IMAGE_URL,
            caption=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    except Exception:
        await update.message.reply_text(
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    total_users = db.get_total_users()
    await update.message.reply_text(f"📊 <b>Bot Statistics</b>\n\n👥 <b>Total Users:</b> <code>{total_users}</code>", parse_mode=ParseMode.HTML)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in Config.ADMIN_IDS:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    
    if not update.message.reply_to_message:
        await update.message.reply_text("❌ Reply to a message with `/broadcast` to send it to all users.")
        return
    
    users = db.get_all_users()
    total_users = len(users)
    
    if total_users == 0:
        await update.message.reply_text("❌ No users found.")
        return
    
    confirm_msg = await update.message.reply_text(f"📢 Broadcasting to {total_users} users...")
    reply_msg = update.message.reply_to_message
    success_count, failed_count = 0, 0
    
    for u in users:
        target_id = u["user_id"]
        try:
            await reply_msg.copy(chat_id=target_id)
            success_count += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed_count += 1
            db.remove_user(target_id)
    
    await confirm_msg.edit_text(
        f"✅ <b>Broadcast Completed!</b>\n\n"
        f"✅ Success: <code>{success_count}</code>\n"
        f"❌ Failed/Removed: <code>{failed_count}</code>",
        parse_mode=ParseMode.HTML
    )

# ============================================
# FAST DOWNLOAD HANDLER
# ============================================
async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_type = update.effective_chat.type
    is_group = chat_type in ["group", "supergroup"]
    text = update.message.text or update.message.caption or ""
    
    url_pattern = r'(https?://(?:www\.)?(?:instagram\.com|instagr\.am)/[^\s]+)'
    urls = re.findall(url_pattern, text)
    
    if not urls:
        return
    
    if not is_group and Config.CHAT_ID:
        is_subscribed = await Utils.check_subscription(context, user.id)
        if not is_subscribed:
            chat_btn = Utils.get_chat_button()
            await update.message.reply_text(
                "⚠️ <b>Please Join Our Channel/Group First to use this bot!</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=chat_btn
            )
            return

    db.add_user(user_id=user.id)

    async with aiohttp.ClientSession() as session:
        for url in urls:
            processing_msg = await update.message.reply_text("⏳ <b>Fetching media...</b>", parse_mode=ParseMode.HTML)
            
            try:
                result = await PlatformAPIManager.fetch_instagram(session, url)
                if not result["success"]:
                    await processing_msg.edit_text(f"❌ {result.get('error', 'Failed')}")
                    continue
                
                media_list = result["media_list"]
                formatted_caption = Utils.format_caption(result["caption"], context.bot.username)
                add_button = Utils.get_add_button(context.bot.username)
                
                if len(media_list) > 1:
                    media_group = []
                    tasks = [Utils.download_file_to_memory(session, m.get("url")) for m in media_list if m.get("url")]
                    downloaded_buffers = await asyncio.gather(*tasks)
                    
                    for idx, (media, bio) in enumerate(zip(media_list, downloaded_buffers)):
                        if not bio:
                            continue
                        
                        media_type = media.get("type", "mp4")
                        bio.seek(0)
                        
                        if media_type == "mp4":
                            media_group.append(InputMediaVideo(media=bio, caption=formatted_caption if idx == 0 else "", parse_mode=ParseMode.HTML))
                        else:
                            media_group.append(InputMediaPhoto(media=bio, caption=formatted_caption if idx == 0 else "", parse_mode=ParseMode.HTML))
                    
                    if media_group:
                        await context.bot.send_media_group(chat_id=update.effective_chat.id, media=media_group, reply_to_message_id=update.message.message_id)
                        await context.bot.send_message(chat_id=update.effective_chat.id, text="🤖 Want this bot in your group?", reply_markup=add_button)
                    await processing_msg.delete()
                else:
                    media = media_list[0]
                    bio = await Utils.download_file_to_memory(session, media.get("url"))
                    
                    if bio:
                        bio.seek(0)
                        if media.get("type") == "mp4":
                            await update.message.reply_video(video=bio, caption=formatted_caption, parse_mode=ParseMode.HTML, reply_markup=add_button)
                        else:
                            await update.message.reply_photo(photo=bio, caption=formatted_caption, parse_mode=ParseMode.HTML, reply_markup=add_button)
                        await processing_msg.delete()
                    else:
                        await processing_msg.edit_text("❌ Failed to download media content.")
            except Exception as e:
                logging.error(f"Error handling media: {e}")
                await processing_msg.edit_text("❌ Something went wrong while downloading.")

# ============================================
# ERROR HANDLER
# ============================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error(f"Exception while handling an update: {context.error}")
    if isinstance(context.error, Conflict):
        logging.warning("⚠️ Conflict error: Bot instance already running somewhere else!")
    elif isinstance(context.error, NetworkError):
        logging.warning("⚠️ Network error occurred. Retrying connection...")

# ============================================
# MAIN FUNCTION
# ============================================
def main():
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    application = Application.builder().token(Config.BOT_TOKEN).build()
    
    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("broadcast", broadcast_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_media))
    
    # Register Error Handler
    application.add_error_handler(error_handler)
    
    print("🤖 Bot Started!")
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
