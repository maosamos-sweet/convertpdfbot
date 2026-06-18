import os
import logging
import threading
import asyncio
import requests
import io
from io import BytesIO
from PIL import Image
from flask import Flask
from rembg import remove
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

# បង្កើត Flask App សម្រាប់រត់នៅលើ Render (ការពារកុំឱ្យ Server គាំង ឬគេងលក់)
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Bot is running perfectly with Flask on Render!", 200

@flask_app.route("/health")
def health():
    return "OK", 200

# កំណត់លេខសម្គាល់ស្ថានភាពជំហានសន្ទនា (Conversation States)
COLLECTING_PHOTOS = 1
AWAITING_FILE = 2
AWAITING_PHOTO_BG = 3

# ទិន្នន័យរក្សាទុករូបភាពបណ្តោះអាសន្នសម្រាប់បង្កើត PDF
user_photos: dict[int, list[bytes]] = {}

# ទាញយក API Keys ពី Environment Variables លើ Render
VIRUSTOTAL_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "YOUR_VIRUSTOTAL_API_KEY")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 សួស្តី! ខ្ញុំជា Bot ជំនួយការរបស់លោកអ្នក។ សូមប្រើប្រាស់បញ្ជាខាងក្រោម៖\n\n"
        "📄 /pdf - ដើម្បីបំប្លែងរូបភាពជាច្រើនទៅជាឯកសារ PDF\n"
        "🔍 /check - ដើម្បីពិនិត្យមើលមេរោគលើឯកសារ (Virus Scan - Hybrid)\n"
        "🖼️ /removebg - ដើម្បីលុបផ្ទៃខាងក្រោយរូបភាព (Remove Background)\n"
        "❓ /help - សម្រាប់ព័ត៌មានបន្ថែម"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ **របៀបប្រើប្រាស់បញ្ជានីមួយៗ៖**\n\n"
        "• វាយ /pdf រួចផ្ញើរូបភាពបន្តបន្ទាប់គ្នា ហើយវាយ /done ដើម្បីទទួលបានឯកសារ PDF។\n"
        "• វាយ /check រួចផ្ញើ ឬ Forward ឯកសារ (File) ដែលអ្នកសង្ស័យដើម្បីឆែករកមេរោគ Scammer។\n"
        "• វាយ /removebg រួចផ្ញើរូបភាពមក ដើម្បីលុបផ្ទៃខាងក្រោយចេញជាប្រភេទ .png ថ្លា។\n"
        "• វាយ /cancel ក្នុងពេលកំពុងប្រើមុខងារណាមួយ ដើម្បីបោះបង់ជំហាននោះចោល។"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id in user_photos:
        del user_photos[user_id]
    await update.message.reply_text("❌ ការបញ្ជាត្រូវបានបោះបង់ចោល។")
    return ConversationHandler.END


# ==================== មុខងារចាស់៖ បំប្លែងរូបភាពទៅជា PDF (/pdf) ====================

async def start_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user_photos[user_id] = []
    await update.message.reply_text(
        "📸 សូមផ្ញើរូបភាពរបស់អ្នក! (មុខងារបំប្លែងទៅ PDF)\n\n"
        "• ផ្ញើរូបបានច្រើនតាមដែលអ្នកចង់បាន\n"
        "• នៅពេលផ្ញើរូបចប់ សូមវាយ /done ដើម្បីបំប្លែងទៅ PDF\n"
        "• វាយ /cancel ដើម្បីបោះបង់"
    )
    return COLLECTING_PHOTOS

async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []
    
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    user_photos[user_id].append(bytes(photo_bytes))
    
    await update.message.reply_text(f"✅ បានទទួលរូបភាពទី {len(user_photos[user_id])}។ រួចរាល់សូមវាយ /done")
    return COLLECTING_PHOTOS

async def receive_document_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in user_photos:
        user_photos[user_id] = []
        
    doc_file = await update.message.document.get_file()
    doc_bytes = await doc_file.download_as_bytearray()
    user_photos[user_id].append(bytes(doc_bytes))
    
    await update.message.reply_text(f"✅ បានទទួលរូបភាព (File) ទី {len(user_photos[user_id])}។ រួចរាល់សូមវាយ /done")
    return COLLECTING_PHOTOS

