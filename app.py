"""
Fanatick WhatsApp Delivery Agent
Connected to Supabase — reads/writes deliveries table in real time
Dashboard: Lovable app connected to same Supabase
"""

import os
import json
import base64
import logging
import requests
from flask import Flask, request, Response
from twilio.rest import Client
from openai import OpenAI
from supabase import create_client, Client as SupabaseClient

# ============================================================
#  CONFIG
# ============================================================

TWILIO_ACCOUNT_SID  = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_AUTH_TOKEN   = os.environ["TWILIO_AUTH_TOKEN"]
TWILIO_NUMBER       = os.environ.get("TWILIO_NUMBER", "whatsapp:+14155238886")
OPENAI_API_KEY      = os.environ["OPENAI_API_KEY"]
SUPABASE_URL        = os.environ.get("SUPABASE_URL", "https://igrlhrtjcmippqilqgyx.supabase.co")
SUPABASE_KEY        = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TRADER_NUMBER       = os.environ.get("TRADER_NUMBER", "whatsapp:+447451295914")
PORT                = int(os.environ.get("PORT", 5000))

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
openai_client = OpenAI(api_key=OPENAI_API_KEY)
supabase: SupabaseClient = create_client(SUPABASE_URL, SUPABASE_KEY)
app = Flask(__name__)

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ============================================================
#  SUPABASE HELPERS
# ============================================================

def get_delivery_by_phone(phone):
    clean = phone.replace("whatsapp:", "")
    result = supabase.table("deliveries")\
        .select("*")\
        .eq("client_whatsapp", clean)\
        .not_.in_("status", ["removed"])\
        .order("created_at", desc=True)\
        .limit(1)\
        .execute()
    return result.data[0] if result.data else None

def update_delivery(delivery_id, data):
    supabase.table("deliveries").update(data).eq("id", delivery_id).execute()
    log.info(f"Updated delivery {delivery_id}: {data}")

def save_proof(delivery_id, proof_url, proof_type="screenshot"):
    supabase.table("delivery_proofs").insert({
        "delivery_id": delivery_id,
        "proof_url": proof_url,
        "proof_type": proof_type
    }).execute()

# ============================================================
#  TWILIO HELPERS
# ============================================================

def send_msg(to, body):
    try:
        num = to if to.startswith("whatsapp:") else f"whatsapp:{to}"
        twilio_client.messages.create(from_=TWILIO_NUMBER, to=num, body=body)
    except Exception as e:
        log.error(f"Send error: {e}")

def notify_trader(body):
    send_msg(TRADER_NUMBER, body)

# ============================================================
#  GPT HELPERS
# ============================================================

def analyze_image(image_url, prompt):
    try:
        r = requests.get(image_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        b64 = base64.b64encode(r.content).decode("utf-8")
        ct = r.headers.get("Content-Type", "image/jpeg")
        res = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:{ct};base64,{b64}"}}
            ]}],
            max_tokens=200
        )
        raw = res.choices[0].message.content.strip()
        if "```" in raw:
            raw = raw.split("```")[1].replace("json","").strip()
        return json.loads(raw)
    except Exception as e:
        log.error(f"Image error: {e}")
        return None

def check_wallet(image_url):
    result = analyze_image(image_url,
        'Does this screenshot show tickets in Apple Wallet or Google Wallet? '
        'Reply JSON only: {"confirmed": true/false, "notes": "brief"}')
    return result or {"confirmed": False, "notes": "Could not analyze"}

def check_removal(image_url):
    result = analyze_image(image_url,
        'Does this show Arsenal tickets have been removed/deleted from Apple or Google Wallet? '
        'Reply JSON only: {"removed": true/false, "notes": "brief"}')
    return result or {"removed": False, "notes": "Could not analyze"}

def get_intent(message):
    m = message.lower().strip()
    if any(w in m for w in ["iphone", "apple", "ios"]):         return "iphone"
    if any(w in m for w in ["android", "samsung", "google", "pixel", "huawei"]):  return "android"
    if any(w in m for w in ["✅", "yes", "ok", "done", "got it", "understood", "ready", "sure", "yep", "added"]):  return "confirmed"
    if any(w in m for w in ["help", "can't", "cant", "stuck", "how", "not working", "don't see"]):  return "confused"
    if any(w in m for w in ["wrong", "incorrect", "mistake", "different seat", "wrong link"]): return "wrong"
    return "other"

# ============================================================
#  FLOW HANDLERS
# ============================================================

