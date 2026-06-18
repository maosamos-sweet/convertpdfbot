import os
import logging
import threading
import asyncio
import requests
import urllib.request
import io
from io import BytesIO
from pathlib import Path
from PIL import Image
from flask import Flask

# ទាញយក rembg model ជាមួយ Progress បង្ហាញទៅ Telegram
MODEL_PATH = Path.home() / ".u2net" / "u2net.onnx"
MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx"
MODEL_SIZE_MB = 176

_download_progress_callback = None

def download_model_with_progress():
    """ទាញយក model ជាមួយ Progress Callback"""
    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    if MODEL_PATH.exists():
        return  # មានហើយ មិនចាំបាច់ Download ទៀតទេ

    downloaded = [0]
    def reporthook(count, block_size, total_size):
        downloaded[0] += block_size
        if total_size > 0 and _download_progress_callback:
            pct = min(int(downloaded[0] * 100 / total_size), 100)
            _download_progress_callback(pct)

    urllib.request.urlretrieve(MODEL_URL, MODEL_PATH, reporthook=reporthook)

_rembg_ready = False
def get_rembg():
    global _rembg_ready
    if not _rembg_ready:
        from rembg import remove  # noqa: trigger model load from cache
        _rembg_ready = True
    from rembg import remove
    return remove
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# កំណត់ការបង្ហាញទិន្នន័យ Log របស់ប្រព័ន្ធ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# បង្កើត Flask App សម្រាប់រត់នៅលើ Render
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ PDF Bot is running perfectly with Flask on Render!", 200

@flask_app.route("/health")
def health():
    return "OK", 200

# កំណត់លេខសម្គាល់ស្ថានភាពជំហានសន្ទនា (Conversation States)
COLLECTING_PHOTOS = 1
AWAITING_FILE = 2
AWAITING_PHOTO_BG = 3

# ផ្ទុកទិន្នន័យរូបភាពបណ្តោះអាសន្នសម្រាប់បង្កើត PDF
user_photos: dict[int, list[bytes]] = {}

# ទាញយក API Keys ពី Environment Variables លើ Render
VIRUSTOTAL_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 សូមស្វាគមន៍មកកាន់ជំនួយការឆ្លាតវៃ!\n\n"
        "📄 /pdf - ដើម្បីបំប្លែងរូបភាពទៅជាឯកសារ PDF\n"
        "🔍 /check - ដើម្បីពិនិត្យមើលមេរោគលើឯកសារ (Virus Scan - Hybrid)\n"
        "🖼️ /removebg - ដើម្បីលុបផ្ទៃខាងក្រោយរូបភាព (Hugging Face Free 100%)\n"
        "❓ /help - សម្រាប់ព័ត៌មានបន្ថែម"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *ជំនួយការបច្ចេកទេស*\n\n"
        "*របៀបប្រើប្រាស់:*\n"
        "1️⃣ វាយ /pdf រួចផ្ញើរូបភាពបន្តបន្ទាប់គ្នា និងវាយ /done ដើម្បីបង្កើត PDF\n"
        "2️⃣ វាយ /check រួចផ្ញើឯកសារដែលសង្ស័យ (គាំទ្រដល់ឯកសារ Scammer 40MB)\n"
        "3️⃣ វាយ /removebg រួចផ្ញើរូបភាពមក ដើម្បីលុបផ្ទៃខាងក្រោយចេញជាប្រភេទ .png\n"
        "4️⃣ វាយ /cancel ដើម្បីបោះបង់ជំហានបច្ចុប្បន្ន",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user_photos.pop(user_id, None)
    await update.message.reply_text("❌ បានបោះបង់ជំហានសន្ទនារួចរាល់។")
    return ConversationHandler.END


# ==================== មុខងារទី ១៖ បំប្លែងរូបភាពទៅជា PDF (/pdf) ====================

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
    status_msg = await update.message.reply_text(f"⏳ កំពុងបំប្លែង {count} រូបទៅជា PDF...")

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

        await status_msg.delete()
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


# ==================== មុខងារទី ២៖ ពិនិត្យមេរោគបែប HYBRID (/check) ====================

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 សូមផ្ញើ ឬ Forward ឯកសារ (File/Document) ដើម្បីឱ្យខ្ញុំពិនិត្យរកល្បិចបោកប្រាស់ ឬមេរោគ។")
    return AWAITING_FILE

