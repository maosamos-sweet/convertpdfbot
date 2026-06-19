import os
import logging
import threading
import asyncio
import requests
import io
import json
import base64
import re
from io import BytesIO
from datetime import datetime, timezone
from collections import defaultdict

# Flask
from flask import Flask, request, abort

# Telegram
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
    ConversationHandler,
)

# PIL for PDF conversion
from PIL import Image

# MongoDB
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError

# ==================== LOGGING ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== ENVIRONMENT VARIABLES ====================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
VIRUSTOTAL_API_KEY  = os.environ.get("VIRUSTOTAL_API_KEY", "")
REMOVEBG_API_KEY    = os.environ.get("REMOVEBG_API_KEY", "")
GEMINI_API_KEY      = os.environ.get("GEMINI_API_KEY", "")
MONGODB_URI         = os.environ.get("MONGODB_URI", "")
PORT                = int(os.environ.get("PORT", 10000))
ADMIN_KEY           = os.environ.get("ADMIN_KEY", "")

# ==================== MONGODB SETUP ====================
# All statistics and user data are stored in MongoDB Atlas.
# Collections: users, activities, stats
# Falls back to in-memory if MONGODB_URI is not set (for local dev).

mongo_client = None
db = None

def init_mongo():
    """Initialize MongoDB Atlas connection and ensure indexes exist."""
    global mongo_client, db
    if not MONGODB_URI:
        logger.warning("MONGODB_URI not set — stats will be in-memory only (not persisted).")
        return
    try:
        mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        # Ping to confirm connection
        mongo_client.admin.command("ping")
        db = mongo_client["quicktools"]

        # Create unique index on user_id to prevent duplicate users
        db["users"].create_index("user_id", unique=True)
        # Index activities by timestamp descending for fast recent-activity queries
        db["activities"].create_index([("timestamp", DESCENDING)])

        logger.info("✅ Connected to MongoDB Atlas.")
    except PyMongoError as e:
        logger.error(f"❌ MongoDB connection failed: {e}")
        db = None


# ==================== IN-MEMORY FALLBACK STATS ====================
# Used when MongoDB is unavailable (local dev / connection error).
_mem_stats = {
    "pdf_converted": 0,
    "virus_checked": 0,
    "removebg_done": 0,
    "ai_images_generated": 0,
    "virus_threats_found": 0,
    "bot_start_time": datetime.now(timezone.utc).isoformat(),
}
_mem_users = set()
_mem_activities = []
_mem_lock = threading.Lock()


# ==================== STAT HELPERS ====================

def upsert_user(user):
    """
    Save or update a Telegram user in the 'users' collection.
    Uses upsert to avoid duplicates — update last_activity on each visit.
    """
    user_doc = {
        "user_id":    user.id,
        "username":   user.username or "",
        "first_name": user.first_name or "",
        "last_activity": datetime.now(timezone.utc).isoformat(),
    }
    if db is not None:
        try:
            db["users"].update_one(
                {"user_id": user.id},
                {
                    "$set": user_doc,
                    "$setOnInsert": {"join_date": datetime.now(timezone.utc).isoformat()},
                },
                upsert=True
            )
        except PyMongoError as e:
            logger.error(f"upsert_user error: {e}")
    else:
        with _mem_lock:
            _mem_users.add(user.id)


def increment_stat(key: str, amount: int = 1):
    """Increment a global stat counter in MongoDB (or in-memory fallback)."""
    if db is not None:
        try:
            db["stats"].update_one(
                {"_id": "global"},
                {"$inc": {key: amount}},
                upsert=True
            )
        except PyMongoError as e:
            logger.error(f"increment_stat error: {e}")
    else:
        with _mem_lock:
            _mem_stats[key] = _mem_stats.get(key, 0) + amount