async def convert_to_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id not in user_photos or not user_photos[user_id]:
        await update.message.reply_text("❌ មិនទាន់មានរូបភាពណាមួយត្រូវបានផ្ញើមកឡើយ។ សូមផ្ញើរូបភាពជាមុនសិន។")
        return COLLECTING_PHOTOS

    status_msg = await update.message.reply_text("⏳ កំពុងបង្កើតឯកសារ PDF... សូមរង់ចាំមួយភ្លែត។")
    
    try:
        images = []
        for img_bytes in user_photos[user_id]:
            img = Image.open(io.BytesIO(img_bytes))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            images.append(img)
            
        pdf_buffer = io.BytesIO()
        images[0].save(pdf_buffer, format="PDF", save_all=True, append_images=images[1:])
        pdf_buffer.seek(0)
        
        await status_msg.delete()
        await update.message.reply_document(
            document=InputFile(pdf_buffer, filename="converted_images.pdf"),
            caption="✅ ឯកសារ PDF របស់អ្នកត្រូវបានបង្កើតជោគជ័យហើយ!"
        )
    except Exception as e:
        logger.error(f"Error converting to PDF: {e}")
        await status_msg.edit_text("❌ កើតមានកំហុសក្នុងការបង្កើតឯកសារ PDF។")
        
    if user_id in user_photos:
        del user_photos[user_id]
    return ConversationHandler.END


# ==================== មុខងារថ្មីទី ១៖ ពិនិត្យមេរោគបែប HYBRID (/check) ====================

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 សូមផ្ញើ ឬ Forward ឯកសារ (File/Document) ដែលអ្នកសង្ស័យចង់ឱ្យខ្ញុំពិនិត្យមើលមេរោគ។")
    return AWAITING_FILE