async def handle_virus_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ សូមផ្ញើជាប្រភេទឯកសារ (Document File)។")
        return ConversationHandler.END

    file_name = document.file_name.lower()
    file_size_mb = document.file_size / (1024 * 1024)
    headers = {"x-apikey": VIRUSTOTAL_API_KEY}

    # ករណីឯកសារធំជាង 20MB (ល្បិច Scammer 40MB)
    if file_size_mb > 20:
        status_msg = await update.message.reply_text(
            f"📦 ឯកសារមានទំហំធំ ({file_size_mb:.2f} MB) លើសពីដែនកំណត់ទាញយករបស់ Render។\n"
            f"🔍 កំពុងឆែករកប្រវត្តិកូដមេរោគតាមរយៈប្រព័ន្ធ Hybrid Database..."
        )
        try:
            search_url = f"https://www.virustotal.com/api/v3/search?query={document.file_name}"
            response = requests.get(search_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                if data.get('data') and len(data['data']) > 0:
                    stats = data['data'][0]['attributes'].get('last_analysis_stats', {})
                    malicious = stats.get('malicious', 0)
                    if malicious > 0:
                        await status_msg.edit_text(
                            f"🚨 **រកឃើញមេរោគក្នុង Database!** 🚨\n\n"
                            f"📁 ឈ្មោះឯកសារ៖ `{document.file_name}`\n"
                            f"⚠️ ប្រព័ន្ធសុវត្ថិភាពចំនួន {malicious} បានបញ្ជាក់ថាជាមេរោគលួចទិន្នន័យ។ ហាមបើកដាច់ខាត!"
                        )
                        return ConversationHandler.END

            if file_name.endswith(('.exe', '.scr', '.pif', '.bat', '.cmd', '.msi', '.vbs')):
                await status_msg.edit_text(
                    f"🚨 **ការព្រមានកម្រិតខ្ពស់៖ សញ្ញាណបោកប្រាស់ Scammer 100%** 🚨\n\n"
                    f"📁 ឈ្មោះ៖ `{document.file_name}` ({file_size_mb:.2f} MB)\n"
                    f"⚠️ វាជាប្រភេទឯកសារកម្មវិធីដែលអាចដំណើរការបាន (.exe) ដែលមានទំហំធំខុសធម្មតា។ "
                    f"នេះជាល្បិចបន្លំថាជារូបភាពដើម្បីកុំឱ្យ Render ទាញយកកើត។ សូមលុបវាចោលភ្លាម ការពារការបាត់បង់គណនី Telegram!"
                )
            else:
                await status_msg.edit_text(f"ℹ️ ឯកសារទំហំ {file_size_mb:.2f} MB មិនមានប្រវត្តិអាក្រក់ក្នុង Database ឡើយ។ សូមប្រុងប្រយ័ត្ន។")
        except Exception as e:
            await status_msg.edit_text("❌ មានបញ្ហាក្នុងការឆែកប្រព័ន្ធទិន្នន័យ Database។")
        return ConversationHandler.END

    # ករណីឯកសារតូចជាង 20MB (Scan ផ្ទាល់)
    status_msg = await update.message.reply_text("⏳ កំពុងទាញយកឯកសារទៅកាន់ម៉ាស៊ីនវិភាគ... សូមរង់ចាំ។")
    try:
        tg_file = await context.bot.get_file(document.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        url = "https://www.virustotal.com/api/v3/files"
        response = requests.post(url, headers=headers, files={"file": (document.file_name, bytes(file_bytes))})
        
        if response.status_code == 200:
            analysis_id = response.json()['data']['id']
            await asyncio.sleep(2)
            report = requests.get(f"https://www.virustotal.com/api/v3/analyses/{analysis_id}", headers=headers).json()
            malicious = report['data']['attributes']['stats'].get('malicious', 0)
            
            if malicious > 0:
                await status_msg.edit_text(f"🚨 **រកឃើញមេរោគគ្រោះថ្នាក់!** មានប្រព័ន្ធកំចាត់មេរោគចំនួន {malicious} បានរាយការណ៍ថាវាជាឯកសារមិនមានសុវត្ថិភាព។")
            else:
                await status_msg.edit_text("✅ ឯកសារនេះត្រូវបានពិនិត្យទាំងស្រុងហើយ៖ មានសុវត្ថិភាពខ្ពស់។")
        else:
            await status_msg.edit_text("❌ មិនអាចភ្ជាប់ទៅកាន់ម៉ាស៊ីន VirusTotal បានឡើយ BOUND_ERROR។")
    except Exception as e:
        await status_msg.edit_text("❌ កើតមានកំហុសក្នុងការវិភាគឯកសារ។")
    return ConversationHandler.END


# ==================== មុខងារទី ៣៖ លុបផ្ទៃខាងក្រោយរូបភាព (/removebg តាម HUGGING FACE FREE) ====================

async def removebg_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🖼️ សូមផ្ញើរូបភាពដែលអ្នកចង់លុបផ្ទៃខាងក្រោយ (Background) មកកាន់ខ្ញុំ។")
    return AWAITING_PHOTO_BG

async def handle_removebg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        photo_file = await update.message.document.get_file()
    else:
        await update.message.reply_text("❌ សូមផ្ញើតែឯកសារប្រភេទរូបភាពប៉ុណ្ណោះ។")
        return ConversationHandler.END

    # ពិនិត្យថាតើ model មានហើយឬអត់
    if not MODEL_PATH.exists():
        status_msg = await update.message.reply_text(
            f"⬇️ កំពុង Download AI Model (~{MODEL_SIZE_MB}MB) លើកដំបូង...\n"
            f"[░░░░░░░░░░] 0%"
        )
    else:
        status_msg = await update.message.reply_text("⏳ កំពុងដំណើរការ AI លុបផ្ទៃខាងក្រោយ... សូមរង់ចាំ។")

    try:
        # ទាញយករូបភាពទៅក្នុង Memory ជាប្រភេទ Bytes
        img_bytes = await photo_file.download_as_bytearray()

        # បើ model មិនទាន់មាន → Download ជាមួយ Progress
        if not MODEL_PATH.exists():
            global _download_progress_callback
            loop = asyncio.get_event_loop()
            last_pct = [-1]

            def on_progress(pct):
                if pct - last_pct[0] >= 10:  # Update រៀងរាល់ 10%
                    last_pct[0] = pct
                    filled = pct // 10
                    bar = "█" * filled + "░" * (10 - filled)
                    asyncio.run_coroutine_threadsafe(
                        status_msg.edit_text(
                            f"⬇️ កំពុង Download AI Model (~{MODEL_SIZE_MB}MB)...\n"
                            f"[{bar}] {pct}%"
                        ),
                        loop
                    )

            _download_progress_callback = on_progress
            await loop.run_in_executor(None, download_model_with_progress)
            _download_progress_callback = None

            await status_msg.edit_text("✅ Download រួចរាល់! កំពុងកាត់ Background...")

        # ដំណើរការ rembg
        loop = asyncio.get_event_loop()
        output_bytes = await loop.run_in_executor(
            None, get_rembg(), bytes(img_bytes)
        )

        output_buffer = BytesIO(output_bytes)
        output_buffer.seek(0)

        await status_msg.delete()
        await context.bot.send_document(
            chat_id=update.message.chat_id,
            document=InputFile(output_buffer, filename="removed_bg.png"),
            caption="✅ លុបផ្ទៃខាងក្រោយជោគជ័យ! (ឥតគិតថ្លៃ ១០០%)"
        )

    except Exception as e:
        logger.error(f"Error in removebg: {e}")
        await status_msg.edit_text("❌ ម៉ាស៊ីនមានបញ្ហាបច្ចេកទេសក្នុងការកាត់រូបភាព។")
        
    return ConversationHandler.END


# ==================== ការគ្រប់គ្រងការរត់កម្មវិធី និង SERVER ====================

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

async def run_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN not set!")

    app = Application.builder().token(token).build()

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("pdf", start_pdf),
            CommandHandler("check", check_command),
            CommandHandler("removebg", removebg_command)
        ],
        states={
            COLLECTING_PHOTOS: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.Document.IMAGE, receive_document_photo),
                CommandHandler("done", convert_to_pdf),
            ],
            AWAITING_FILE: [
                MessageHandler(filters.Document.ALL, handle_virus_check)
            ],
            AWAITING_PHOTO_BG: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_removebg)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(conv_handler)

    logger.info("Bot is starting setup...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

def main():
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
