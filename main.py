import os, sqlite3, html, requests, time
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN")
TRANSLATOR = os.getenv("TRANSLATOR", "libre")   # libre|deepl|google
LIBRE_URL = os.getenv("LIBRE_URL", "https://libretranslate.com")
DEFAULT_MODE = os.getenv("DEFAULT_MODE", "button")  # button|smart|dm
ALLOWED_LANGS = set(os.getenv("ALLOWED_LANGS", "ru,en,de,it,es,fr").split(","))

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher(bot)

# --- DB ---
conn = sqlite3.connect("bot.db", check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, lang TEXT)""")
cur.execute("""CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, mode TEXT DEFAULT 'button')""")
conn.commit()

def get_user_lang(uid):
    row = cur.execute("SELECT lang FROM users WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else None

def set_user_lang(uid, lang):
    cur.execute("""INSERT INTO users(user_id,lang) VALUES(?,?)
                   ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang""", (uid, lang))
    conn.commit()

def get_chat_mode(cid):
    row = cur.execute("SELECT mode FROM chats WHERE chat_id=?", (cid,)).fetchone()
    return row[0] if row else DEFAULT_MODE

def set_chat_mode(cid, mode):
    cur.execute("""INSERT INTO chats(chat_id,mode) VALUES(?,?)
                   ON CONFLICT(chat_id) DO UPDATE SET mode=excluded.mode""", (cid, mode))
    conn.commit()

# --- Translate ---
def detect_and_translate(text, target):
    text = (text or "").strip()
    if not text:
        return None, None
    if TRANSLATOR == "libre":
        try:
            dj = requests.post(f"{LIBRE_URL}/detect", data={"q": text}, timeout=10).json()
            src = dj[0]["language"] if dj else "auto"
            tj = requests.post(f"{LIBRE_URL}/translate",
                               data={"q": text, "source": src, "target": target}, timeout=15).json()
            return src, tj.get("translatedText")
        except Exception:
            return "auto", None
    # заглушки для deepl/google — можно добавить позже по желанию
    return "auto", text

# --- UI ---
def translate_button(message_id):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🌐 Перевести на мой язык", callback_data=f"tr:{message_id}"))
    return kb

# --- Commands ---
@dp.message_handler(commands=["start","help"])
async def start(m: types.Message):
    await m.reply(
        "Привет! Я многоязычный переводчик.\n"
        "1) Установи язык: <code>/setlang en</code>\n"
        "2) В группе админ задаёт режим: <code>/mode button</code> или <code>/mode smart</code>\n"
        "Режим <b>button</b> — по кнопке под сообщением (самый чистый)."
    )

@dp.message_handler(commands=["setlang"])
async def setlang(m: types.Message):
    parts = m.text.split()
    if len(parts) < 2:
        return await m.reply("Укажи язык: /setlang en\nДоступны: " + ", ".join(sorted(ALLOWED_LANGS)))
    lang = parts[1].lower()
    if lang not in ALLOWED_LANGS:
        return await m.reply("Недоступный язык. Доступно: " + ", ".join(sorted(ALLOWED_LANGS)))
    set_user_lang(m.from_user.id, lang)
    await m.reply(f"✅ Твой язык: <b>{lang}</b>")

@dp.message_handler(commands=["mode"])
async def mode(m: types.Message):
    if m.chat.type == "private":
        return await m.reply("Команда для групп.")
    parts = m.text.split()
    if len(parts) < 2 or parts[1] not in ("button","smart","dm"):
        return await m.reply("Используй: /mode button|smart|dm")
    set_chat_mode(m.chat.id, parts[1])
    await m.reply(f"✅ Режим группы: <b>{parts[1]}</b>")

# --- Group text handler ---
@dp.message_handler(content_types=types.ContentType.TEXT)
async def on_text(m: types.Message):
    if m.chat.type == "private":  # не засоряем ЛС
        return
    if m.text.startswith("/"):    # игнорируем команды
        return
    if len(m.text.strip()) < 2:
        return

    mode = get_chat_mode(m.chat.id)
    if mode == "button":
        await bot.send_message(
            m.chat.id, "Нужен перевод? Нажми кнопку👇",
            reply_to_message_id=m.message_id, reply_markup=translate_button(m.message_id)
        )
        return

    # smart/dm — минимальная логика примера
    src, _ = detect_and_translate(m.text, list(ALLOWED_LANGS)[0])
    if not src:
        return
    targets = [lng for lng in ALLOWED_LANGS if lng != src]

    if mode == "smart":
        chunks = []
        for lng in sorted(targets):
            _, tr = detect_and_translate(m.text, lng)
            if tr:
                chunks.append(f"▪️ <b>{lng}</b>: <span class=\"tg-spoiler\">{html.escape(tr)}</span>")
                time.sleep(0.2)
        if chunks:
            await bot.send_message(m.chat.id, "🤖 Перевод:\n" + "\n".join(chunks),
                                   reply_to_message_id=m.message_id)
        return

    if mode == "dm":
        ulang = get_user_lang(m.from_user.id)
        if ulang and ulang != src:
            _, tr = detect_and_translate(m.text, ulang)
            if tr:
                try:
                    await bot.send_message(m.from_user.id, f"💬 ({src}→{ulang}): {tr}")
                except Exception:
                    pass

# --- Button callback ---
@dp.callback_query_handler(lambda c: c.data.startswith("tr:"))
async def on_tr_button(cb: types.CallbackQuery):
    user_lang = get_user_lang(cb.from_user.id)
    if not user_lang:
        return await cb.answer("Сначала /setlang xx", show_alert=True)

    # кнопка висит как ответ на исходник, достанем текст
    src_msg = cb.message.reply_to_message
    if not src_msg or not (src_msg.text or src_msg.caption):
        return await cb.answer("Нет текста для перевода", show_alert=True)
    text = src_msg.text or src_msg.caption

    src, tr = detect_and_translate(text, user_lang)
    if not tr:
        return await cb.answer("Не удалось перевести", show_alert=True)

    # ответ под исходным, со спойлером — чтобы чат был чистым
    nick = (cb.from_user.first_name or "User")
    await bot.send_message(
        cb.message.chat.id,
        f"🤖 [{html.escape(nick)}] <span class=\"tg-spoiler\">{html.escape(tr)}</span>",
        reply_to_message_id=src_msg.message_id
    )
    await cb.answer("Готово ✅")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
