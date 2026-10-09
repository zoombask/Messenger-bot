#!/usr/bin/env python3
"""Facebook Messenger webhook for Nishan's AI sales bot (stdlib only).

Reuses bot.py's brain (Gemini, Google Sheets catalog, orders, demand,
validation, abandoned-cart recovery) via import — no code duplication.

- GET  /webhook : Meta verification (hub.mode / hub.verify_token / challenge)
- POST /webhook : Messenger events -> routed through bot.handle_update()
                  with Messenger-specific sending via monkeypatched bot.tg/bot.send
- Admin (Nishan) notifications ALWAYS go to Telegram, never Messenger.

State is kept in state_messenger.json (separate from Telegram's state.json),
so the live Telegram bot is never touched.

Public HTTPS: Meta must be able to POST to this server. On this VM there is
no public URL, so deploy this file somewhere public (or use a tunnel) and
set the Callback URL in the Meta App dashboard. Everything else is ready.
"""
import hashlib
import hmac
import json
import os
import threading
import time
import traceback
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(BASE_DIR)

import sys
sys.path.insert(0, BOT_DIR)
import bot  # noqa: E402  (the shared brain — Telegram bot untouched)

# ---------------------------------------------------------------- config
MSGR_PAGE_TOKEN = bot.ENV.get("MSGR_PAGE_TOKEN", "").strip()
MSGR_VERIFY_TOKEN = bot.ENV.get("MSGR_VERIFY_TOKEN", "").strip()
MSGR_APP_SECRET = bot.ENV.get("MSGR_APP_SECRET", "").strip()
GRAPH_API = "https://graph.facebook.com/v21.0"

# Separate state file — Telegram's state.json is never touched.
bot.STATE_PATH = os.path.join(BOT_DIR, "state_messenger.json")

# Messenger message IDs already handled (duplicate-delivery guard).
_seen_mids = set()
_SEEN_CAP = 2000

# ---------------------------------------------------------------- sending
def _split_text(text, limit=1900):
    """Split long text for Messenger's ~2000 char limit."""
    text = text or ""
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    if text:
        yield text


def msgr_api(payload, timeout=30):
    """POST to the Messenger Send API."""
    if not MSGR_PAGE_TOKEN:
        raise RuntimeError("MSGR_PAGE_TOKEN is not configured")
    url = f"{GRAPH_API}/me/messages?access_token={MSGR_PAGE_TOKEN}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def msgr_send_text(psid, text):
    for chunk in _split_text(text):
        msgr_api({"recipient": {"id": str(psid)},
                  "messaging_type": "RESPONSE",
                  "message": {"text": chunk}})


def msgr_send_image(psid, photo_url, caption=""):
    if caption:
        msgr_send_text(psid, caption)
    msgr_api({"recipient": {"id": str(psid)},
              "messaging_type": "RESPONSE",
              "message": {"attachment": {
                  "type": "image",
                  "payload": {"url": photo_url, "is_reusable": True}}}})


def msgr_send_action(psid, action="typing_on"):
    act = "typing_on" if action == "typing" else "mark_seen"
    try:
        msgr_api({"recipient": {"id": str(psid)},
                  "sender_action": act})
    except Exception:
        pass


# ------------------------------------------------- route bot.py through Messenger
_orig_tg = bot.tg
_orig_send = bot.send
_orig_forward_order = bot.forward_order


def _msgr_tg(method, params=None, timeout=35):
    """Route bot.py's Telegram calls: admin -> Telegram, customer -> Messenger."""
    params = params or {}
    chat_id = str(params.get("chat_id", ""))
    # Nishan's notifications must ALWAYS go to Telegram.
    if chat_id and chat_id == str(bot.OWNER_CHAT_ID):
        return _orig_tg(method, params, timeout)
    if method == "sendMessage":
        msgr_send_text(chat_id, params.get("text", ""))
        return {"ok": True}
    if method == "sendPhoto":
        msgr_send_image(chat_id, params.get("photo", ""),
                        params.get("caption", ""))
        return {"ok": True}
    if method == "sendChatAction":
        msgr_send_action(chat_id, params.get("action", "typing"))
        return {"ok": True}
    if method == "sendDocument":
        # Messenger has no direct CSV-document equivalent here; notify admin.
        _orig_tg("sendMessage",
                 {"chat_id": bot.OWNER_CHAT_ID,
                  "text": "ℹ️ Messenger কাস্টমার CSV চেয়েছে — Telegram থেকে "
                          "ডাউনলোড করুন।"}, timeout)
        return {"ok": True}
    # getWebhookInfo / deleteWebhook / getUpdates etc. stay on Telegram.
    return _orig_tg(method, params, timeout)


def _msgr_send(chat_id, text):
    msgr_send_text(str(chat_id), text)
    return {"ok": True}


def _msgr_forward_order(chat_id, fields):
    """Tag Messenger orders so Nishan sees the channel, then reuse bot logic."""
    fields = list(fields)
    while len(fields) < 7:
        fields.append("")
    if fields[3] and not fields[3].startswith("[Messenger]"):
        fields[3] = "[Messenger] " + fields[3]
    return _orig_forward_order(chat_id, fields)


bot.tg = _msgr_tg
bot.send = _msgr_send
bot.forward_order = _msgr_forward_order