def get_stats() -> dict:
    """Return the global stats dict (from MongoDB or in-memory fallback)."""
    if db is not None:
        try:
            doc = db["stats"].find_one({"_id": "global"}) or {}
            return {
                "pdf_converted":       doc.get("pdf_converted", 0),
                "virus_checked":       doc.get("virus_checked", 0),
                "removebg_done":       doc.get("removebg_done", 0),
                "ai_images_generated": doc.get("ai_images_generated", 0),
                "virus_threats_found": doc.get("virus_threats_found", 0),
                "total_users":         db["users"].count_documents({}),
                "bot_start_time":      doc.get("bot_start_time", _mem_stats["bot_start_time"]),
            }
        except PyMongoError as e:
            logger.error(f"get_stats error: {e}")
    # Fallback
    with _mem_lock:
        return {
            **_mem_stats,
            "total_users": len(_mem_users),
        }


def record_activity(action: str, user, detail: str = ""):
    """
    Log an activity entry to MongoDB 'activities' collection.
    Also upserts the user record and keeps in-memory list for fallback.
    """
    # Always track the user
    upsert_user(user)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "time":      datetime.now(timezone.utc).strftime("%H:%M:%S"),
        "action":    action,
        "user_id":   user.id,
        "username":  user.username or "",
        "user":      f"@{user.username}" if user.username else f"#{user.id}",
        "detail":    detail,
    }

    if db is not None:
        try:
            db["activities"].insert_one(entry)
            # Prune activities older than the last 500 entries to save storage
            count = db["activities"].count_documents({})
            if count > 500:
                oldest = db["activities"].find().sort("timestamp", 1).limit(count - 500)
                ids = [d["_id"] for d in oldest]
                db["activities"].delete_many({"_id": {"$in": ids}})
        except PyMongoError as e:
            logger.error(f"record_activity error: {e}")
    else:
        with _mem_lock:
            _mem_activities.insert(0, entry)
            del _mem_activities[20:]  # keep last 20 in memory


def get_recent_activities(limit: int = 20) -> list:
    """Fetch the most recent activity entries."""
    if db is not None:
        try:
            docs = db["activities"].find().sort("timestamp", DESCENDING).limit(limit)
            return list(docs)
        except PyMongoError as e:
            logger.error(f"get_recent_activities error: {e}")
    with _mem_lock:
        return list(_mem_activities[:limit])


# ==================== FLASK APP ====================
flask_app = Flask(__name__)


