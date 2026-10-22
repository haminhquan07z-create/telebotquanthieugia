import os, re, io, time, sqlite3, random, asyncio
from datetime import datetime, timedelta, time as dtime
import requests, yt_dlp, qrcode
import google.generativeai as genai
from telegram import Update, ChatPermissions
from telegram.constants import ChatType
from telegram.ext import Application, CommandHandler, MessageHandler, ChatMemberHandler, ContextTypes, filters

BOT_TOKEN = os.getenv("BOT_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_KEY")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
MAX_FILE_MB = 45

genai.configure(api_key=GEMINI_KEY)
MODEL = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction="Trả lời ngắn gọn, tự nhiên bằng tiếng Việt. Xưng 'mình', gọi 'bạn'.")

DB = sqlite3.connect("bot.db", check_same_thread=False)
DB.execute("CREATE TABLE IF NOT EXISTS history (user_id INTEGER, role TEXT, content TEXT, ts INTEGER)")
DB.execute("CREATE TABLE IF NOT EXISTS notify (chat_id INTEGER PRIMARY KEY)")
DB.commit()
chat_cache = {}

def load_history(uid):
    rows = DB.execute("SELECT role, content FROM history WHERE user_id=? ORDER BY ts DESC LIMIT 20", (uid,)).fetchall()
    return [{"role": r, "parts": [c]} for r, c in reversed(rows)]

def save_msg(uid, role, content):
    DB.execute("INSERT INTO history VALUES (?,?,?,?)", (uid, role, content, int(time.time())))
    DB.commit()

def get_chat(uid):
    if uid not in chat_cache: chat_cache[uid] = MODEL.start_chat(history=load_history(uid))
    return chat_cache[uid]

async def ask_ai(uid, prompt):
    try:
        resp = await asyncio.to_thread(get_chat(uid).send_message, prompt)
        text = (resp.text or "").strip() or "Hmm, mình không nghĩ ra."
        save_msg(uid, "user", prompt); save_msg(uid, "model", text)
        return text
    except Exception as e: return f"❌ Lỗi AI: {e}"

settings, warns, user_msg_times, guess_targets = {}, {}, {}, {}

def get_settings(cid):
    if cid not in settings: settings[cid] = {"anti_link": False, "anti_flood": False}
    return settings[cid]

def is_admin(update):
    if update.effective_chat.type == ChatType.PRIVATE or (OWNER_ID and update.effective_user.id == OWNER_ID): return True
    try:
        m = update.effective_chat.get_member(update.effective_user.id)
        return m.status in ("administrator", "creator")
    except: return False

def get_target(update):
    return update.message.reply_to_message.from_user if update.message.reply_to_message else None

async def start(update, ctx):
    await update.message.reply_text(f"👋 Chào {update.effective_user.first_name}!\n📌 /help – xem lệnh\n💬 Chat riêng: nhắn gì cũng được AI trả lời.")

async def help_cmd(update, ctx):
    await update.message.reply_markdown("🤖 *AI*\n/ai <câu hỏi> | /reset\n\n🎵 *Media*\n/mp3 <link> | /mp4 <link>\n\n🎮 *Game*\n/roll | /coin | /guess | /g <số>\n\n💰 *Tiện ích*\n/price <coin> | /weather <tp> | /qr <text> | /tr <lang> <text>\n/id | /ping | /time\n\n🛡 *Group (admin)*\n/antilink on|off | /antiflood on|off\n/warn | /mute [phút] | /unmute | /kick | /ban (reply tin)\n/setnotify – bật/tắt báo giá 9h sáng")

async def reset(update, ctx):
    chat_cache.pop(update.effective_user.id, None); DB.execute("DELETE FROM history WHERE user_id=?", (update.effective_user.id,)); DB.commit()
    await update.message.reply_text("🧹 Đã xoá lịch sử chat.")

async def ping(update, ctx):
    t0 = time.time(); m = await update.message.reply_text("🏓 ...")
    await m.edit_text(f"🏓 Pong `{(time.time()-t0)*1000:.0f}ms`", parse_mode="Markdown")

async def get_id(update, ctx): await update.message.reply_text(f"👤 `{update.effective_user.id}`\n💬 `{update.effective_chat.id}`", parse_mode="Markdown")
async def time_cmd(update, ctx): await update.message.reply_text(datetime.now().strftime("🕒 %H:%M:%S – %d/%m/%Y"))

