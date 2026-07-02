import os
import json
import io
import time
import requests
from http.server import BaseHTTPRequestHandler
from PIL import Image
from pymongo import MongoClient

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
MONGODB_URI = os.environ.get("MONGODB_URI", "")
REMOVEBG_API_KEY = os.environ.get("REMOVEBG_API_KEY", "")
VIRUSTOTAL_API_KEY = os.environ.get("VIRUSTOTAL_API_KEY", "")
HF_TOKEN = os.environ.get("HF_TOKEN", "")

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
            upsert=True,
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
            "Content-Type": "application/json",
        },
        json={
            "inputs": prompt,
            "parameters": {
                "num_inference_steps": 4,
                "guidance_scale": 0.0,
                "width": 1024,
                "height": 1024,
            },
        },
        timeout=120,
    )

    if response.status_code != 200:
        try:
            return None, response.json()
        except Exception:
            return None, response.text[:300]

    return response.content, None


def check_virustotal(file_bytes, filename):
    upload = requests.post(
        "https://www.virustotal.com/api/v3/files",
        headers={"x-apikey": VIRUSTOTAL_API_KEY},
        files={"file": (filename, file_bytes)},
        timeout=60,
    )

    if upload.status_code not in [200, 201]:
        return None, f"Upload failed: {upload.text[:500]}"

    analysis_id = upload.json()["data"]["id"]

    result = None
    for _ in range(12):
        time.sleep(5)

        r = requests.get(
            f"https://www.virustotal.com/api/v3/analyses/{analysis_id}",
            headers={"x-apikey": VIRUSTOTAL_API_KEY},
            timeout=30,
        )

        if r.status_code != 200:
            continue

        result = r.json()
        status = result["data"]["attributes"].get("status")

        if status == "completed":
            return result, None

    return None, "Scan not completed yet. Please try again later."


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = msg.get("text", "")

    if text == "/start":
        clear_state(user_id)
        send(
            chat_id,
            "QuickTools KH on Vercel ✅\n\n"
            "/pdf រូបភាពទៅ PDF\n"
            "/check ពិនិត្យ file\n"
            "/removebg លុប background\n"
            "/tti AI image with FLUX",
        )
        return

    if text == "/help":
        send(
            chat_id,
            "/pdf ផ្ញើរូបភាពច្រើន រួច /done\n"
            "/check ផ្ញើ file\n"
            "/removebg ផ្ញើរូប\n"
            "/tti សរសេរ prompt បង្កើតរូប AI",
        )
        return

    if text == "/cancel":
        clear_state(user_id)
        send(chat_id, "❌ បានបោះបង់")
        return

    if text == "/pdf":
        set_state(user_id, "pdf", {"photos": []})
        send(chat_id, "ផ្ញើរូបភាព រួចវាយ /done")
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
            images[0].save(
                out,
                format="PDF",
                save_all=True,
                append_images=images[1:],
                resolution=150,
            )
            out.seek(0)

            tg(
                "sendDocument",
                {"chat_id": chat_id, "caption": "✅ PDF រួចរាល់"},
                {"document": ("converted.pdf", out, "application/pdf")},
            )

        except Exception as e:
            send(chat_id, f"❌ PDF error: {str(e)[:150]}")

        clear_state(user_id)
        return

    if text == "/removebg":
        set_state(user_id, "removebg")
        send(chat_id, "ផ្ញើរូបភាពដែលចង់លុប background")
        return

    if text == "/check":
        set_state(user_id, "check")
        send(chat_id, "ផ្ញើឯកសារដែលចង់ពិនិត្យ")
        return

    if text == "/tti":
        set_state(user_id, "tti")
        send(
            chat_id,
            "សរសេរ prompt របស់អ្នក\n"
            "ឧ: A beautiful Khmer warrior king, ultra realistic, cinematic lighting",
        )
        return

    state_doc = get_state(user_id)
    state = state_doc.get("state")

    if state == "tti" and text:
        send(chat_id, "Generating with FLUX AI...")

        prompt = text.strip()
        image_bytes, error = generate_flux_image(prompt)

        if image_bytes:
            tg(
                "sendPhoto",
                {"chat_id": chat_id, "caption": f"FLUX\n\n{prompt[:900]}"},
                {"photo": ("flux.png", io.BytesIO(image_bytes), "image/png")},
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
                    timeout=60,
                )

                if r.status_code == 200:
                    tg(
                        "sendDocument",
                        {"chat_id": chat_id, "caption": "✅ Removed background"},
                        {"document": ("removed_bg.png", io.BytesIO(r.content), "image/png")},
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
                filename = doc.get("file_name", "file")

                send(chat_id, "⏳ កំពុង upload និងពិនិត្យ file...")

                b = get_file(doc["file_id"])
                result, error = check_virustotal(b, filename)

                if error:
                    send(chat_id, f"⚠️ VirusTotal: {error}")
                    clear_state(user_id)
                    return

                stats = result["data"]["attributes"]["stats"]

                malicious = stats.get("malicious", 0)
                suspicious = stats.get("suspicious", 0)
                harmless = stats.get("harmless", 0)
                undetected = stats.get("undetected", 0)

                if malicious > 0 or suspicious > 0:
                    verdict = "⚠️ គ្រោះថ្នាក់ / សង្ស័យ"
                else:
                    verdict = "✅ មើលទៅ Clean"

                msg_text = (
                    f"{verdict}\n\n"
                    f"📄 File: {filename}\n"
                    f"🛑 Malicious: {malicious}\n"
                    f"⚠️ Suspicious: {suspicious}\n"
                    f"✅ Harmless: {harmless}\n"
                    f"❔ Undetected: {undetected}"
                )

                send(chat_id, msg_text)

            except Exception as e:
                send(chat_id, f"❌ VirusTotal error: {str(e)[:200]}")

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
