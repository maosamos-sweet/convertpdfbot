import os
import logging
import threading
import asyncio
import requests
import io
import json
from io import BytesIO
from datetime import datetime, timezone
from collections import defaultdict
from flask import Flask, request, abort
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

# ==================== STATS TRACKING ====================
stats = {
    "pdf_converted": 0,
    "virus_checked": 0,
    "removebg_done": 0,
    "total_users": set(),
    "recent_activity": [],   # list of dicts
    "virus_threats_found": 0,
    "bot_start_time": datetime.now(timezone.utc).isoformat(),
}
stats_lock = threading.Lock()

def record_activity(action: str, user_id: int, detail: str = ""):
    with stats_lock:
        stats["total_users"].add(user_id)
        entry = {
            "time": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "action": action,
            "user": f"#{user_id}",
            "detail": detail,
        }
        stats["recent_activity"].insert(0, entry)
        stats["recent_activity"] = stats["recent_activity"][:20]  # keep last 20

# ==================== DASHBOARD HTML ====================
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>⚡ QuickTools KH — Admin</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400;600&display=swap');

  :root {
    --bg:       #0a0a0f;
    --surface:  #13131a;
    --border:   #1e1e2e;
    --accent:   #f5c518;
    --accent2:  #ff6b35;
    --green:    #22c55e;
    --red:      #ef4444;
    --blue:     #3b82f6;
    --text:     #e2e8f0;
    --muted:    #64748b;
    --radius:   12px;
  }

  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Space Grotesk', sans-serif;
    min-height: 100vh;
  }

  /* TOP NAV */
  nav {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 32px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    position: sticky; top: 0; z-index: 10;
  }
  .logo { display: flex; align-items: center; gap: 10px; }
  .logo-bolt { font-size: 22px; }
  .logo-text { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; }
  .logo-text span { color: var(--accent); }
  .live-badge {
    display: flex; align-items: center; gap: 6px;
    background: rgba(34,197,94,0.12);
    border: 1px solid rgba(34,197,94,0.3);
    border-radius: 99px;
    padding: 5px 12px;
    font-size: 12px; font-weight: 600; color: var(--green);
  }
  .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--green);
    animation: pulse 2s infinite;
  }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.4} }

  /* MAIN LAYOUT */
  main { max-width: 1100px; margin: 0 auto; padding: 32px 24px; }

  /* UPTIME BAR */
  .uptime-bar {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 14px 20px;
    margin-bottom: 28px;
    display: flex; align-items: center; gap: 12px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 13px; color: var(--muted);
  }
  .uptime-bar strong { color: var(--accent); }

  /* STAT CARDS */
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 16px;
    margin-bottom: 32px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 22px 24px;
    position: relative;
    overflow: hidden;
    transition: border-color .2s;
  }
  .card:hover { border-color: var(--accent); }
  .card::before {
    content: '';
    position: absolute; top: 0; left: 0; right: 0; height: 3px;
    background: var(--card-color, var(--accent));
  }
  .card-icon { font-size: 28px; margin-bottom: 12px; }
  .card-value {
    font-size: 38px; font-weight: 700;
    font-family: 'JetBrains Mono', monospace;
    line-height: 1;
    margin-bottom: 4px;
  }
  .card-label { font-size: 13px; color: var(--muted); font-weight: 500; }
  .card-sub { font-size: 11px; color: var(--muted); margin-top: 8px; }

  .card-pdf   { --card-color: var(--blue); }
  .card-virus { --card-color: var(--red); }
  .card-bg    { --card-color: var(--green); }
  .card-users { --card-color: var(--accent); }
  .card-threat{ --card-color: var(--accent2); }

  /* SECTION TITLE */
  .section-title {
    font-size: 13px; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .section-title::after {
    content: ''; flex: 1; height: 1px; background: var(--border);
  }

  /* ACTIVITY TABLE */
  .table-wrap {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    margin-bottom: 32px;
  }
  table { width: 100%; border-collapse: collapse; }
  th {
    background: var(--bg);
    padding: 12px 16px;
    text-align: left;
    font-size: 11px; font-weight: 700;
    letter-spacing: .07em; text-transform: uppercase;
    color: var(--muted);
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 12px 16px;
    font-size: 13px;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(255,255,255,0.02); }

  .badge {
    display: inline-block;
    padding: 3px 9px; border-radius: 99px;
    font-size: 11px; font-weight: 600;
    font-family: 'JetBrains Mono', monospace;
  }
  .badge-pdf   { background:rgba(59,130,246,.15); color:#60a5fa; }
  .badge-virus { background:rgba(239,68,68,.15);  color:#f87171; }
  .badge-bg    { background:rgba(34,197,94,.15);  color:#4ade80; }

  .user-chip {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px; color: var(--muted);
  }
  .time-chip {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px; color: var(--muted);
  }

  .empty-row td {
    text-align: center; padding: 32px;
    color: var(--muted); font-size: 13px;
  }

  /* REFRESH BTN */
  .btn-refresh {
    display: inline-flex; align-items: center; gap: 6px;
    background: var(--accent); color: #000;
    border: none; border-radius: 8px;
    padding: 8px 16px; font-size: 13px; font-weight: 700;
    cursor: pointer; font-family: inherit;
    transition: opacity .15s;
  }
  .btn-refresh:hover { opacity: .85; }

  .top-actions {
    display: flex; justify-content: flex-end; margin-bottom: 20px;
  }

  /* FOOTER */
  footer {
    text-align: center; padding: 24px;
    font-size: 12px; color: var(--muted);
    border-top: 1px solid var(--border);
  }
</style>
</head>
<body>

<nav>
  <div class="logo">
    <span class="logo-bolt">⚡</span>
    <span class="logo-text">QuickTools <span>KH</span> — Admin</span>
  </div>
  <div class="live-badge"><div class="dot"></div> Bot is Live</div>
</nav>

<main>
  <div class="uptime-bar">
    🕐 Server started: <strong>{{START_TIME}}</strong>
    &nbsp;·&nbsp; Last refresh: <strong id="last-refresh">—</strong>
  </div>

  <div class="cards">
    <div class="card card-pdf">
      <div class="card-icon">📄</div>
      <div class="card-value">{{PDF_COUNT}}</div>
      <div class="card-label">PDF Converted</div>
      <div class="card-sub">Total since startup</div>
    </div>
    <div class="card card-virus">
      <div class="card-icon">🔍</div>
      <div class="card-value">{{VIRUS_COUNT}}</div>
      <div class="card-label">Virus Scanned</div>
      <div class="card-sub">{{THREAT_COUNT}} threats found</div>
    </div>
    <div class="card card-bg">
      <div class="card-icon">🖼️</div>
      <div class="card-value">{{BG_COUNT}}</div>
      <div class="card-label">Background Removed</div>
      <div class="card-sub">via remove.bg API</div>
    </div>
    <div class="card card-users">
      <div class="card-icon">👥</div>
      <div class="card-value">{{USER_COUNT}}</div>
      <div class="card-label">Unique Users</div>
      <div class="card-sub">Since last restart</div>
    </div>
    <div class="card card-threat">
      <div class="card-icon">⚡</div>
      <div class="card-value">{{TOTAL_ACTIONS}}</div>
      <div class="card-label">Total Actions</div>
      <div class="card-sub">All commands combined</div>
    </div>
  </div>

  <div class="top-actions">
    <button class="btn-refresh" onclick="location.reload()">↻ Refresh</button>
  </div>

  <div class="section-title">Recent Activity</div>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>Time</th>
          <th>Action</th>
          <th>User ID</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {{ACTIVITY_ROWS}}
      </tbody>
    </table>
  </div>
</main>

<footer>⚡ QuickTools KH Bot Dashboard · Built with Flask on Render</footer>

<script>
  document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
  // Auto refresh every 30s
  setTimeout(() => location.reload(), 30000);
</script>
</body>
</html>"""

def build_dashboard():
    with stats_lock:
        pdf    = stats["pdf_converted"]
        virus  = stats["virus_checked"]
        bg     = stats["removebg_done"]
        users  = len(stats["total_users"])
        threats= stats["virus_threats_found"]
        total  = pdf + virus + bg
        start  = stats["bot_start_time"][:19].replace("T", " ") + " UTC"
        rows   = stats["recent_activity"]

    badge_map = {
        "PDF": "badge-pdf",
        "VIRUS": "badge-virus",
        "REMOVEBG": "badge-bg",
    }

    if rows:
        row_html = ""
        for r in rows:
            bc = badge_map.get(r["action"], "badge-pdf")
            row_html += f"""<tr>
              <td><span class="time-chip">{r['time']}</span></td>
              <td><span class="badge {bc}">{r['action']}</span></td>
              <td><span class="user-chip">{r['user']}</span></td>
              <td>{r['detail']}</td>
            </tr>"""
    else:
        row_html = '<tr class="empty-row"><td colspan="4">No activity yet — waiting for users 👀</td></tr>'

    html = DASHBOARD_HTML
    html = html.replace("{{PDF_COUNT}}", str(pdf))
    html = html.replace("{{VIRUS_COUNT}}", str(virus))
    html = html.replace("{{BG_COUNT}}", str(bg))
    html = html.replace("{{USER_COUNT}}", str(users))
    html = html.replace("{{THREAT_COUNT}}", str(threats))
    html = html.replace("{{TOTAL_ACTIONS}}", str(total))
    html = html.replace("{{START_TIME}}", start)
    html = html.replace("{{ACTIVITY_ROWS}}", row_html)
    return html

# ==================== FLASK ROUTES ====================
@flask_app.route("/")
def dashboard():
    ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
    if ADMIN_KEY:
        provided = request.args.get("key", "")
        if provided != ADMIN_KEY:
            abort(403)
    return build_dashboard(), 200, {"Content-Type": "text/html; charset=utf-8"}

@flask_app.route("/health")
def health():
    return "OK", 200

@flask_app.route("/api/stats")
def api_stats():
    ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
    if ADMIN_KEY and request.args.get("key", "") != ADMIN_KEY:
        abort(403)
    with stats_lock:
        return {
            "pdf_converted": stats["pdf_converted"],
            "virus_checked": stats["virus_checked"],
            "removebg_done": stats["removebg_done"],
            "total_users": len(stats["total_users"]),
            "virus_threats_found": stats["virus_threats_found"],
            "total_actions": stats["pdf_converted"] + stats["virus_checked"] + stats["removebg_done"],
        }

# ==================== CONVERSATION STATES ====================
COLLECTING_PHOTOS = 1
AWAITING_FILE = 2
AWAITING_PHOTO_BG = 3

user_photos: dict[int, list[bytes]] = {}
VIRUSTOTAL_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "")

# ==================== BOT HANDLERS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    record_activity("START", update.effective_user.id, "User started bot")
    await update.message.reply_text(
        "👋 សូមស្វាគមន៍មកកាន់ ⚡ QuickTools KH!\n\n"
        "📄 /pdf - បំប្លែងរូបភាពទៅជា PDF\n"
        "🔍 /check - ពិនិត្យមើលមេរោគ\n"
        "🖼️ /removebg - លុបផ្ទៃខាងក្រោយរូបភាព\n"
        "❓ /help - ព័ត៌មានបន្ថែម"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *ជំនួយការបច្ចេកទេស*\n\n"
        "*របៀបប្រើប្រាស់:*\n"
        "1️⃣ វាយ /pdf រួចផ្ញើរូបភាពបន្តបន្ទាប់គ្នា និងវាយ /done ដើម្បីបង្កើត PDF\n"
        "2️⃣ វាយ /check រួចផ្ញើឯកសារដែលសង្ស័យ (គាំទ្រដល់ 40MB)\n"
        "3️⃣ វាយ /removebg រួចផ្ញើរូបភាពមក ដើម្បីលុបផ្ទៃខាងក្រោយ\n"
        "4️⃣ វាយ /cancel ដើម្បីបោះបង់ជំហានបច្ចុប្បន្ន",
        parse_mode="Markdown"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user_photos.pop(user_id, None)
    await update.message.reply_text("❌ បានបោះបង់ជំហានសន្ទនារួចរាល់។")
    return ConversationHandler.END

# ==================== PDF ====================
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
        images[0].save(pdf_buffer, format="PDF", save_all=True, append_images=images[1:], resolution=150)
        pdf_buffer.seek(0)

        await status_msg.delete()
        await update.message.reply_document(
            document=InputFile(pdf_buffer, filename="converted.pdf"),
            caption=f"✅ PDF ត្រូវបានបំប្លែងដោយជោគជ័យ!\n📄 ចំនួនទំព័រ: {count}"
        )
        with stats_lock:
            stats["pdf_converted"] += 1
        record_activity("PDF", user_id, f"{count} page(s)")
    except Exception as e:
        logger.error(f"Error: {e}")
        await update.message.reply_text(f"❌ មានបញ្ហា!\nError: {str(e)}")
    finally:
        user_photos.pop(user_id, None)

    return ConversationHandler.END

# ==================== VIRUS CHECK ====================
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 សូមផ្ញើ ឬ Forward ឯកសារ (File/Document) ដើម្បីឱ្យខ្ញុំពិនិត្យ។")
    return AWAITING_FILE

async def handle_virus_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    user_id = update.effective_user.id
    if not document:
        await update.message.reply_text("❌ សូមផ្ញើជាប្រភេទឯកសារ (Document File)។")
        return ConversationHandler.END

    file_name = document.file_name.lower()
    file_size_mb = document.file_size / (1024 * 1024)
    headers = {"x-apikey": VIRUSTOTAL_API_KEY}

    if file_size_mb > 20:
        status_msg = await update.message.reply_text(
            f"📦 ឯកសារមានទំហំធំ ({file_size_mb:.2f} MB)...\n"
            f"🔍 កំពុងឆែករកប្រវត្តិក្នុង Database..."
        )
        try:
            search_url = f"https://www.virustotal.com/api/v3/search?query={document.file_name}"
            response = requests.get(search_url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                if data.get('data') and len(data['data']) > 0:
                    s = data['data'][0]['attributes'].get('last_analysis_stats', {})
                    malicious = s.get('malicious', 0)
                    if malicious > 0:
                        with stats_lock:
                            stats["virus_threats_found"] += 1
                        await status_msg.edit_text(f"🚨 **រកឃើញមេរោគ!** ប្រព័ន្ធ {malicious} បានបញ្ជាក់ថាជាមេរោគ!")
                        record_activity("VIRUS", user_id, f"THREAT: {document.file_name}")
                        return ConversationHandler.END

            if file_name.endswith(('.exe', '.scr', '.pif', '.bat', '.cmd', '.msi', '.vbs')):
                with stats_lock:
                    stats["virus_threats_found"] += 1
                await status_msg.edit_text(f"🚨 **ការព្រមាន!** ឯកសារ .exe ទំហំធំ — សញ្ញាណបោកប្រាស់!")
                record_activity("VIRUS", user_id, f"SUSPICIOUS: {document.file_name}")
            else:
                await status_msg.edit_text(f"ℹ️ ឯកសារ {file_size_mb:.2f}MB — គ្មានប្រវត្តិអាក្រក់ក្នុង DB។ ប្រុងប្រយ័ត្ន!")
                record_activity("VIRUS", user_id, f"CLEAN: {document.file_name}")
        except Exception:
            await status_msg.edit_text("❌ មានបញ្ហាក្នុងការឆែក Database។")
        with stats_lock:
            stats["virus_checked"] += 1
        return ConversationHandler.END

    status_msg = await update.message.reply_text("⏳ កំពុងទាញយកឯកសារ... សូមរង់ចាំ។")
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
                with stats_lock:
                    stats["virus_threats_found"] += 1
                await status_msg.edit_text(f"🚨 **រកឃើញមេរោគ!** ប្រព័ន្ធ {malicious} បានរាយការណ៍!")
                record_activity("VIRUS", user_id, f"THREAT: {document.file_name}")
            else:
                await status_msg.edit_text("✅ ឯកសារមានសុវត្ថិភាព!")
                record_activity("VIRUS", user_id, f"CLEAN: {document.file_name}")
        else:
            await status_msg.edit_text("❌ មិនអាចភ្ជាប់ VirusTotal បានទេ។")
    except Exception as e:
        await status_msg.edit_text("❌ កើតមានកំហុសក្នុងការវិភាគ។")

    with stats_lock:
        stats["virus_checked"] += 1
    return ConversationHandler.END

# ==================== REMOVE BG ====================
async def removebg_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🖼️ សូមផ្ញើរូបភាពដែលអ្នកចង់លុបផ្ទៃខាងក្រោយ។")
    return AWAITING_PHOTO_BG

async def handle_removebg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if update.message.photo:
        photo_file = await update.message.photo[-1].get_file()
    elif update.message.document and update.message.document.mime_type.startswith("image/"):
        photo_file = await update.message.document.get_file()
    else:
        await update.message.reply_text("❌ សូមផ្ញើតែឯកសារប្រភេទរូបភាពប៉ុណ្ណោះ។")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("⏳ កំពុងកាត់ Background តាម AI... សូមរង់ចាំ។")

    try:
        img_bytes = await photo_file.download_as_bytearray()

        REMOVEBG_API_KEY = os.environ.get("REMOVEBG_API_KEY", "")
        if not REMOVEBG_API_KEY:
            await status_msg.edit_text("❌ គ្មាន REMOVEBG_API_KEY ក្នុង Render Environment Variables!")
            return ConversationHandler.END

        response = requests.post(
            "https://api.remove.bg/v1.0/removebg",
            files={"image_file": ("photo.jpg", bytes(img_bytes), "image/jpeg")},
            data={"size": "auto"},
            headers={"X-Api-Key": REMOVEBG_API_KEY},
            timeout=30
        )

        if response.status_code == 200:
            output_buffer = BytesIO(response.content)
            output_buffer.seek(0)
            await status_msg.delete()
            await context.bot.send_document(
                chat_id=update.message.chat_id,
                document=InputFile(output_buffer, filename="removed_bg.png"),
                caption="✅ លុបផ្ទៃខាងក្រោយជោគជ័យ!"
            )
            with stats_lock:
                stats["removebg_done"] += 1
            record_activity("REMOVEBG", user_id, "Success")
        else:
            err = response.json().get("errors", [{}])[0].get("title", response.text[:100])
            await status_msg.edit_text(f"❌ remove.bg Error: {err}")
            record_activity("REMOVEBG", user_id, f"Failed: {err}")

    except Exception as e:
        logger.error(f"Error in removebg: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ មានបញ្ហា៖ {str(e)[:300]}")

    return ConversationHandler.END

# ==================== SERVER ====================
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
