import os, json, io, time, re, requests
from http.server import BaseHTTPRequestHandler
from PIL import Image
from pymongo import MongoClient

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MONGODB_URI = os.environ.get("MONGODB_URI", "")
REMOVEBG_API_KEY = os.environ.get("REMOVEBG_API_KEY", "")
VIRUSTOTAL_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

API = f"https://api.telegram.org/bot{TOKEN}"
HF_MODEL_URL = "https://router.huggingface.co/hf-inference/models/black-forest-labs/FLUX.1-schnell"

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
    if db is not None:
        db.sessions.update_one(
            {"uid": uid},
            {"$set": {"state": state, "data": data or {}, "updated": time.time()}},
            upsert=True
        )

def get_state(uid):
    if db is None:
        return {}
    return db.sessions.find_one({"uid": uid}) or {}

def clear_state(uid):
    if db is not None:
        db.sessions.delete_one({"uid": uid})

def generate_flux_image(prompt):
    if not HF_TOKEN:
        return None, "HF_TOKEN missing"

    response = requests.post(
        HF_MODEL_URL,
        headers={
            "Authorization": f"Bearer {HF_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "inputs": prompt,
            "parameters": {
                "num_inference_steps": 4,
                "guidance_scale": 0.0,
                "width": 1024,
                "height": 1024
            }
        },
        timeout=120
    )

    if response.status_code != 200:
        try:
            return None, response.json()
        except Exception:
            return None, response.text[:300]

    return response.content, None

def _vt_stats_to_verdict(stats):
    malicious = stats.get("malicious", 0)
    suspicious = stats.get("suspicious", 0)
    harmless = stats.get("harmless", 0)
    undetected = stats.get("undetected", 0)
    total = malicious + suspicious + harmless + undetected

    if malicious > 0 or suspicious > 0:
        return f"🚨 គ្រោះថ្នាក់! {malicious} malicious, {suspicious} suspicious (ក្នុងចំណោម {total} engines)"
    return f"✅ សុវត្ថិភាព — 0 detections (ក្នុងចំណោម {total} engines)"


def check_virustotal(file_bytes, filename):
    """Look up a file on VirusTotal by SHA256 first (avoids AlreadySubmittedError
    and is instant for already-known files). Falls back to uploading + polling
    only if VT has never seen this file before.
    Returns (verdict_text, error)."""
    import hashlib
    sha256 = hashlib.sha256(file_bytes).hexdigest()

    # 1. Try existing report first
    try:
        r = requests.get(
            f"https://www.virustotal.com/api/v3/files/{sha256}",
            headers={"x-apikey": VIRUSTOTAL_API_KEY},
            timeout=30
        )
        if r.status_code == 200:
            stats = r.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
            return _vt_stats_to_verdict(stats), None
        # 404 = VT has never seen this file -> fall through to upload
    except Exception:
        pass

    # 2. Upload for fresh scan
    try:
        r = requests.post(
            "https://www.virustotal.com/api/v3/files",
            headers={"x-apikey": VIRUSTOTAL_API_KEY},
            files={"file": (filename, file_bytes)},
            timeout=60
        )
    except Exception as e:
        return None, f"upload failed: {str(e)[:150]}"

    analysis_id = None
    if r.status_code == 200:
        analysis_id = r.json().get("data", {}).get("id")
    elif r.status_code == 409:
        # Someone else is scanning the same hash right now - poll the file
        # report by hash instead of by analysis id.
        for _ in range(20):
            time.sleep(3)
            try:
                fr = requests.get(
                    f"https://www.virustotal.com/api/v3/files/{sha256}",
                    headers={"x-apikey": VIRUSTOTAL_API_KEY},
                    timeout=30
                )
            except Exception:
                continue
            if fr.status_code == 200:
                stats = fr.json().get("data", {}).get("attributes", {}).get("last_analysis_stats", {})
                if stats:
                    return _vt_stats_to_verdict(stats), None
        return None, "ការវិភាគចំណាយពេលយូរពេក — សូមព្យាយាមម្តងទៀតក្រោយ"
    else:
        return None, f"upload failed ({r.status_code}): {r.text[:200]}"

    if not analysis_id:
        return None, "no analysis id returned"

    # 3. Poll the analysis until it completes
    for _ in range(20):
        time.sleep(3)
        try:
            ar = requests.get(
                f"https://www.virustotal.com/api/v3/analyses/{analysis_id}",
                headers={"x-apikey": VIRUSTOTAL_API_KEY},
                timeout=30
            )
        except Exception:
            continue

        if ar.status_code != 200:
            continue

        data = ar.json().get("data", {})
        status = data.get("attributes", {}).get("status")
        if status == "completed":
            stats = data.get("attributes", {}).get("stats", {})
            return _vt_stats_to_verdict(stats), None

    return None, "ការវិភាគចំណាយពេលយូរពេក — សូមព្យាយាមម្តងទៀតក្រោយ"


SUSPICIOUS_EXT_PATTERN = re.compile(
    r"\.(pdf|doc|docx|jpg|jpeg|png|xls|xlsx|txt)\.(exe|scr|bat|cmd|js|jar|vbs|ps1|msi|z|zip|rar|7z|apk|com|pif)$",
    re.IGNORECASE
)