async def ai_cmd(update, ctx):
    q = " ".join(ctx.args).strip()
    if not q: return await update.message.reply_text("Dùng: /ai <câu hỏi>")
    await update.message.chat.send_action("typing"); ans = await ask_ai(update.effective_user.id, q)
    for i in range(0, len(ans), 4000): await update.message.reply_text(ans[i:i+4000])

async def on_text(update, ctx):
    if update.effective_chat.type != ChatType.PRIVATE: return
    await update.message.chat.send_action("typing"); ans = await ask_ai(update.effective_user.id, update.message.text)
    for i in range(0, len(ans), 4000): await update.message.reply_text(ans[i:i+4000])

def _dl_sync(url, audio, uid):
    opts = {'outtmpl': f'{DOWNLOAD_DIR}/{uid}_%(id)s.%(ext)s', 'noplaylist': True, 'quiet': True, 'no_warnings': True}
    if audio: opts.update({'format': 'bestaudio/best', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}]})
    else: opts['format'] = f'best[filesize<{MAX_FILE_MB}M]/best'
    if os.path.exists('cookies.txt'): opts['cookiefile'] = 'cookies.txt'
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True); fn = ydl.prepare_filename(info)
        return (os.path.splitext(fn)[0] + '.mp3') if audio else fn, info.get('title', 'media')

async def download_cmd(update, ctx, audio=True):
    if not ctx.args: return await update.message.reply_text(f"Dùng: /{'mp3' if audio else 'mp4'} <link>")
    url = ctx.args[0]
    if not re.match(r'https?://', url): return await update.message.reply_text("❌ Link không hợp lệ.")
    s = await update.message.reply_text("⏳ Đang tải...")
    try:
        fp, title = await asyncio.to_thread(_dl_sync, url, audio, update.effective_user.id)
        if os.path.getsize(fp)/1024/1024 > MAX_FILE_MB: os.remove(fp); return await s.edit_text(f"❌ File quá {MAX_FILE_MB}MB.")
        await s.edit_text("📤 Đang gửi...")
        with open(fp, 'rb') as f:
            if audio: await update.message.reply_audio(f, title=title, filename=os.path.basename(fp))
            else: await update.message.reply_video(f, caption=title, filename=os.path.basename(fp))
        os.remove(fp); await s.delete()
    except Exception as e: await s.edit_text(f"❌ Lỗi: {e}\n(YouTube có thể chặn IP, thử TikTok)")

async def mp3_cmd(update, ctx): await download_cmd(update, ctx, True)
async def mp4_cmd(update, ctx): await download_cmd(update, ctx, False)
async def roll(update, ctx): await update.message.reply_text(f"🎲 {random.randint(1,6)}")
async def coin(update, ctx): await update.message.reply_text(random.choice(["🪙 Ngửa","🪙 Sấp"]))

async def guess(update, ctx):
    guess_targets[update.effective_user.id] = random.randint(1, 100)
    await update.message.reply_text("🎯 Mình nghĩ 1 số 1-100. Đoán: /g <số>")

async def guess_num(update, ctx):
    uid = update.effective_user.id
    if uid not in guess_targets: return await update.message.reply_text("Gõ /guess trước!")
    if not ctx.args or not ctx.args[0].isdigit(): return await update.message.reply_text("Dùng: /g <số>")
    n, t = int(ctx.args[0]), guess_targets[uid]
    if n == t: guess_targets.pop(uid); await update.message.reply_text(f"🎉 Đúng! Số đó là {t}.")
    elif n < t: await update.message.reply_text("📈 Lớn hơn!")
    else: await update.message.reply_text("📉 Nhỏ hơn!")

async def price(update, ctx):
    if not ctx.args: return await update.message.reply_text("Dùng: /price <coin> (btc, eth, sol...)")
    sym = ctx.args[0].upper() + "USDT"
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/24hr?symbol={sym}", timeout=10)
        if r.status_code != 200: return await update.message.reply_text(f"❌ Không thấy {ctx.args[0]}.")
        d = r.json(); ch = float(d['priceChangePercent']); a = "🟢" if ch >= 0 else "🔴"
        await update.message.reply_text(f"💰 *{sym}*\nGiá: `${float(d['lastPrice']):,.4f}`\n24h: {a} `{ch:+.2f}%`\nCao: `${float(d['highPrice']):,.4f}` | Thấp: `${float(d['lowPrice']):,.4f}`", parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"❌ {e}")