# ==================== DASHBOARD HTML ====================
# Full dashboard HTML with new AI Images card and MongoDB-backed data.
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
    --purple:   #a855f7;
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
  .db-badge {
    display: flex; align-items: center; gap: 6px;
    background: rgba(168,85,247,0.12);
    border: 1px solid rgba(168,85,247,0.3);
    border-radius: 99px;
    padding: 5px 12px;
    font-size: 12px; font-weight: 600; color: var(--purple);
  }
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

  main { max-width: 1200px; margin: 0 auto; padding: 32px 24px; }

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

  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
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

  .card-pdf    { --card-color: var(--blue); }
  .card-virus  { --card-color: var(--red); }
  .card-bg     { --card-color: var(--green); }
  .card-users  { --card-color: var(--accent); }
  .card-ai     { --card-color: var(--purple); }
  .card-threat { --card-color: var(--accent2); }

  .section-title {
    font-size: 13px; font-weight: 700; letter-spacing: .08em;
    text-transform: uppercase; color: var(--muted);
    margin-bottom: 14px;
    display: flex; align-items: center; gap: 8px;
  }
  .section-title::after {
    content: ''; flex: 1; height: 1px; background: var(--border);
  }

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
  .badge-pdf    { background:rgba(59,130,246,.15); color:#60a5fa; }
  .badge-virus  { background:rgba(239,68,68,.15);  color:#f87171; }
  .badge-bg     { background:rgba(34,197,94,.15);  color:#4ade80; }
  .badge-ai     { background:rgba(168,85,247,.15); color:#c084fc; }
  .badge-start  { background:rgba(245,197,24,.10); color:#f5c518; }

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
  <div style="display:flex;gap:10px;align-items:center;">
    <div class="db-badge">🍃 MongoDB Atlas</div>
    <div class="live-badge"><div class="dot"></div> Bot is Live</div>
  </div>
</nav>

<main>
  <div class="uptime-bar">
    🕐 Server started: <strong>{{START_TIME}}</strong>
    &nbsp;·&nbsp; Last refresh: <strong id="last-refresh">—</strong>
  </div>

  <!-- STAT CARDS — now 6 cards including AI Images -->
  <div class="cards">
    <div class="card card-users">
      <div class="card-icon">👥</div>
      <div class="card-value">{{USER_COUNT}}</div>
      <div class="card-label">Total Users</div>
      <div class="card-sub">Unique users ever</div>
    </div>
    <div class="card card-pdf">
      <div class="card-icon">📄</div>
      <div class="card-value">{{PDF_COUNT}}</div>
      <div class="card-label">PDFs Generated</div>
      <div class="card-sub">Total conversions</div>
    </div>
    <div class="card card-virus">
      <div class="card-icon">🔍</div>
      <div class="card-value">{{VIRUS_COUNT}}</div>
      <div class="card-label">Virus Scans</div>
      <div class="card-sub">{{THREAT_COUNT}} threats found</div>
    </div>
    <div class="card card-bg">
      <div class="card-icon">🖼️</div>
      <div class="card-value">{{BG_COUNT}}</div>
      <div class="card-label">RemoveBG</div>
      <div class="card-sub">via remove.bg API</div>
    </div>
    <div class="card card-ai">
      <div class="card-icon">🎨</div>
      <div class="card-value">{{AI_COUNT}}</div>
      <div class="card-label">AI Images Generated</div>
      <div class="card-sub">via Gemini API</div>
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
          <th>User</th>
          <th>Detail</th>
        </tr>
      </thead>
      <tbody>
        {{ACTIVITY_ROWS}}
      </tbody>
    </table>
  </div>
</main>

<footer>⚡ QuickTools KH Bot Dashboard · MongoDB Atlas · Built with Flask on Render</footer>

<script>
  document.getElementById('last-refresh').textContent = new Date().toLocaleTimeString();
  // Auto-refresh every 30 seconds
  setTimeout(() => location.reload(), 30000);
</script>
</body>
</html>"""


def build_dashboard():
    """Assemble the dashboard HTML using live stats from MongoDB."""
    s = get_stats()
    pdf    = s.get("pdf_converted", 0)
    virus  = s.get("virus_checked", 0)
    bg     = s.get("removebg_done", 0)
    ai     = s.get("ai_images_generated", 0)
    users  = s.get("total_users", 0)
    threats= s.get("virus_threats_found", 0)
    total  = pdf + virus + bg + ai
    start  = str(s.get("bot_start_time", ""))[:19].replace("T", " ") + " UTC"
    rows   = get_recent_activities(20)

    # Map action names to badge CSS classes
    badge_map = {
        "PDF":     "badge-pdf",
        "VIRUS":   "badge-virus",
        "REMOVEBG":"badge-bg",
        "TTI":     "badge-ai",
        "START":   "badge-start",
    }

    if rows:
        row_html = ""
        for r in rows:
            bc = badge_map.get(r.get("action", ""), "badge-start")
            row_html += f"""<tr>
              <td><span class="time-chip">{r.get('time', '')}</span></td>
              <td><span class="badge {bc}">{r.get('action', '')}</span></td>
              <td><span class="user-chip">{r.get('user', '')}</span></td>
              <td>{r.get('detail', '')}</td>
            </tr>"""
    else:
        row_html = '<tr class="empty-row"><td colspan="4">No activity yet — waiting for users 👀</td></tr>'

    html = DASHBOARD_HTML
    html = html.replace("{{PDF_COUNT}}",      str(pdf))
    html = html.replace("{{VIRUS_COUNT}}",    str(virus))
    html = html.replace("{{BG_COUNT}}",       str(bg))
    html = html.replace("{{AI_COUNT}}",       str(ai))
    html = html.replace("{{USER_COUNT}}",     str(users))
    html = html.replace("{{THREAT_COUNT}}",   str(threats))
    html = html.replace("{{TOTAL_ACTIONS}}", str(total))
    html = html.replace("{{START_TIME}}",     start)
    html = html.replace("{{ACTIVITY_ROWS}}", row_html)
    return html


# ==================== FLASK ROUTES ====================

@flask_app.route("/")
def dashboard():
    """Admin dashboard — optionally protected by ADMIN_KEY query param."""
    if ADMIN_KEY:
        if request.args.get("key", "") != ADMIN_KEY:
            abort(403)
    return build_dashboard(), 200, {"Content-Type": "text/html; charset=utf-8"}


@flask_app.route("/health")
def health():
    """Render health check endpoint."""
    return "OK", 200


@flask_app.route("/api/stats")
def api_stats():
    """JSON stats endpoint for external monitoring."""
    if ADMIN_KEY and request.args.get("key", "") != ADMIN_KEY:
        abort(403)
    s = get_stats()
    return {
        "pdf_converted":       s.get("pdf_converted", 0),
        "virus_checked":       s.get("virus_checked", 0),
        "removebg_done":       s.get("removebg_done", 0),
        "ai_images_generated": s.get("ai_images_generated", 0),
        "total_users":         s.get("total_users", 0),
        "virus_threats_found": s.get("virus_threats_found", 0),
        "total_actions":       (s.get("pdf_converted", 0) + s.get("virus_checked", 0)
                                + s.get("removebg_done", 0) + s.get("ai_images_generated", 0)),
    }


# ==================== CONVERSATION STATES ====================
COLLECTING_PHOTOS = 1
AWAITING_FILE     = 2
AWAITING_PHOTO_BG = 3
# TTI states
TTI_AWAITING_PROMPT = 10

# In-memory session storage for photos (stateless between restarts is fine — active sessions only)
user_photos: dict[int, list[bytes]] = {}

# TTI style storage per user (set by inline button callback)
user_tti_style: dict[int, str] = {}


# ==================== /start & /help ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    record_activity("START", user, "User started bot")
    await update.message.reply_text(
        "👋 សូមស្វាគមន៍មកកាន់ ⚡ QuickTools KH!\n\n"
        "📄 /pdf — បំប្លែងរូបភាពទៅជា PDF\n"
        "🔍 /check — ពិនិត្យមើលមេរោគ\n"
        "🖼️ /removebg — លុបផ្ទៃខាងក្រោយរូបភាព\n"
        "🎨 /tti — Text To Image (AI)\n"
        "❓ /help — ព័ត៌មានបន្ថែម"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *ជំនួយការបច្ចេកទេស*\n\n"
        "*របៀបប្រើប្រាស់:*\n"
        "1️⃣ /pdf — ផ្ញើរូបភាព រួចវាយ /done ដើម្បីបំប្លែង PDF\n"
        "2️⃣ /check — ផ្ញើឯកសារដែលសង្ស័យ (ដល់ 40MB)\n"
        "3️⃣ /removebg — ផ្ញើរូបភាព ដើម្បីលុបផ្ទៃខាងក្រោយ\n"
        "4️⃣ /tti — ជ្រើសស្ទីល ហើយផ្ញើ prompt ដើម្បីបង្កើតរូបភាព AI\n"
        "5️⃣ /cancel — បោះបង់ជំហានបច្ចុប្បន្ន",
        parse_mode="Markdown"
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    user_photos.pop(user_id, None)
    user_tti_style.pop(user_id, None)
    await update.message.reply_text("❌ បានបោះបង់ជំហានសន្ទនារួចរាល់។")
    return ConversationHandler.END


# ==================== /pdf — IMAGE TO PDF ====================

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
        increment_stat("pdf_converted")
        record_activity("PDF", update.effective_user, f"{count} page(s)")
    except Exception as e:
        logger.error(f"PDF error: {e}")
        await update.message.reply_text(f"❌ មានបញ្ហា!\nError: {str(e)}")
    finally:
        user_photos.pop(user_id, None)

    return ConversationHandler.END


# ==================== /check — VIRUS TOTAL ====================

async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🔍 សូមផ្ញើ ឬ Forward ឯកសារ (File/Document) ដើម្បីឱ្យខ្ញុំពិនិត្យ។")
    return AWAITING_FILE


async def handle_virus_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    document = update.message.document
    user = update.effective_user
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
                        increment_stat("virus_threats_found")
                        await status_msg.edit_text(f"🚨 **រកឃើញមេរោគ!** ប្រព័ន្ធ {malicious} បានបញ្ជាក់ថាជាមេរោគ!")
                        record_activity("VIRUS", user, f"THREAT: {document.file_name}")
                        increment_stat("virus_checked")
                        return ConversationHandler.END

            if file_name.endswith(('.exe', '.scr', '.pif', '.bat', '.cmd', '.msi', '.vbs')):
                increment_stat("virus_threats_found")
                await status_msg.edit_text(f"🚨 **ការព្រមាន!** ឯកសារ .exe ទំហំធំ — សញ្ញាណបោកប្រាស់!")
                record_activity("VIRUS", user, f"SUSPICIOUS: {document.file_name}")
            else:
                await status_msg.edit_text(f"ℹ️ ឯកសារ {file_size_mb:.2f}MB — គ្មានប្រវត្តិអាក្រក់ក្នុង DB។ ប្រុងប្រយ័ត្ន!")
                record_activity("VIRUS", user, f"CLEAN: {document.file_name}")
        except Exception:
            await status_msg.edit_text("❌ មានបញ្ហាក្នុងការឆែក Database។")
        increment_stat("virus_checked")
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
            report = requests.get(
                f"https://www.virustotal.com/api/v3/analyses/{analysis_id}", headers=headers
            ).json()
            malicious = report['data']['attributes']['stats'].get('malicious', 0)
            if malicious > 0:
                increment_stat("virus_threats_found")
                await status_msg.edit_text(f"🚨 **រកឃើញមេរោគ!** ប្រព័ន្ធ {malicious} បានរាយការណ៍!")
                record_activity("VIRUS", user, f"THREAT: {document.file_name}")
            else:
                await status_msg.edit_text("✅ ឯកសារមានសុវត្ថិភាព!")
                record_activity("VIRUS", user, f"CLEAN: {document.file_name}")
        else:
            await status_msg.edit_text("❌ មិនអាចភ្ជាប់ VirusTotal បានទេ។")
    except Exception as e:
        logger.error(f"VirusTotal error: {e}")
        await status_msg.edit_text("❌ កើតមានកំហុសក្នុងការវិភាគ។")

    increment_stat("virus_checked")
    return ConversationHandler.END


# ==================== /removebg — BACKGROUND REMOVAL ====================

async def removebg_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🖼️ សូមផ្ញើរូបភាពដែលអ្នកចង់លុបផ្ទៃខាងក្រោយ។")
    return AWAITING_PHOTO_BG


async def handle_removebg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
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

        api_key = REMOVEBG_API_KEY
        if not api_key:
            await status_msg.edit_text("❌ គ្មាន REMOVEBG_API_KEY ក្នុង Environment Variables!")
            return ConversationHandler.END

        response = requests.post(
            "https://api.remove.bg/v1.0/removebg",
            files={"image_file": ("photo.jpg", bytes(img_bytes), "image/jpeg")},
            data={"size": "auto"},
            headers={"X-Api-Key": api_key},
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
            increment_stat("removebg_done")
            record_activity("REMOVEBG", user, "Success")
        else:
            err = response.json().get("errors", [{}])[0].get("title", response.text[:100])
            await status_msg.edit_text(f"❌ remove.bg Error: {err}")
            record_activity("REMOVEBG", user, f"Failed: {err}")

    except Exception as e:
        logger.error(f"RemoveBG error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ មានបញ្ហា៖ {str(e)[:300]}")

    return ConversationHandler.END


# ==================== /tti — TEXT TO IMAGE (GEMINI) ====================
# Flow: /tti → style selection (inline buttons) → prompt text → generate → send image

# Style definitions: label → Gemini prompt modifier
TTI_STYLES = {
    "realistic":    ("📷 Realistic",    "photorealistic, high detail, 8K, DSLR photo"),
    "anime":        ("🎨 Anime",        "anime style, Studio Ghibli inspired, vibrant colors"),
    "digital_art":  ("🖌️ Digital Art",  "digital art, concept art, detailed illustration"),
    "fantasy":      ("🏰 Fantasy",      "fantasy art, epic, magical, detailed environment"),
    "scifi":        ("🚀 Sci-Fi",       "sci-fi, futuristic, cyberpunk, neon lights, detailed"),
}


async def tti_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Entry point for /tti — show style selection as inline keyboard.
    Does NOT use ConversationHandler because the callback comes via CallbackQueryHandler.
    """
    user = update.effective_user
    record_activity("TTI", user, "Started TTI")

    # Build inline keyboard with style options
    keyboard = [
        [
            InlineKeyboardButton("📷 Realistic",   callback_data="tti_realistic"),
            InlineKeyboardButton("🎨 Anime",        callback_data="tti_anime"),
        ],
        [
            InlineKeyboardButton("🖌️ Digital Art",  callback_data="tti_digital_art"),
            InlineKeyboardButton("🏰 Fantasy",      callback_data="tti_fantasy"),
        ],
        [
            InlineKeyboardButton("🚀 Sci-Fi",       callback_data="tti_scifi"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🎨 *Text To Image*\nChoose a style:",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return TTI_AWAITING_PROMPT


async def tti_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Called when user taps a style button.
    Stores chosen style and asks for the prompt text.
    """
    query = update.callback_query
    await query.answer()  # acknowledge button tap

    user = update.effective_user
    # Extract style key from callback_data (e.g. "tti_realistic" → "realistic")
    style_key = query.data.replace("tti_", "")

    if style_key not in TTI_STYLES:
        await query.edit_message_text("❌ Invalid style. Please use /tti again.")
        return ConversationHandler.END

    # Store the chosen style for this user
    user_tti_style[user.id] = style_key
    style_label, _ = TTI_STYLES[style_key]

    await query.edit_message_text(
        f"✅ Style: *{style_label}*\n\n"
        "Now send your image prompt (in any language).\n"
        "Example: `A Khmer warrior riding a dragon`",
        parse_mode="Markdown"
    )
    return TTI_AWAITING_PROMPT


async def tti_receive_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Receives the user's text prompt, calls Gemini imagen API, sends the generated image.
    Uses Gemini's imagen-3.0-generate-002 model for image generation.
    """
    user = update.effective_user
    prompt_text = update.message.text.strip()

    if not prompt_text:
        await update.message.reply_text("❌ Please send a text prompt.")
        return TTI_AWAITING_PROMPT

    # Get style (default to realistic if somehow missing)
    style_key = user_tti_style.get(user.id, "realistic")
    style_label, style_modifier = TTI_STYLES.get(style_key, TTI_STYLES["realistic"])

    if not GEMINI_API_KEY:
        await update.message.reply_text("❌ GEMINI_API_KEY is not configured.")
        return ConversationHandler.END

    status_msg = await update.message.reply_text("⏳ Generating image…")

    # Build the full prompt with style modifier appended
    full_prompt = f"{prompt_text}, {style_modifier}"

    try:
        # Call Gemini Imagen API (imagen-3.0-generate-002)
        # Endpoint: generativelanguage.googleapis.com
        api_url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "imagen-3.0-generate-002:predict"
            f"?key={GEMINI_API_KEY}"
        )

        payload = {
            "instances": [{"prompt": full_prompt}],
            "parameters": {
                "sampleCount": 1,          # Generate 1 image (low RAM / quota usage)
                "aspectRatio": "1:1",      # Square output
            }
        }

        response = requests.post(api_url, json=payload, timeout=60)

        if response.status_code != 200:
            err_detail = response.text[:300]
            logger.error(f"Gemini Imagen error {response.status_code}: {err_detail}")
            await status_msg.edit_text(
                f"❌ Gemini API error ({response.status_code}).\n"
                "Please check your GEMINI_API_KEY and try again."
            )
            return ConversationHandler.END

        result = response.json()

        # The API returns base64-encoded PNG in predictions[0].bytesBase64Encoded
        predictions = result.get("predictions", [])
        if not predictions:
            await status_msg.edit_text("❌ No image was generated. Try a different prompt.")
            return ConversationHandler.END

        img_b64 = predictions[0].get("bytesBase64Encoded", "")
        if not img_b64:
            await status_msg.edit_text("❌ Image data missing from API response.")
            return ConversationHandler.END

        # Decode base64 image bytes
        img_bytes = base64.b64decode(img_b64)
        img_buffer = BytesIO(img_bytes)
        img_buffer.seek(0)

        # Delete the "generating" status message
        await status_msg.delete()

        # Send the generated image as a photo
        await update.message.reply_photo(
            photo=InputFile(img_buffer, filename="generated.png"),
            caption=(
                f"🎨 *{style_label}*\n"
                f"📝 Prompt: _{prompt_text}_"
            ),
            parse_mode="Markdown"
        )

        # Record stat and activity
        increment_stat("ai_images_generated")
        record_activity("TTI", user, f"{style_label}: {prompt_text[:60]}")

    except requests.exceptions.Timeout:
        await status_msg.edit_text("❌ Request timed out. Gemini API took too long.")
    except Exception as e:
        logger.error(f"TTI error: {e}", exc_info=True)
        await status_msg.edit_text(f"❌ Error generating image: {str(e)[:200]}")
    finally:
        # Clean up style storage for this user
        user_tti_style.pop(user.id, None)

    return ConversationHandler.END


# ==================== SERVER RUNNERS ====================

def run_flask():
    """Run Flask in a background daemon thread (for Render health checks + dashboard)."""
    flask_app.run(host="0.0.0.0", port=PORT)


async def run_bot():
    """Build and start the Telegram bot with all handlers."""
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN not set!")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # ---- ConversationHandler for PDF / Check / RemoveBG (existing features) ----
    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("pdf",      start_pdf),
            CommandHandler("check",    check_command),
            CommandHandler("removebg", removebg_command),
        ],
        states={
            COLLECTING_PHOTOS: [
                MessageHandler(filters.PHOTO,          receive_photo),
                MessageHandler(filters.Document.IMAGE, receive_document_photo),
                CommandHandler("done", convert_to_pdf),
            ],
            AWAITING_FILE: [
                MessageHandler(filters.Document.ALL, handle_virus_check)
            ],
            AWAITING_PHOTO_BG: [
                MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_removebg)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # ---- ConversationHandler for TTI (new feature) ----
    tti_handler = ConversationHandler(
        entry_points=[CommandHandler("tti", tti_command)],
        states={
            TTI_AWAITING_PROMPT: [
                # Style button callback
                CallbackQueryHandler(tti_style_callback, pattern=r"^tti_"),
                # Text prompt from user
                MessageHandler(filters.TEXT & ~filters.COMMAND, tti_receive_prompt),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # Register handlers (order matters — specific before general)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help",  help_command))
    app.add_handler(conv_handler)
    app.add_handler(tti_handler)

    logger.info("🤖 Bot is starting...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("✅ Bot is running. Press Ctrl+C to stop.")
    await asyncio.Event().wait()  # Run forever


def main():
    """Entry point — init MongoDB, start Flask thread, run bot."""
    # Initialize MongoDB Atlas connection
    init_mongo()

    # Record bot start time in MongoDB stats document (only if not already set)
    if db is not None:
        try:
            db["stats"].update_one(
                {"_id": "global"},
                {"$setOnInsert": {"bot_start_time": datetime.now(timezone.utc).isoformat()}},
                upsert=True
            )
        except PyMongoError:
            pass

    # Start Flask dashboard in background thread (daemon = dies when main thread exits)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Flask dashboard running on port {PORT}")

    # Run Telegram bot (blocking)
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