def handle_phone_detect(d, from_num, msg, media):
    intent = get_intent(msg)
    if intent == "iphone":
        update_delivery(d["id"], {"status": "briefed", "phone_type": "iphone"})
        send_msg(from_num,
            "Perfect 🍎 Before I send your ticket link, please read this:\n\n"
            "⚠️ *Important:*\n"
            "• Do NOT share the link\n"
            "• Add to *Apple Wallet* immediately\n"
            "• Keep until after the game\n"
            "• Remove after full time\n\n"
            "Reply ✅ when ready")
    elif intent == "android":
        update_delivery(d["id"], {"status": "briefed", "phone_type": "android"})
        send_msg(from_num,
            "Perfect 🤖 Before I send your ticket link, please read this:\n\n"
            "⚠️ *Important:*\n"
            "• Do NOT share the link\n"
            "• Add to *Google Wallet* immediately\n"
            "• Keep until after the game\n"
            "• Remove after full time\n\n"
            "Reply ✅ when ready")
    else:
        send_msg(from_num, "Are you on *iPhone* or *Android*? 📱")

def handle_briefed(d, from_num, msg, media):
    if get_intent(msg) != "confirmed":
        send_msg(from_num, "Please reply ✅ when you've read the instructions 👆")
        return

    links = d.get("links") or ""
    if isinstance(links, list):
        link_list = links
    else:
        link_list = [l.strip() for l in links.split("\n") if l.strip()]

    wallet = "Apple Wallet" if d.get("phone_type") == "iphone" else "Google Wallet"
    links_text = "\n".join([f"🎫 Ticket {i+1}:\n{l}" for i, l in enumerate(link_list)])

    send_msg(from_num,
        f"Here are your ticket links:\n\n{links_text}\n\n"
        f"For each:\n1️⃣ Tap the link\n2️⃣ Add to *{wallet}*\n"
        f"3️⃣ Send me a screenshot of your wallet 📸")
    update_delivery(d["id"], {"status": "links_sent"})

def handle_links_sent(d, from_num, msg, media):
    intent = get_intent(msg)

    if media:
        result = check_wallet(media)
        save_proof(d["id"], media, "wallet_screenshot")
        if result.get("confirmed"):
            update_delivery(d["id"], {"status": "wallet_confirmed"})
            send_msg(from_num,
                f"✅ *Confirmed!* Tickets are in your wallet.\n\n"
                f"Enjoy {d.get('game_name', 'the game')}! 🏟️⚽\n\n"
                f"I'll message you after full time to remind you to remove the tickets.")
            notify_trader(
                f"✅ Wallet confirmed\n"
                f"Client: {d.get('client_name')}\n"
                f"Order: #{d.get('order_number')}\n"
                f"Game: {d.get('game_name')}")
        else:
            send_msg(from_num,
                "Hmm, I can't confirm the tickets in that screenshot.\n"
                "Make sure all tickets are visible and send another 📸")

    elif intent == "confused":
        wallet = "Apple Wallet" if d.get("phone_type") == "iphone" else "Google Wallet"
        if d.get("phone_type") == "iphone":
            send_msg(from_num,
                "Try this:\n1. Open link in *Safari*\n2. Scroll down → *Add to Apple Wallet*\n"
                "3. Tap *Add*\n\nStill stuck? Send a screenshot 📱")
        else:
            send_msg(from_num,
                "Try this:\n1. Open link in *Chrome*\n2. Scroll down → *Save to Google Wallet*\n"
                "3. Tap *Save*\n\nStill stuck? Send a screenshot 📱")

    elif intent == "wrong":
        notify_trader(
            f"⚠️ Wrong link!\nClient: {d.get('client_name')}\n"
            f"Order: #{d.get('order_number')}\nNumber: {d.get('client_whatsapp')}\n\n"
            f"Fix: RESEND {d.get('client_whatsapp')} | new_link1 | new_link2")
        send_msg(from_num, "Sorry! 🙏 Flagged with the team — correct link coming shortly.")

    else:
        send_msg(from_num, "Once added, send me a screenshot of your wallet 📸")

def handle_wallet_confirmed(d, from_num, msg, media):
    if media:
        result = check_removal(media)
        save_proof(d["id"], media, "removal_proof")
        if result.get("removed"):
            update_delivery(d["id"], {"status": "removed"})
            send_msg(from_num, "✅ Tickets removed. Thanks, hope you enjoyed the game! 🙌")
            notify_trader(
                f"✅ Removal confirmed\nClient: {d.get('client_name')}\n"
                f"Order: #{d.get('order_number')} — complete ✓")
        else:
            send_msg(from_num,
                "Can't confirm removal. Please delete from wallet and send a screenshot 📱")
    else:
        send_msg(from_num,
            "👋 Game over — please *remove your tickets from your wallet* now.\n"
            "Send me a screenshot confirming removal 📸")