async def weather(update, ctx):
    if not ctx.args: return await update.message.reply_text("Dùng: /weather <thành phố>\nVD: /weather Hanoi")
    city = " ".join(ctx.args)
    try:
        g = requests.get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1, "language": "vi"}, timeout=10).json()
        if not g.get("results"): return await update.message.reply_text("❌ Không tìm thấy thành phố.")
        loc = g["results"][0]
        w = requests.get("https://api.open-meteo.com/v1/forecast", params={"latitude": loc["latitude"], "longitude": loc["longitude"], "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m"}, timeout=10).json()["current"]
        codes = {0:"☀️ Trời quang",1:"🌤 Ít mây",2:"⛅ Có mây",3:"☁️ Nhiều mây",45:"🌫 Sương mù",51:"🌦 Mưa nhẹ",61:"🌧 Mưa",63:"🌧 Mưa vừa",65:"⛈ Mưa to",71:"🌨 Tuyết",80:"🌦 Mưa rào",95:"⛈ Giông"}
        await update.message.reply_text(f"🌍 *{loc['name']}, {loc.get('country','')}*\n{codes.get(w['weather_code'], '🌡 Không rõ')}\n🌡 {w['temperature_2m']}°C | 💧 {w['relative_humidity_2m']}%\n💨 Gió {w['wind_speed_10m']} km/h", parse_mode="Markdown")
    except Exception as e: await update.message.reply_text(f"❌ {e}")

async def qr(update, ctx):
    text = " ".join(ctx.args).strip()
    if not text: return await update.message.reply_text("Dùng: /qr <text hoặc link>")
    img = qrcode.make(text); buf = io.BytesIO(); img.save(buf, 'PNG'); buf.seek(0)
    await update.message.reply_photo(buf, caption=f"QR: {text[:100]}")

async def translate_cmd(update, ctx):
    if len(ctx.args) < 2: return await update.message.reply_text("Dùng: /tr <ngôn ngữ> <text>\nVD: /tr en xin chào")
    await update.message.chat.send_action("typing")
    ans = await ask_ai(update.effective_user.id, f"Dịch đoạn sau sang {ctx.args[0]}, chỉ trả lời bản dịch, không giải thích: {' '.join(ctx.args[1:])}")
    await update.message.reply_text(ans)

async def toggle_anti(update, ctx, key):
    if not is_admin(update): return await update.message.reply_text("❌ Chỉ admin.")
    if not ctx.args or ctx.args[0].lower() not in ("on","off"): return await update.message.reply_text(f"Dùng: /{key} on|off")
    get_settings(update.effective_chat.id)[key] = (ctx.args[0].lower() == "on")
    await update.message.reply_text(f"✅ {key}: {ctx.args[0].lower()}")

async def antilink(update, ctx): await toggle_anti(update, ctx, "anti_link")
async def antiflood(update, ctx): await toggle_anti(update, ctx, "anti_flood")

async def warn(update, ctx):
    if not is_admin(update): return await update.message.reply_text("❌ Admin.")
    t = get_target(update)
    if not t: return await update.message.reply_text("Reply tin cần warn.")
    cid = update.effective_chat.id; warns.setdefault(cid, {}); warns[cid][t.id] = warns[cid].get(t.id, 0) + 1; n = warns[cid][t.id]
    if n >= 3: warns[cid][t.id] = 0; await update.effective_chat.ban_member(t.id); await update.message.reply_text(f"🚫 {t.first_name} warn 3/3 → kick.")
    else: await update.message.reply_text(f"⚠️ {t.first_name} warn {n}/3.")

async def mute(update, ctx):
    if not is_admin(update): return await update.message.reply_text("❌ Admin.")
    t = get_target(update)
    if not t: return await update.message.reply_text("Reply tin cần mute.")
    mins = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 60
    await update.effective_chat.restrict_member(t.id, ChatPermissions(can_send_messages=False), until_date=datetime.now() + timedelta(minutes=mins))
    await update.message.reply_text(f"🔇 {t.first_name} mute {mins} phút.")

async def unmute(update, ctx):
    if not is_admin(update): return await update.message.reply_text("❌ Admin.")
    t = get_target(update)
    if not t: return await update.message.reply_text("Reply tin cần unmute.")
    await update.effective_chat.restrict_member(t.id, ChatPermissions(can_send_messages=True, can_send_media_messages=True, can_send_other_messages=True, can_add_web_page_previews=True))
    await update.message.reply_text(f"🔊 {t.first_name} unmute.")

