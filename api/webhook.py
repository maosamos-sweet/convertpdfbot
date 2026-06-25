import os
import json
import requests
from http.server import BaseHTTPRequestHandler

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"

def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10
    )

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        try:
            length = int(self.headers.get("content-length", 0))
            body = self.rfile.read(length)
            update = json.loads(body.decode("utf-8"))

            message = update.get("message", {})
            chat = message.get("chat", {})
            text = message.get("text", "")
            chat_id = chat.get("id")

            if not chat_id:
                self.send_response(200)
                self.end_headers()
                return

            if text == "/start":
                send_message(chat_id, "👋 សួស្តី! Bot is running on Vercel Webhook ✅")
            else:
                send_message(chat_id, "✅ Webhook received your message.")

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        except Exception as e:
            print("Webhook error:", e)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"QuickTools KH Webhook is alive")
