import os
import logging
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
import io
import asyncio

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# States
COLLECTING_PHOTOS = 1

# Storage: user_id -> list of images
user_photos: dict[int, list[bytes]] = {}


async def start_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /pdf command - start collecting photos"""
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
    """Receive photos from user"""
    user_id = update.effective_user.id

    if user_id not in user_photos:
        user_photos[user_id] = []

    # Get highest quality photo
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
    """Receive photos sent as documents (uncompressed)"""
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
    """Convert collected photos to PDF"""
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
            # Convert to RGB (required for PDF)
            if img.mode in ("RGBA", "P", "LA"):
                img = img.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")
            images.append(img)

        # Create PDF in memory
        pdf_buffer = io.BytesIO()
        first_image = images[0]
        rest_images = images[1:] if len(images) > 1 else []

        first_image.save(
            pdf_buffer,
            format="PDF",
            save_all=True,
            append_images=rest_images,
            resolution=150
        )
        pdf_buffer.seek(0)

        # Send PDF to user
        await update.message.reply_document(
            document=InputFile(pdf_buffer, filename="converted.pdf"),
            caption=f"✅ PDF ត្រូវបានបំប្លែងដោយជោគជ័យ!\n📄 ចំនួនទំព័រ: {count}"
        )

    except Exception as e:
        logger.error(f"Error converting to PDF: {e}")
        await update.message.reply_text(
            f"❌ មានបញ្ហាក្នុងការបំប្លែង PDF!\nError: {str(e)}"
        )
    finally:
        # Clear user's photos
        user_photos.pop(user_id, None)

    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the operation"""
    user_id = update.effective_user.id
    user_photos.pop(user_id, None)

    await update.message.reply_text(
        "❌ បានបោះបង់ការបំប្លែង PDF!\n"
        "វាយ /pdf ម្តងទៀតដើម្បីចាប់ផ្តើមថ្មី។"
    )
    return ConversationHandler.END


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command"""
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
    """Start command"""
    await update.message.reply_text(
        "👋 សូមស្វាគមន៍មកកាន់ PDF Converter Bot!\n\n"
        "វាយ /pdf ដើម្បីចាប់ផ្តើមបំប្លែងរូបភាពទៅ PDF\n"
        "វាយ /help សម្រាប់ព័ត៌មានបន្ថែម"
    )


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable not set!")

    app = Application.builder().token(token).build()

    # Conversation handler for /pdf flow
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
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