# ============================================================
#  TRADER COMMANDS
# ============================================================

def handle_trader(msg):
    m = msg.strip()

    if m.upper() == "HELP":
        send_msg(TRADER_NUMBER,
            "🤖 *Commands:*\n"
            "STATUS — active deliveries\n"
            "RESEND +44xxx | link1 | link2 — fix wrong links\n"
            "GAMEOVER +44xxx — trigger removal chase\n"
            "CANCEL +44xxx — cancel delivery")
        return

    if m.upper() == "STATUS":
        res = supabase.table("deliveries")\
            .select("client_name,game_name,status,order_number")\
            .not_.in_("status", ["removed"])\
            .execute()
        if not res.data:
            send_msg(TRADER_NUMBER, "No active deliveries.")
            return
        lines = ["📊 *Active deliveries:*\n"]
        for d in res.data:
            lines.append(f"• {d['client_name']} — {d['game_name']}\n  {d['status']} | #{d['order_number']}")
        send_msg(TRADER_NUMBER, "\n".join(lines))
        return

    if m.upper().startswith("RESEND"):
        try:
            parts = m.split("|")
            phone = parts[0].replace("RESEND", "").strip()
            new_links = [p.strip() for p in parts[1:]]
            d = get_delivery_by_phone(phone)
            if d:
                update_delivery(d["id"], {"links": "\n".join(new_links), "status": "links_sent"})
                links_text = "\n".join([f"🎫 Ticket {i+1}:\n{l}" for i, l in enumerate(new_links)])
                send_msg(f"whatsapp:{phone}",
                    f"Sorry for the mix-up! 🙏 Here are your correct links:\n\n{links_text}\n\n"
                    f"Please add to your wallet and send a screenshot ✅")
                send_msg(TRADER_NUMBER, f"✅ Correct links sent to {d.get('client_name')}")
            else:
                send_msg(TRADER_NUMBER, f"No active delivery for {phone}")
        except:
            send_msg(TRADER_NUMBER, "Format: RESEND +44xxx | link1 | link2")
        return

    if m.upper().startswith("GAMEOVER"):
        phone = m.replace("GAMEOVER", "").strip()
        d = get_delivery_by_phone(f"whatsapp:{phone}")
        if d:
            send_msg(f"whatsapp:{phone}",
                "👋 The game is over — please *remove your tickets from your wallet* now.\n"
                "Send me a screenshot confirming removal 📸")
            send_msg(TRADER_NUMBER, f"✅ Removal chase sent to {d.get('client_name')}")
        else:
            send_msg(TRADER_NUMBER, f"No active delivery for {phone}")
        return

    if m.upper().startswith("CANCEL"):
        phone = m.replace("CANCEL", "").strip()
        d = get_delivery_by_phone(f"whatsapp:{phone}")
        if d:
            update_delivery(d["id"], {"status": "removed"})
            send_msg(TRADER_NUMBER, f"✅ Delivery cancelled for {d.get('client_name')}")
        else:
            send_msg(TRADER_NUMBER, f"No active delivery for {phone}")
        return

# ============================================================
#  WEBHOOK
# ============================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    from_num = request.form.get("From", "")
    body     = request.form.get("Body", "").strip()
    media    = request.form.get("MediaUrl0")

    log.info(f"From: {from_num} | Body: {body[:60]} | Media: {bool(media)}")

    if from_num == TRADER_NUMBER:
        handle_trader(body)
        return Response("", 200)

    delivery = get_delivery_by_phone(from_num)
    if not delivery:
        send_msg(from_num,
            "Hi! This is Fanatick ticket delivery. "
            "If you're expecting tickets, your delivery will start shortly 🎫")
        return Response("", 200)

    status = delivery.get("status", "phone_detected")

    if status == "phone_detected":
        handle_phone_detect(delivery, from_num, body, media)
    elif status == "briefed":
        handle_briefed(delivery, from_num, body, media)
    elif status == "links_sent":
        handle_links_sent(delivery, from_num, body, media)
    elif status == "wallet_confirmed":
        handle_wallet_confirmed(delivery, from_num, body, media)
    elif status == "removed":
        send_msg(from_num, "Your delivery is complete! Thanks 🙏")

    return Response("", 200)

@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200

@app.route("/", methods=["GET"])
def home():
    return {"status": "Fanatick Delivery Agent running"}, 200

# ============================================================
#  MAIN
# ============================================================

if __name__ == "__main__":
    log.info("🚀 Fanatick Delivery Agent starting...")
    app.run(host="0.0.0.0", port=PORT)