async def kick(update, ctx):
    if not is_admin(update): return await update.message.reply_text("❌ Admin.")
    t = get_target(update)
    if not t: return await update.message.reply_text("Reply tin cần kick.")
    await update.effective_chat.ban_member(t.id); await update.effective_chat.unban_member(t.id)
    await update.message.reply_text(f"👢 Kick {t.first_name}.")

async def ban(update, ctx):
    if not is_admin(update): return await update.message.reply_text("❌ Admin.")
    t = get_target(update)
    if not t: return await update.message.reply_text("Reply tin cần ban.")
    await update.effective_chat.ban_member(t.id); await update.message.reply_text(f"🚫 Ban {t.first_name}.")

async def moderate(update, ctx):
    if update.effective_chat.type == ChatType.PRIVATE: return
    cid = update.effective_chat.id; s = get_settings(cid); msg = update.message
    if not msg or not msg.from_user or msg.from_user.is_bot: return
    try:
        m = await update.effective_chat.get_member(msg.from_user.id)
        if m.status in ("administrator","creator"): return
    except: return
    if s.get("anti_link"):
        if re.search(r'(https?://|t\.me/|www\.)', msg.text or msg.caption or "", re.I):
            await msg.delete(); await ctx.bot.send_message(cid, f"⚠️ {msg.from_user.first_name}, không gửi link!"); return
    if s.get("anti_flood"):
        now = time.time(); arr = user_msg_times.setdefault(cid, {}).setdefault(msg.from_user.id, []); arr[:] = [t for t in arr if now - t < 5] + [now]
        if len(arr) > 5:
            await update.effective_chat.restrict_member(msg.from_user.id, ChatPermissions(can_send_messages=False), until_date=datetime.now() + timedelta(minutes=5))
            await ctx.bot.send_message(cid, f"🔇 {msg.from_user.first_name} mute 5' vì spam.")

async def welcome(update, ctx):
    r = update.chat_member
    if r.new_chat_member.status == "member" and r.old_chat_member.status in ("left","kicked"):
        await ctx.bot.send_message(r.chat.id, f"👋 Chào {r.new_chat_member.user.first_name}! /help xem lệnh.")

async def daily_job(ctx):
    rows = DB.execute("SELECT chat_id FROM notify").fetchall()
    if not rows: return
    try:
        r = requests.get('https://api.binance.com/api/v3/ticker/24hr?symbols=["BTCUSDT","ETHUSDT","SOLUSDT"]', timeout=10)
        data = {d['symbol']: d for d in r.json()}; text = "📊 *Báo giá hàng ngày*\n"
        for s in ("BTCUSDT","ETHUSDT","SOLUSDT"):
            if s in data: d = data[s]; ch = float(d['priceChangePercent']); text += f"\n{s}: `${float(d['lastPrice']):,.2f}` {'🟢' if ch >= 0 else '🔴'} {ch:+.2f}%"
        for (cid,) in rows: await ctx.bot.send_message(cid, text, parse_mode="Markdown")
    except: pass

async def setnotify(update, ctx):
    if not is_admin(update): return await update.message.reply_text("❌ Admin.")
    cid = update.effective_chat.id; ex = DB.execute("SELECT 1 FROM notify WHERE chat_id=?", (cid,)).fetchone()
    if ex: DB.execute("DELETE FROM notify WHERE chat_id=?", (cid,)); DB.commit(); await update.message.reply_text("🔕 Đã tắt thông báo.")
    else: DB.execute("INSERT INTO notify VALUES (?)", (cid,)); DB.commit(); await update.message.reply_text("🔔 Đã bật thông báo 9h sáng.")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    cmds = {"start": start, "help": help_cmd, "reset": reset, "ping": ping, "id": get_id, "time": time_cmd, "ai": ai_cmd, "mp3": mp3_cmd, "mp4": mp4_cmd, "roll": roll, "coin": coin, "guess": guess, "g": guess_num, "price": price, "weather": weather, "qr": qr, "tr": translate_cmd, "antilink": antilink, "antiflood": antiflood, "warn": warn, "mute": mute, "unmute": unmute, "kick": kick, "ban": ban, "setnotify": setnotify}
    for name, fn in cmds.items(): app.add_handler(CommandHandler(name, fn))
    app.add_handler(ChatMemberHandler(welcome, ChatMemberHandler.CHAT_MEMBER))
    async def dispatch(update, ctx):
        if update.effective_chat.type == ChatType.PRIVATE: await on_text(update, ctx)
        else: await moderate(update, ctx)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, dispatch))
    if app.job_queue: app.job_queue.run_daily(daily_job, time=dtime(hour=2, minute=0))
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__": main()
