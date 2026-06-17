import os
import logging
import threading
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)
from PIL import Image
from flask import Flask
import io
import asyncio

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ PDF Bot is running!", 200

@flask_app.route("/health")
def health():
    return "OK", 200

COLLECTING_PHOTOS = 1
user_photos: dict[int, list[bytes]] = {}


async def start_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user_photos[user_id] = []
    await update.message.reply_text(
        "📸 សូមផ្ញើរូបភាពរបស់អ្នក!\n\n"
        "• ផ្ញើរូបបានច្រើនតាមដែលអ្នកចង់បាន\n"
        "• នៅពេលផ្ញើរូបចប់ សូមវាយ /done ដើម្បីបំប្លែងទៅ PDF\n"
        "• វាយ /cancel ដើម្បីបោះបង់"
    )
    return COLLECTING_PHOTOS


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []
    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()
    user_photos[user_id].append(bytes(photo_bytes))
    count = len(user_photos[user_id])
    await update.message.reply_text(
        f"✅ បានទទួលរូបទី {count}\n"
        f"• ផ្ញើរូបបន្ថែម ឬ វាយ /done ដើម្បីបំប្លែងទៅ PDF"
    )
    return COLLECTING_PHOTOS


async def receive_document_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []
    doc = update.message.document
    if doc.mime_type and doc.mime_type.startswith("image/"):
        file = await context.bot.get_file(doc.file_id)
        photo_bytes = await file.download_as_bytearray()
        user_photos[user_id].append(bytes(photo_bytes))
        count = len(user_photos[user_id])
        await update.message.reply_text(
            f"✅ បានទទួលរូបទី {count} (ឯកសារ)\n"
            f"• ផ្ញើរូបបន្ថែម ឬ វាយ /done ដើម្បីបំប្លែងទៅ PDF"
        )
    else:
        await update.message.reply_text("❌ សូមផ្ញើតែរូបភាពប៉ុណ្ណោះ!")
    return COLLECTING_PHOTOS


async def convert_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in user_photos or len(user_photos[user_id]) == 0:
        await update.message.reply_text("❌ មិនទាន់មានរូបភាពទេ! សូមផ្ញើរូបភាពសិន។")
        return COLLECTING_PHOTOS

    photos = user_photos[user_id]
    count = len(photos)
    await update.message.reply_text(f"⏳ កំពុងបំប្លែង {count} រូបទៅជា PDF...")

    try:
        images = []
        for photo_bytes in photos:
            img = Image.open(io.BytesIO(photo_bytes))
            if img.mode != "RGB":
                img = img.convert("RGB")
            images.append(img)

        pdf_buffer = io.BytesIO()
        images[0].save(
            pdf_buffer,
            format="PDF",
            save_all=True,
            append_images=images[1:],
            resolution=150
        )
        pdf_buffer.seek(0)

        await update.message.reply_document(
            document=InputFile(pdf_buffer, filename="converted.pdf"),
            caption=f"✅ PDF ត្រូវបានបំប្លែងដោយជោគជ័យ!\n📄 ចំនួនទំព័រ: {count}"
        )
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ មានបញ្ហា!\nError: {str(e)}")
    finally:
        user_photos.pop(user_id, None)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user_photos.pop(user_id, None)
    await update.message.reply_text(
        "❌ បានបោះបង់!\nវាយ /pdf ម្តងទៀតដើម្បីចាប់ផ្តើមថ្មី។"
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *PDF Converter Bot*\n\n"
        "*របៀបប្រើ:*\n"
        "1️⃣ វាយ /pdf ដើម្បីចាប់ផ្តើម\n"
        "2️⃣ ផ្ញើរូបភាព (អាចផ្ញើច្រើនរូប)\n"
        "3️⃣ វាយ /done ដើម្បីបំប្លែងទៅ PDF\n\n"
        "*Commands:*\n"
        "/pdf - ចាប់ផ្តើមបំប្លែង\n"
        "/done - បំប្លែងទៅ PDF\n"
        "/cancel - បោះបង់\n"
        "/help - ជំនួយ",
        parse_mode="Markdown"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 សូមស្វាគមន៍មកកាន់ PDF Converter Bot!\n\n"
        "វាយ /pdf ដើម្បីចាប់ផ្តើមបំប្លែងរូបភាពទៅ PDF\n"
        "វាយ /help សម្រាប់ព័ត៌មានបន្ថែម"
    )


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)


async def run_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set!")

    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("pdf", start_pdf)],
        states={
            COLLECTING_PHOTOS: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.Document.IMAGE, receive_document_photo),
                CommandHandler("done", convert_to_pdf),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)

    logger.info("Bot is starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    # Keep running forever
    await asyncio.Event().wait()


def main():
    # Start Flask in background thread
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("Flask server started")

    # Run bot in main thread with its own event loop
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