def local_filename_check(filename):
    """Quick heuristic for disguised-executable filenames like
    'Resume.pdf.z' or 'Photo.jpg.exe' - a common scam pattern."""
    if SUSPICIOUS_EXT_PATTERN.search(filename or ""):
        return (
            "🚨 ប្រុងប្រយ័ត្ន! ឈ្មោះ file នេះមាន double extension "
            f"(`{filename}`) — នេះជា pattern ដែល scammer ច្រើនប្រើដើម្បីលាក់ file "
            "គ្រោះថ្នាក់ (.exe/.scr/.z ជាដើម) ឲ្យមើលទៅដូច file ធម្មតា។ "
            "កុំបើក file នេះ! កំពុងបន្តពិនិត្យជាមួយ VirusTotal..."
        )
    return None


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    if text == "/start":
        clear_state(user_id)
        send(chat_id, "👋 QuickTools KH on Vercel ✅\n\n/pdf រូបភាពទៅ PDF\n/check ពិនិត្យ file\n/removebg លុប background\n/tti AI image with FLUX")
        return

    if text == "/help":
        send(chat_id, "/pdf ផ្ញើរូបភាពច្រើន រួច /done\n/check ផ្ញើ file\n/removebg ផ្ញើរូប\n/tti សរសេរ prompt បង្កើតរូប AI")
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

        try:
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

            tg(
                "sendDocument",
                {"chat_id": chat_id, "caption": "✅ PDF រួចរាល់"},
                {"document": ("converted.pdf", out, "application/pdf")}
            )
        except Exception as e:
            send(chat_id, f"❌ PDF error: {str(e)[:150]}")

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
        send(chat_id, "🎨 សរសេរ prompt របស់អ្នក\nឧ: A beautiful Khmer warrior king, ultra realistic, cinematic lighting")
        return

    state_doc = get_state(user_id)
    state = state_doc.get("state")

    if state == "tti" and text:
        send(chat_id, "🎨 Generating with FLUX AI...")

        prompt = text.strip()
        image_bytes, error = generate_flux_image(prompt)

        if image_bytes:
            tg(
                "sendPhoto",
                {"chat_id": chat_id, "caption": f"🎨 FLUX\n\n{prompt[:900]}"},
                {"photo": ("flux.png", io.BytesIO(image_bytes), "image/png")}
            )
        else:
            send(chat_id, f"❌ Hugging Face error:\n{str(error)[:500]}")

        clear_state(user_id)
        return

    if "photo" in msg:
        if state == "pdf":
            fid = msg["photo"][-1]["file_id"]
            data = state_doc.get("data", {})
            photos = data.get("photos", [])
            photos.append(fid)
            set_state(user_id, "pdf", {"photos": photos})
            send(chat_id, f"✅ រូបទី {len(photos)} បានទទួល។ ផ្ញើបន្ថែម ឬ /done")
            return

        if state == "removebg":
            if not REMOVEBG_API_KEY:
                send(chat_id, "❌ REMOVEBG_API_KEY មិនទាន់ដាក់")
                return

            try:
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
                    tg(
                        "sendDocument",
                        {"chat_id": chat_id, "caption": "✅ Removed background"},
                        {"document": ("removed_bg.png", io.BytesIO(r.content), "image/png")}
                    )
                else:
                    send(chat_id, f"❌ RemoveBG failed: {r.text[:200]}")
            except Exception as e:
                send(chat_id, f"❌ RemoveBG error: {str(e)[:150]}")

            clear_state(user_id)
            return

    if "document" in msg:
        doc = msg["document"]

        if state == "pdf" and doc.get("mime_type", "").startswith("image/"):
            fid = doc["file_id"]
            data = state_doc.get("data", {})
            photos = data.get("photos", [])
            photos.append(fid)
            set_state(user_id, "pdf", {"photos": photos})
            send(chat_id, f"✅ រូបទី {len(photos)} បានទទួល។ ផ្ញើបន្ថែម ឬ /done")
            return

        if state == "check":
            if not VIRUSTOTAL_API_KEY:
                send(chat_id, "❌ VIRUSTOTAL_API_KEY មិនទាន់ដាក់")
                return

            try:
                warning = local_filename_check(doc.get("file_name", ""))
                if warning:
                    send(chat_id, warning)

                send(chat_id, "⏳ កំពុងពិនិត្យ file... (អាចចំណាយពេលដល់ 1 នាទី)")
                b = get_file(doc["file_id"])

                verdict, error = check_virustotal(b, doc.get("file_name", "file"))
                if verdict:
                    send(chat_id, verdict)
                else:
                    send(chat_id, f"❌ VirusTotal error: {error}")
            except Exception as e:
                send(chat_id, f"❌ VirusTotal error: {str(e)[:150]}")

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
            if WEBHOOK_SECRET:
                incoming = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                if incoming != WEBHOOK_SECRET:
                    self.send_response(403)
                    self.end_headers()
                    self.wfile.write(b"Forbidden")
                    return

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
