import os, json, io, time, urllib.parse, requests
from http.server import BaseHTTPRequestHandler
from PIL import Image
from pymongo import MongoClient

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MONGODB_URI = os.environ.get("MONGODB_URI", "")
REMOVEBG_API_KEY = os.environ.get("REMOVEBG_API_KEY", "")
VIRUSTOTAL_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "")

API = f"https://api.telegram.org/bot{TOKEN}"

mongo = MongoClient(MONGODB_URI) if MONGODB_URI else None
db = mongo["quicktools_vercel"] if mongo else None

def tg(method, data=None, files=None):
    return requests.post(f"{API}/{method}", data=data, files=files, timeout=60)

def send(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text})

def get_file(file_id):
    r = requests.get(f"{API}/getFile?file_id={file_id}", timeout=30).json()
    path = r["result"]["file_path"]
    return requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=60).content

def set_state(uid, state, data=None):
    if db:
        db.sessions.update_one(
            {"uid": uid},
            {"$set": {"state": state, "data": data or {}, "updated": time.time()}},
            upsert=True
        )

def get_state(uid):
    if not db:
        return {}
    return db.sessions.find_one({"uid": uid}) or {}

def clear_state(uid):
    if db:
        db.sessions.delete_one({"uid": uid})

def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    if text == "/start":
        clear_state(user_id)
        send(chat_id, "👋 QuickTools KH on Vercel ✅\n\n/pdf រូបភាពទៅ PDF\n/check ពិនិត្យ file\n/removebg លុប background\n/tti AI image")
        return

    if text == "/help":
        send(chat_id, "/pdf ផ្ញើរូបភាពច្រើន រួច /done\n/check ផ្ញើ file\n/removebg ផ្ញើរូប\n/tti សរសេរ prompt")
        return

    if text == "/cancel":
        clear_state(user_id)
        send(chat_id, "❌ បានបោះបង់")
        return

    if text == "/pdf":
        set_state(user_id, "pdf", {"photos": []})
        send(chat_id, "📸 ផ្ញើរូបភាព រួចវាយ /done")
        return

    if text == "/done":
        s = get_state(user_id)
        if s.get("state") != "pdf":
            send(chat_id, "❌ សូមវាយ /pdf ជាមុន")
            return
        photos = s.get("data", {}).get("photos", [])
        if not photos:
            send(chat_id, "❌ មិនទាន់មានរូបភាព")
            return

        send(chat_id, f"⏳ កំពុងបង្កើត PDF {len(photos)} រូប...")
        images = []
        for fid in photos:
            b = get_file(fid)
            img = Image.open(io.BytesIO(b))
            if img.mode != "RGB":
                img = img.convert("RGB")
            images.append(img)

        out = io.BytesIO()
        images[0].save(out, format="PDF", save_all=True, append_images=images[1:], resolution=150)
        out.seek(0)

        tg("sendDocument",
           {"chat_id": chat_id, "caption": "✅ PDF រួចរាល់"},
           {"document": ("converted.pdf", out, "application/pdf")})
        clear_state(user_id)
        return

    if text == "/removebg":
        set_state(user_id, "removebg")
        send(chat_id, "🖼️ ផ្ញើរូបភាពដែលចង់លុប background")
        return

    if text == "/check":
        set_state(user_id, "check")
        send(chat_id, "🔍 ផ្ញើឯកសារដែលចង់ពិនិត្យ")
        return

    if text == "/tti":
        set_state(user_id, "tti")
        send(chat_id, "🎨 សរសេរ prompt របស់អ្នក ឧ: Khmer warrior riding dragon")
        return

    state = get_state(user_id).get("state")

    if state == "tti" and text:
        send(chat_id, "⏳ Generating image...")
        prompt = urllib.parse.quote(text + ", high quality, detailed")
        img_url = f"https://image.pollinations.ai/prompt/{prompt}"
        r = requests.get(img_url, timeout=60)
        if r.status_code == 200:
            tg("sendPhoto",
               {"chat_id": chat_id, "caption": f"🎨 {text}"},
               {"photo": ("image.png", io.BytesIO(r.content), "image/png")})
        else:
            send(chat_id, "❌ AI image failed")
        clear_state(user_id)
        return

    if "photo" in msg:
        state_doc = get_state(user_id)
        if state_doc.get("state") == "pdf":
            fid = msg["photo"][-1]["file_id"]
            data = state_doc.get("data", {})
            photos = data.get("photos", [])
            photos.append(fid)
            set_state(user_id, "pdf", {"photos": photos})
            send(chat_id, f"✅ រូបទី {len(photos)} បានទទួល។ ផ្ញើបន្ថែម ឬ /done")
            return

        if state_doc.get("state") == "removebg":
            if not REMOVEBG_API_KEY:
                send(chat_id, "❌ REMOVEBG_API_KEY មិនទាន់ដាក់")
                return
            fid = msg["photo"][-1]["file_id"]
            b = get_file(fid)
            send(chat_id, "⏳ កំពុងលុប background...")
            r = requests.post(
                "https://api.remove.bg/v1.0/removebg",
                files={"image_file": ("photo.jpg", b, "image/jpeg")},
                data={"size": "auto"},
                headers={"X-Api-Key": REMOVEBG_API_KEY},
                timeout=60
            )
            if r.status_code == 200:
                tg("sendDocument",
                   {"chat_id": chat_id, "caption": "✅ Removed background"},
                   {"document": ("removed_bg.png", io.BytesIO(r.content), "image/png")})
            else:
                send(chat_id, "❌ RemoveBG failed")
            clear_state(user_id)
            return

    if "document" in msg:
        state_doc = get_state(user_id)
        doc = msg["document"]

        if state_doc.get("state") == "pdf" and doc.get("mime_type", "").startswith("image/"):
            fid = doc["file_id"]
            data = state_doc.get("data", {})
            photos = data.get("photos", [])
            photos.append(fid)
            set_state(user_id, "pdf", {"photos": photos})
            send(chat_id, f"✅ រូបទី {len(photos)} បានទទួល។ ផ្ញើបន្ថែម ឬ /done")
            return

        if state_doc.get("state") == "check":
            if not VIRUSTOTAL_API_KEY:
                send(chat_id, "❌ VIRUSTOTAL_API_KEY មិនទាន់ដាក់")
                return
            send(chat_id, "⏳ កំពុងពិនិត្យ file...")
            b = get_file(doc["file_id"])
            r = requests.post(
                "https://www.virustotal.com/api/v3/files",
                headers={"x-apikey": VIRUSTOTAL_API_KEY},
                files={"file": (doc.get("file_name", "file"), b)},
                timeout=60
            )
            if r.status_code == 200:
                send(chat_id, "✅ File uploaded to VirusTotal. សូមរង់ចាំ report នៅ version បន្ទាប់។")
            else:
                send(chat_id, "❌ VirusTotal failed")
            clear_state(user_id)
            return

    send(chat_id, "សូមប្រើ /help")

class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"QuickTools KH webhook alive")

    def do_POST(self):
        try:
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length)
            update = json.loads(body.decode("utf-8"))

            if "message" in update:
                handle_message(update["message"])

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except Exception as e:
            print("ERROR:", e)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