# ------------------------------------------------- incoming events
def _mark_seen(mid):
    _seen_mids.add(mid)
    if len(_seen_mids) > _SEEN_CAP:
        # drop oldest half (sets are unordered; fine for a dedup cache)
        for _ in range(_SEEN_CAP // 2):
            _seen_mids.pop()


def handle_messaging_event(ev):
    """Handle one Messenger messaging event."""
    sender = (ev.get("sender") or {}).get("id")
    if not sender:
        return
    psid = str(sender)

    # Postbacks (e.g. Get Started button).
    postback = ev.get("postback") or {}
    if postback:
        payload = (postback.get("payload") or "").upper()
        if payload in ("GET_STARTED", "START"):
            msgr_send_text(
                psid,
                "আসসালামু আলাইকুম! 👋 আমি নিশানের এজেন্ট। প্রোডাক্ট দেখতে "
                "চাইলে লিখুন (যেমন: 'ঘড়ি দেখাও'), অর্ডার করতে চাইলে বলুন। "
                "আমি সাহায্য করছি। 😊")
            return

    msg = ev.get("message") or {}
    if msg.get("is_echo"):
        return  # our own sent message — ignore
    mid = msg.get("mid", "")
    if mid and mid in _seen_mids:
        print(f"[msgr] duplicate mid {mid} ignored", flush=True)
        return
    if mid:
        _mark_seen(mid)

    text = (msg.get("text") or "").strip()
    if not text:
        # Stickers/attachments without text.
        msgr_send_text(psid, "দুঃখিত, আমি এখন শুধু লেখা মেসেজ বুঝতে পারি। 😊")
        return

    # Reuse the entire Telegram brain: phone capture, carts, Gemini,
    # ORDER:/DEMAND:/PHOTO:/SEARCH: — all routed to Messenger via the
    # monkeypatched send/tg above.
    fake_update = {"message": {"chat": {"id": psid}, "text": text}}
    try:
        bot.handle_update(fake_update)
    except Exception:
        print(f"[msgr update error]\n{traceback.format_exc()}", flush=True)
        try:
            msgr_send_text(psid, "দুঃখিত, একটু সমস্যা হচ্ছে। আবার চেষ্টা করুন। 🙏")
        except Exception:
            pass


def verify_signature(body, sig_header):
    """Verify X-Hub-Signature-256 using the App Secret."""
    if not MSGR_APP_SECRET:
        print("[msgr] WARNING: MSGR_APP_SECRET empty — skipping verification",
              flush=True)
        return True
    if not sig_header or not sig_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(MSGR_APP_SECRET.encode(), body,
                                    hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


class WebhookHandler(BaseHTTPRequestHandler):
    server_version = "NishanMsgr/1.0"

    def _respond(self, code, body, ctype="text/plain; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # quieter logs
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def do_GET(self):
        parts = urllib.parse.urlparse(self.path)
        if parts.path.rstrip("/") != "/webhook":
            self._respond(404, "not found")
            return
        q = urllib.parse.parse_qs(parts.query)
        mode = q.get("hub.mode", [""])[0]
        token = q.get("hub.verify_token", [""])[0]
        challenge = q.get("hub.challenge", [""])[0]
        if mode == "subscribe" and token and token == MSGR_VERIFY_TOKEN:
            print("[msgr] webhook verified by Meta", flush=True)
            self._respond(200, challenge)
        else:
            print("[msgr] webhook verification FAILED", flush=True)
            self._respond(403, "verification failed")

    def do_POST(self):
        parts = urllib.parse.urlparse(self.path)
        if parts.path.rstrip("/") != "/webhook":
            self._respond(404, "not found")
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        if not verify_signature(body,
                                self.headers.get("X-Hub-Signature-256", "")):
            print("[msgr] bad signature — rejected", flush=True)
            self._respond(403, "bad signature")
            return
        try:
            data = json.loads(body.decode("utf-8") or "{}")
        except Exception:
            self._respond(400, "bad json")
            return
        # Always ACK fast; Meta retries on timeouts (dedup via mid).
        self._respond(200, "ok")
        if data.get("object") != "page":
            return
        for entry in data.get("entry", []):
            for ev in entry.get("messaging", []):
                try:
                    handle_messaging_event(ev)
                except Exception:
                    print(f"[msgr event error]\n{traceback.format_exc()}",
                          flush=True)


def _cart_loop():
    """Abandoned-cart recovery for Messenger (every 15 min)."""
    while True:
        time.sleep(900)
        try:
            bot.check_abandoned_carts()
        except Exception:
            print(f"[msgr cart error]\n{traceback.format_exc()}", flush=True)


def main():
    bot.load_state()
    print(f"[msgr] state loaded: customers={len(bot.customers)}, "
          f"orders={len(bot.orders)}, demand={len(bot.demand)}", flush=True)
    if not MSGR_PAGE_TOKEN:
        print("[msgr] WARNING: MSGR_PAGE_TOKEN empty — sending disabled until "
              "you add it to .env", flush=True)
    if not MSGR_VERIFY_TOKEN:
        print("[msgr] WARNING: MSGR_VERIFY_TOKEN empty — webhook verification "
              "will fail until you set it", flush=True)
    threading.Thread(target=_cart_loop, daemon=True).start()
    # Render sets $PORT and requires 0.0.0.0; local dev uses MSGR_PORT/127.0.0.1
    port = int(os.environ.get("PORT", os.environ.get("MSGR_PORT", "8765")))
    host = "0.0.0.0" if os.environ.get("PORT") else "127.0.0.1"
    server = ThreadingHTTPServer((host, port), WebhookHandler)
    print(f"[msgr] webhook listening on {host}:{port}/webhook", flush=True)
    print("[msgr] NOTE: Meta needs a PUBLIC HTTPS URL — point your Callback "
          "URL there (deploy or tunnel).", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