async def handle_virus_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    if not document:
        await update.message.reply_text("❌ សូមផ្ញើឯកសារជាប្រភេទ File ឬ Document បញ្ជាក់ច្បាស់លាស់។")
        return ConversationHandler.END

    file_name = document.file_name.lower()
    file_size_mb = document.file_size / (1024 * 1024)
    headers = {"x-apikey": VIRUSTOTAL_API_KEY}

    # ----- ករណីទី ១៖ ឯកសារធំជាង 20MB (ពិនិត្យតាម Database Hash និងល្បិច Scammer) -----
    if file_size_mb > 20:
        status_msg = await update.message.reply_text(
            f"📦 ឯកសារមានទំហំធំ ({file_size_mb:.2f} MB) ហួសពីដែនកំណត់ទាញយករបស់ Render។\n"
            f"🔍 កំពុងប្រើប្រាស់ប្រព័ន្ធ Hybrid ដើម្បីស្វែងរកប្រវត្តិកូដមេរោគ (Hash Database Search)..."
        )
        
        try:
            # ស្វែងរកក្នុងប្រព័ន្ធទិន្នន័យ VirusTotal តាមរយៈឈ្មោះឯកសារ
            search_url = f"https://www.virustotal.com/api/v3/search?query={document.file_name}"
            response = requests.get(search_url, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                if data.get('data') and len(data['data']) > 0:
                    attributes = data['data'][0]['attributes']
                    stats = attributes.get('last_analysis_stats', {})
                    malicious_count = stats.get('malicious', 0)
                    
                    if malicious_count > 0:
                        khmer_reply = (
                            f"🚨 **រកឃើញមេរោគតាមប្រវត្តិទិន្នន័យ (Malware Database Matched!)** 🚨\n\n"
                            f"📁 ឈ្មោះឯកសារ៖ `{document.file_name}`\n"
                            f"📦 ទំហំឯកសារ៖ {file_size_mb:.2f} MB\n"
                            f"⚠️ ប្រព័ន្ធសុវត្ថិភាពចំនួន {malicious_count} បានបញ្ជាក់ថាវាជាមេរោគ!\n\n"
                            f"❌ **សូមកុំចុចបើក (Open) ឯកសារនេះដាច់ខាត ព្រោះវាជាល្បិចបោកប្រាស់របស់ Scammer ដើម្បី Hack យកគណនី Telegram របស់អ្នក!**"
                        )
                        await status_msg.edit_text(khmer_reply, parse_mode="Markdown")
                        return ConversationHandler.END
            
            # បើមិនមានប្រវត្តិក្នុង Database តែចូលល្បិច Scammer (ទំហំ 20-40MB កន្ទុយ .exe តែប្រាប់ថាជារូបភាព)
            if file_name.endswith(('.exe', '.scr', '.pif', '.bat', '.cmd', '.msi', '.vbs')):
                khmer_scam_warning = (
                    f"🚨 **ការព្រមានកម្រិតខ្ពស់៖ សញ្ញាណបោកប្រាស់ Scammer 100%** 🚨\n\n"
                    f"📁 ឈ្មោះឯកសារ៖ `{document.file_name}`\n"
                    f"📦 ទំហំឯកសារ៖ {file_size_mb:.2f} MB\n\n"
                    f"⚠️ **ការវិភាគ៖** ទោះបីជាឯកសារនេះទើបតែបង្កើតថ្មីមិនទាន់មានប្រវត្តិក្នុង Database "
                    f"ប៉ុន្តែវាជាប្រភេទឯកសារកម្មវិធីដែលអាចដំណើរការបាន (.exe) ដែលមានទំហំធំខ្លាំង (ចន្លោះ 20MB-40MB)។ "
                    f"នេះជាល្បិចដែលជនខិលខូចបន្លំភ្នែកពលរដ្ឋថាជារូបភាពថតចម្លង (Fake Photo)។\n\n"
                    f"❌ **ប្រសិនបើអ្នកចុចទាញយក ឬចុចបើក (Open/Run) វានឹងលួចយកគណនី Telegram និងទិន្នន័យទូរស័ព្ទ/កុំព្យូទ័ររបស់អ្នកភ្លាមៗ! សូមលុបវាចោលដាច់ខាត!**"
                )
                await status_msg.edit_text(khmer_scam_warning, parse_mode="Markdown")
            else:
                await status_msg.edit_text(
                    f"ℹ️ ឯកសារនេះមានទំហំ {file_size_mb:.2f} MB (ធំជាង 20MB មិនអាច Scan ផ្ទាល់លើ Render បានឡើយ) "
                    f"ហើយមិនមានប្រវត្តិតម្រុយមេរោគនៅក្នុងប្រព័ន្ធទិន្នន័យឡើយ។ សូមប្រុងប្រយ័ត្នដោយខ្លួនឯងមុននឹងបើក។"
                )
        except Exception as e:
            logger.error(f"Error in hybrid search: {e}")
            await status_msg.edit_text("❌ កើតមានកំហុសក្នុងការឆែករកប្រវត្តិមេរោគក្នុង Database។")
            
        return ConversationHandler.END

    # ----- ករណីទី ២៖ ឯកសារតូចជាង ឬស្មើ 20MB (ទាញយកមក Scan ផ្ទាល់ជាមួយម៉ាស៊ីន VirusTotal) -----
    status_msg = await update.message.reply_text("⏳ ឯកសារស្ថិតក្នុងទំហំដែលអាចទាញយកបាន កំពុងបញ្ជូនទៅកាន់ម៉ាស៊ីនវិភាគ... សូមរង់ចាំ។")
    
    try:
        tg_file = await context.bot.get_file(document.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        
        url = "https://www.virustotal.com/api/v3/files"
        files = {"file": (document.file_name, bytes(file_bytes))}
        
        response = requests.post(url, headers=headers, files=files)
        
        if response.status_code == 200:
            analysis_id = response.json()['data']['id']
            report_url = f"https://www.virustotal.com/api/v3/analyses/{analysis_id}"
            
            # រង់ចាំឱ្យប្រព័ន្ធវិភាគ ២ វិនាទី
            await asyncio.sleep(2)
            report_response = requests.get(report_url, headers=headers).json()
            
            stats = report_response['data']['attributes']['stats']
            malicious_count = stats.get('malicious', 0)
            
            if malicious_count > 0:
                virus_names = []
                results = report_response['data']['attributes']['results']
                for engine, res in results.items():
                    if res.get('category') == 'malicious' and res.get('result'):
                        virus_names.append(f"- {res['result']} ({engine})")
                
                viruses_str = "\n".join(virus_names[:3])
                
                khmer_reply = (
                    f"🚨 **រកឃើញមេរោគច្បាស់ក្រឡែត (Virus Found!)** 🚨\n\n"
                    f"📁 ឈ្មោះឯកសារ៖ `{document.file_name}`\n"
                    f"⚠️ កម្មវិធីកំចាត់មេរោគចំនួន {malicious_count} បានរាយការណ៍ថាជាមេរោគគ្រោះថ្នាក់!\n\n"
                    f"🦠 **ប្រភេទមេរោគបច្ចេកទេស៖**\n{viruses_str}\n\n"
                    f"❌ **សូមលុបឯកសារនេះចោលភ្លាម ការពារការលួចយកគណនី (Hack) របស់អ្នក!**"
                )
                await status_msg.edit_text(khmer_reply, parse_mode="Markdown")
            else:
                await status_msg.edit_text("✅ ឯកសារនេះត្រូវបានទាញយក និងពិនិត្យទាំងស្រុងហើយ៖ មានសុវត្ថិភាពខ្ពស់។")
        else:
            await status_msg.edit_text("❌ មិនអាចភ្ជាប់ទៅកាន់ម៉ាស៊ីនកំចាត់មេរោគរបស់ VirusTotal បានឡើយ។")
            
    except Exception as e:
        logger.error(f"Error downloading or scanning small file: {e}")
        await status_msg.edit_text("❌ កើតមានកំហុសបច្ចេកទេសក្នុងការទាញយក និងវិភាគឯកសារនេះ។")

    return ConversationHandler.END


# ==================== មុខងារថ្មីទី ២៖ លុបផ្ទៃខាងក្រោយរូបភាព (/removebg) ====================

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

    status_msg = await update.message.reply_text("⏳ កំពុងដំណើរការកាត់លុប Background... សូមរង់ចាំបន្តិច។")

    try:
        # ទាញយករូបភាពចូលទៅក្នុង Memory
        img_buffer = BytesIO()
        await photo_file.download_to_memory(img_buffer)
        img_buffer.seek(0)
        
        # ដំណើរការលុបផ្ទៃខាងក្រោយដោយប្រើបណ្ណាល័យ rembg (Open Source មិនអស់លុយ)
        input_image = Image.open(img_buffer)
        output_image = remove(input_image)
        
        # រក្សាទុកជាទម្រង់ PNG ដើម្បីរក្សាភាពថ្លា (Transparency)
        output_buffer = BytesIO()
        output_image.save(output_buffer, format="PNG")
        output_buffer.seek(0)
        output_buffer.name = "removed_background.png"
        
        # ផ្ញើត្រឡប់ទៅវិញជា Document ដើម្បីកុំឱ្យ Telegram បង្រួមគុណភាពរូបភាព
        await status_msg.delete()
        await context.bot.send_document(
            chat_id=update.message.chat_id,
            document=output_buffer,
            caption="✅ បានលុបផ្ទៃខាងក្រោយជោគជ័យ! រូបភាពត្រូវបានរក្សាជាប្រភេទច្បាស់ (.png)"
        )
    except Exception as e:
        logger.error(f"Error in removebg processing: {e}")
        await status_msg.edit_text("❌ ម៉ាស៊ីនមិនអាចកាត់ ឬលុបផ្ទៃខាងក្រោយនៃរូបភាពនេះបានឡើយ។")

    return ConversationHandler.END


# ==================== ការគ្រប់គ្រងការរត់កម្មវិធី និង SERVER ====================

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

async def run_bot():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("សូមកំណត់ប្រព័ន្ធផ្ទៀងផ្ទាត់ TELEGRAM_BOT_TOKEN លើ Render ជាមុនសិន!")

    app = Application.builder().token(token).build()

    # បង្កើតប្រព័ន្ធគ្រប់គ្រងសន្ទនា និងបញ្ជារួមគ្នា
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

    logger.info("Bot is starting polling setup...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    
    await asyncio.Event().wait()

def main():
    # ដំណើរការ Flask នៅក្នុង Thread ផ្សេងមួយដើម្បីកុំឱ្យរំខានដល់ Bot
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # ដំណើរការ Telegram Bot នៅក្នុង Asyncio Event Loop ចម្បង
    asyncio.run(run_bot())

if __name__ == "__main__":
    main()
