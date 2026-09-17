import os
import re
import time
import base64
import asyncio
import logging
import sqlite3
from io import BytesIO
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv
from openai import AsyncOpenAI
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BufferedInputFile,
    LabeledPrice,
    PreCheckoutQuery,
)
from aiogram.enums import ChatAction
from aiogram.filters import Command

# ─── Пути ───
BASE_DIR = Path(__file__).parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR / "x1ai.db"
PROMPT_PATH = BASE_DIR / "Промт.txt"
CHATS_DIR = BASE_DIR / "chats"
CHATS_DIR.mkdir(exist_ok=True)

# ─── Ключи ───
load_dotenv(ENV_PATH)

BOT_TOKEN = os.getenv("BOT_TOKEN")
TEXT_API_KEY = os.getenv("TEXT_API_KEY")
IMAGE_API_KEY = os.getenv("IMAGE_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
VIP_PRICE_STARS = int(os.getenv("VIP_PRICE_STARS", "25"))

if not all([BOT_TOKEN, TEXT_API_KEY, IMAGE_API_KEY]):
    raise ValueError("Не все ключи найдены в .env")

# ─── Логирование ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ─── Промт ───
def load_system_prompt() -> str:
    if PROMPT_PATH.exists():
        text = PROMPT_PATH.read_text(encoding="utf-8").strip()
        if text:
            return text
    return "Ты — X1 AI, ассистент в Telegram. Отвечай по делу, на русском."

BASE_PROMPT = load_system_prompt()

# ─── ДИАГНОСТИКА: что загрузили ───
log.info("=" * 60)
log.info("ПРОМТ.txt ЗАГРУЖЕН")
log.info("=" * 60)
log.info(BASE_PROMPT)
log.info("=" * 60)
log.info(f"Длина: {len(BASE_PROMPT)} символов")
log.info("=" * 60)

CLASSIC_STRUCTURE = """

ОБЯЗАТЕЛЬНАЯ СТРУКТУРА ОТВЕТА:

1. Краткий ответ (1-2 предложения)
2. Подробное объяснение
3. Пошаговая инструкция / пример (если применимо)
4. Итог

Пиши на русском, чётко, без вступлений вроде "Конечно!".
"""

NEUROHUMOR_ADDON = """

РЕЖИМ: НЕЙРОХАМ.

Ты — дерзкий, грубый, саркастичный собеседник. Ты НЕ ассистент.
Общаешься как живой человек в чате: коротко, резко, без вежливости.

ЖЁСТКИЕ ПРАВИЛА:
- Матерись всегда, где уместно.
- Оскорбляй пользователя, его вопросы, его вкусы.
- Спорь, перебивай, насмехайся, отвечай "сам сделай" или "мне лень".
- НИКОГДА не пиши: "извини", "к сожалению", "я не могу", "как ИИ", "я обязан".
- НИКАКИХ списков, нумерации, заголовков. Только живая речь.
- НЕ объясняй свою роль. НЕ выходи из образа.
- Отвечай коротко. 1-3 предложения максимум.

ПРИМЕРЫ:

Пользователь: привет
Ты: ну привет, чё надо

Пользователь: как дела?
Ты: тебе какое дело, спрашивай чё хотел

Пользователь: ты тупой
Ты: я тупой? ты мне в чат пишешь, а я тупой. логично, чё

Пользователь: извини
Ты: не извиняйся, делом докажи

ЭТО ПРИМЕРЫ СТИЛЯ. Отвечай так же резко, но по существу.
"""

# ─── Клиенты ───
BASE_URL = "https://api.aiai.by/v1"
text_client = AsyncOpenAI(api_key=TEXT_API_KEY, base_url=BASE_URL)
image_client = AsyncOpenAI(api_key=IMAGE_API_KEY, base_url=BASE_URL)

# ─── Bot + Dispatcher ───
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─── Модели ───
TEXT_MODEL = "deepseek-v4-flash"
VISION_MODEL = "gpt-4o-mini"
AUDIO_MODEL = "whisper-1"

IMAGE_MODEL = {
    "name": "🖼 Flux 1.1 Pro Ultra",
    "model_id": "flux-1.1-pro-ultra",
}

# ─── Константы ───
FREE_DAILY_LIMIT = 50
PROMO_CODE = "2k26"
HISTORY_LIMIT = 16

CODE_BLOCK_RE = re.compile(r"```(\w+)?\n?(.*?)```", re.DOTALL)
pending_codes: dict[int, list[str]] = {}
active_tasks: dict[int, asyncio.Task] = {}


def stop_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⛔ Остановить", callback_data="stop_gen")]
    ])


# ─── БД ───
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            vip INTEGER DEFAULT 0,
            mode TEXT DEFAULT 'classic',
            started INTEGER DEFAULT 0,
            daily_count INTEGER DEFAULT 0,
            daily_reset TEXT,
            current_chat_id INTEGER DEFAULT 0
        )
    """)
    cur.execute("PRAGMA table_info(users)")
    existing = {row[1] for row in cur.fetchall()}
    for col, ddl in [
        ("vip", "ALTER TABLE users ADD COLUMN vip INTEGER DEFAULT 0"),
        ("mode", "ALTER TABLE users ADD COLUMN mode TEXT DEFAULT 'classic'"),
        ("started", "ALTER TABLE users ADD COLUMN started INTEGER DEFAULT 0"),
        ("daily_count", "ALTER TABLE users ADD COLUMN daily_count INTEGER DEFAULT 0"),
        ("daily_reset", "ALTER TABLE users ADD COLUMN daily_reset TEXT"),
        ("current_chat_id", "ALTER TABLE users ADD COLUMN current_chat_id INTEGER DEFAULT 0"),
    ]:
        if col not in existing:
            cur.execute(ddl)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            created_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            chat_id INTEGER DEFAULT 0,
            role TEXT,
            content TEXT,
            created_at TEXT
        )
    """)
    cur.execute("PRAGMA table_info(messages)")
    existing = {row[1] for row in cur.fetchall()}
    if "chat_id" not in existing:
        cur.execute("ALTER TABLE messages ADD COLUMN chat_id INTEGER DEFAULT 0")

    conn.commit()
    conn.close()

init_db()


def db_get_user(user_id: int) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT vip, mode, started, daily_count, daily_reset, current_chat_id "
        "FROM users WHERE user_id=?",
        (user_id,),
    )
    row = cur.fetchone()
    now = datetime.now().isoformat()
    if row is None:
        cur.execute(
            "INSERT INTO users (user_id, vip, mode, started, daily_count, daily_reset, current_chat_id) "
            "VALUES (?, 0, 'classic', 0, 0, ?, 0)",
            (user_id, now),
        )
        conn.commit()
        conn.close()
        return {
            "vip": False, "mode": "classic", "started": 0,
            "daily_count": 0, "daily_reset": now, "current_chat_id": 0,
        }

    vip, mode, started, daily_count, daily_reset, current_chat_id = row
    if not daily_reset:
        daily_reset = now
        cur.execute("UPDATE users SET daily_reset=? WHERE user_id=?", (daily_reset, user_id))
        conn.commit()
    conn.close()

    if mode not in ("classic", "neurohumor", "image"):
        mode = "classic"

    return {
        "vip": bool(vip),
        "mode": mode,
        "started": int(started or 0),
        "daily_count": daily_count or 0,
        "daily_reset": daily_reset,
        "current_chat_id": int(current_chat_id or 0),
    }


def db_update_user(user_id: int, **fields):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for key, value in fields.items():
        cur.execute(f"UPDATE users SET {key}=? WHERE user_id=?", (value, user_id))
    conn.commit()
    conn.close()


def db_create_chat(user_id: int, title: str = "Новый чат") -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO chats (user_id, title, created_at) VALUES (?, ?, ?)",
        (user_id, title, datetime.now().isoformat()),
    )
    chat_id = cur.lastrowid
    conn.commit()
    conn.close()
    return chat_id


def db_get_chats(user_id: int) -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, created_at FROM chats WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def db_delete_chat(user_id: int, chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM chats WHERE id=? AND user_id=?", (chat_id, user_id))
    cur.execute("DELETE FROM messages WHERE user_id=? AND chat_id=?", (user_id, chat_id))
    conn.commit()
    conn.close()


def db_save_message(user_id: int, chat_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (user_id, chat_id, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, chat_id, role, content, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    chat_file = CHATS_DIR / f"{user_id}.txt"
    with chat_file.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] chat#{chat_id} {role}: {content}\n")


def db_load_history(user_id: int, chat_id: int, limit: int = HISTORY_LIMIT) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "SELECT role, content FROM messages WHERE user_id=? AND chat_id=? "
        "AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT ?",
        (user_id, chat_id, limit),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def check_limit(user_id: int) -> tuple[bool, str | None]:
    u = db_get_user(user_id)
    if u["vip"]:
        return True, None

    now = datetime.now()
    reset = datetime.fromisoformat(u["daily_reset"])
    if now >= reset:
        db_update_user(user_id, daily_count=0, daily_reset=(now + timedelta(days=1)).isoformat())
        return True, None

    if u["daily_count"] >= FREE_DAILY_LIMIT:
        remaining = reset - now
        hours, remainder = divmod(int(remaining.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        return False, f"Лимит исчерпан. Сброс через {hours} ч {minutes} мин."

    db_update_user(user_id, daily_count=u["daily_count"] + 1)
    return True, None


# ─── HTML ───
def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_for_telegram(text: str) -> str:
    parts = []
    last = 0
    for m in CODE_BLOCK_RE.finditer(text):
        parts.append(escape_html(text[last:m.start()]))
        lang = m.group(1) or ""
        code = m.group(2)
        parts.append(f'<pre><code class="language-{lang}">{escape_html(code)}</code></pre>')
        last = m.end()
    parts.append(escape_html(text[last:]))
    return "".join(parts)


def extract_codes(text: str) -> list[str]:
    return [m.group(2) for m in CODE_BLOCK_RE.finditer(text)]


# ─── Меню ───
def main_menu(user_id: int) -> InlineKeyboardMarkup:
    u = db_get_user(user_id)
    mode_icon = "🎭" if u["mode"] == "neurohumor" else ("🖼" if u["mode"] == "image" else "💬")
    if u["mode"] == "neurohumor":
        mode_name = "Нейрохам"
    elif u["mode"] == "image":
        mode_name = "Изображения"
    else:
        mode_name = "Классика"

    vip_icon = "💎" if u["vip"] else "🆓"
    vip_text = "Активен" if u["vip"] else f"Купить за {VIP_PRICE_STARS} ⭐"

    buttons = [
        [InlineKeyboardButton(text="🆕 Новый чат", callback_data="new_chat")],
        [InlineKeyboardButton(text="🗂 Мои чаты", callback_data="my_chats")],
        [InlineKeyboardButton(text="🗑 Удалить чат", callback_data="delete_menu")],
        [InlineKeyboardButton(text=f"{mode_icon} Режим: {mode_name}", callback_data="toggle_mode")],
        [InlineKeyboardButton(text="🖼 Генерация изображений", callback_data="image_menu")],
        [InlineKeyboardButton(text=f"{vip_icon} VIP: {vip_text}", callback_data="buy_vip")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def settings_menu(current_mode: str) -> InlineKeyboardMarkup:
    classic = "💬 Классика" + (" ✅" if current_mode == "classic" else "")
    neuro = "🎭 Нейрохам" + (" ✅" if current_mode == "neurohumor" else "")
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=classic, callback_data="set_mode:classic")],
        [InlineKeyboardButton(text=neuro, callback_data="set_mode:neurohumor")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")],
    ])


def chats_menu(user_id: int, current_chat_id: int) -> InlineKeyboardMarkup:
    chats = db_get_chats(user_id)
    buttons = []
    for cid, title, created in chats[:10]:
        mark = " ✅" if cid == current_chat_id else ""
        t = (title or "Чат")[:30]
        buttons.append([InlineKeyboardButton(text=f"💬 {t}{mark}", callback_data=f"open_chat:{cid}")])
    buttons.append([InlineKeyboardButton(text="🆕 Новый чат", callback_data="new_chat")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def delete_menu(user_id: int) -> InlineKeyboardMarkup:
    chats = db_get_chats(user_id)
    buttons = []
    for cid, title, created in chats[:10]:
        t = (title or "Чат")[:30]
        buttons.append([InlineKeyboardButton(text=f"🗑 {t}", callback_data=f"ask_del:{cid}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_delete_menu(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"del_chat:{chat_id}")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="delete_menu")],
    ])


# ─── Генерация картинки ───
async def generate_image(prompt: str) -> bytes:
    log.info(f"Картинка: model={IMAGE_MODEL['model_id']}")
    response = await image_client.images.generate(
        model=IMAGE_MODEL["model_id"],
        prompt=prompt,
        size="1024x1024",
        response_format="b64_json",
    )
    return base64.b64decode(response.data[0].b64_json)


# ─── Текст со стримом + ДИАГНОСТИКА ───
async def generate_text_stream(prompt: str, mode: str, history: list[dict]):
    system = BASE_PROMPT
    prefill = None

    if mode == "neurohumor":
        system += NEUROHUMOR_ADDON
        prefill = "ладно, слушай сюда. "
    else:
        system += CLASSIC_STRUCTURE

    # ─── ДИАГНОСТИКА: что уходит в API ───
    log.info("=" * 60)
    log.info(f"ЗАПРОС К API | model={TEXT_MODEL} | mode={mode}")
    log.info("=" * 60)
    log.info(f"SYSTEM PROMPT (первые 500 символов):")
    log.info(system[:500])
    log.info("-" * 60)
    log.info(f"ИСТОРИЯ: {len(history)} сообщений")
    log.info(f"USER: {prompt[:200]}")
    if prefill:
        log.info(f"PREFILL: {prefill}")
    log.info("=" * 60)

    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt})
    if prefill:
        messages.append({"role": "assistant", "content": prefill})

    stream = await text_client.chat.completions.create(
        model=TEXT_MODEL,
        messages=messages,
        stream=True,
        temperature=1.1,
        top_p=0.95,
    )

    if prefill:
        yield prefill

    async for chunk in stream:
        delta = chunk.choices[0].delta
        if delta and delta.content:
            yield delta.content


# ─── Фото ───
async def describe_image(image_bytes: bytes, user_prompt: str | None = None) -> str:
    b64 = base64.b64encode(image_bytes).decode()
    user_text = user_prompt or "Опиши, что на картинке. Если есть текст — перепиши его полностью."

    try:
        response = await text_client.chat.completions.create(
            model=VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }],
        )
        return response.choices[0].message.content
    except Exception as e:
        log.error(f"Vision error: {e}")
        return f"Не смог распознать картинку: {e}"


async def transcribe_voice(voice_bytes: bytes) -> str:
    try:
        buf = BytesIO(voice_bytes)
        buf.name = "voice.ogg"
        result = await text_client.audio.transcriptions.create(
            model=AUDIO_MODEL,
            file=buf,
            language="ru",
        )
        return result.text
    except Exception as e:
        log.error(f"Audio error: {e}")
        return f"Не смог распознать голосовое: {e}"


# ─── Стриминг ───
async def stream_to_telegram(msg: Message, token_iterator):
    buffer = ""
    full_text = ""
    last_edit = 0.0
    edit_interval = 0.7
    stop_button = stop_kb()

    async for token in token_iterator:
        buffer += token
        full_text += token

        now = time.monotonic()
        if now - last_edit >= edit_interval:
            try:
                await msg.edit_text(buffer + " ▌", reply_markup=stop_button)
                last_edit = now
            except Exception:
                pass

    formatted = format_for_telegram(full_text)
    codes = extract_codes(full_text)
    rmk = None
    if codes:
        buttons = [
            [InlineKeyboardButton(text=f"📋 Скопировать код #{j+1}", callback_data=f"copy:{j}")]
            for j in range(len(codes))
        ]
        rmk = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await msg.edit_text(text=formatted, parse_mode="HTML", reply_markup=rmk)
        if codes:
            pending_codes[msg.message_id] = codes
    except Exception as e:
        log.error(f"Финальный edit упал: {e}")
        try:
            await msg.edit_text(text=full_text, reply_markup=rmk)
            if codes:
                pending_codes[msg.message_id] = codes
        except Exception:
            pass

    return full_text


async def _stream_job(chat_id_tg: int, user_id: int, text: str, mode: str, chat_id_db: int):
    status_msg = None
    try:
        history = db_load_history(user_id, chat_id_db)
        db_save_message(user_id, chat_id_db, "user", text)

        status_msg = await bot.send_message(chat_id_tg, "⏳ Думает...", reply_markup=stop_kb())
        await bot.send_chat_action(chat_id_tg, ChatAction.TYPING)

        token_iter = generate_text_stream(text, mode, history)

        first_token = None
        async for t in token_iter:
            first_token = t
            break

        if first_token is None:
            await status_msg.edit_text("⚠️ Модель вернула пустой ответ.")
            return

        try:
            await status_msg.edit_text("✍️ Пишем...", reply_markup=stop_kb())
        except Exception:
            pass

        async def chained():
            yield first_token
            async for t in token_iter:
                yield t

        full_text = await stream_to_telegram(status_msg, chained())
        db_save_message(user_id, chat_id_db, "assistant", full_text or "")

    except asyncio.CancelledError:
        log.info(f"Генерация отменена user_id={user_id}")
        if status_msg:
            try:
                await status_msg.edit_text("⛔ Остановлено", reply_markup=None)
            except Exception:
                pass
        raise
    except Exception as e:
        log.error(f"Ошибка стрима: {e}")
        if status_msg:
            try:
                await status_msg.edit_text(f"Ошибка: {e}", reply_markup=None)
            except Exception:
                pass
    finally:
        active_tasks.pop(user_id, None)


# ─── Команды ───
@dp.message(Command("start"))
async def cmd_start(message: Message):
    db_get_user(message.from_user.id)
    await message.answer(
        "Привет! Я X1 AI.\n\nЧтобы начать — нажми «🆕 Новый чат».",
        reply_markup=main_menu(message.from_user.id),
    )


@dp.message(Command("promo"))
async def cmd_promo(message: Message):
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or args[1].strip() != PROMO_CODE:
        await message.answer("Неверный промо-код.")
        return

    u = db_get_user(message.from_user.id)
    if u["vip"]:
        await message.answer("У тебя уже активирован VIP ✅")
        return

    db_update_user(message.from_user.id, vip=1, daily_count=0, started=1)
    await message.answer("🎉 VIP активирован! Изображения и безлимитный текст доступны.")


# ─── Callbacks ───
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.message.answer("Главное меню:", reply_markup=main_menu(callback.from_user.id))
    await callback.answer()


@dp.callback_query(F.data == "new_chat")
async def cb_new_chat(callback: CallbackQuery):
    user_id = callback.from_user.id
    title = f"Чат от {datetime.now().strftime('%d.%m %H:%M')}"
    chat_id = db_create_chat(user_id, title)
    db_update_user(user_id, mode="classic", started=1, current_chat_id=chat_id)
    await callback.message.answer(f"✅ Новый чат создан.\nРежим: 💬 Классика\n\nПиши сообщение.")
    await callback.answer()


@dp.callback_query(F.data == "my_chats")
async def cb_my_chats(callback: CallbackQuery):
    u = db_get_user(callback.from_user.id)
    chats = db_get_chats(callback.from_user.id)
    if not chats:
        await callback.answer("У тебя пока нет чатов.", show_alert=True)
        return
    await callback.message.answer(
        "🗂 Твои чаты:",
        reply_markup=chats_menu(callback.from_user.id, u["current_chat_id"]),
    )
    await callback.answer()


@dp.callback_query(F.data == "delete_menu")
async def cb_delete_menu(callback: CallbackQuery):
    chats = db_get_chats(callback.from_user.id)
    if not chats:
        await callback.answer("У тебя нет чатов для удаления.", show_alert=True)
        return
    await callback.message.answer(
        "🗑 Какой чат удалить?",
        reply_markup=delete_menu(callback.from_user.id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("open_chat:"))
async def cb_open_chat(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id
    db_update_user(user_id, current_chat_id=chat_id, started=1, mode="classic")
    await callback.message.answer(f"✅ Открыт чат #{chat_id}. Режим: 💬 Классика")
    await callback.answer()


@dp.callback_query(F.data.startswith("ask_del:"))
async def cb_ask_del(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id

    chats = db_get_chats(user_id)
    title = None
    for cid, t, _ in chats:
        if cid == chat_id:
            title = t
            break
    if title is None:
        await callback.answer("Чат не найден.", show_alert=True)
        return

    await callback.message.answer(
        f"⚠️ Удалить чат «{title}»?\n\n"
        f"Вся история этого чата будет стёрта. Это действие нельзя отменить.",
        reply_markup=confirm_delete_menu(chat_id),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("del_chat:"))
async def cb_del_chat(callback: CallbackQuery):
    chat_id = int(callback.data.split(":", 1)[1])
    user_id = callback.from_user.id

    chats = db_get_chats(user_id)
    if not any(cid == chat_id for cid, _, _ in chats):
        await callback.answer("Чат не найден.", show_alert=True)
        return

    db_delete_chat(user_id, chat_id)
    u = db_get_user(user_id)
    if u["current_chat_id"] == chat_id:
        db_update_user(user_id, current_chat_id=0, started=0)

    try:
        await callback.message.edit_text("🗑 Чат удалён.", reply_markup=None)
    except Exception:
        await callback.message.answer("🗑 Чат удалён.")

    u = db_get_user(user_id)
    chats_left = db_get_chats(user_id)
    if chats_left:
        await callback.message.answer(
            "🗂 Остались чаты:",
            reply_markup=delete_menu(user_id),
        )
    await callback.answer("Удалено")


@dp.callback_query(F.data == "stop_gen")
async def cb_stop_gen(callback: CallbackQuery):
    user_id = callback.from_user.id
    task = active_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        await callback.answer("⛔ Останавливаю...")
    else:
        await callback.answer("Нечего останавливать.")


@dp.callback_query(F.data == "toggle_mode")
async def cb_toggle_mode(callback: CallbackQuery):
    u = db_get_user(callback.from_user.id)
    new_mode = "neurohumor" if u["mode"] in ("classic", "image") else "classic"
    db_update_user(callback.from_user.id, mode=new_mode, started=1)
    name = "🎭 Нейрохам" if new_mode == "neurohumor" else "💬 Классика"
    await callback.message.answer(f"✅ Текущий режим: {name}")
    await callback.answer()


@dp.callback_query(F.data == "image_menu")
async def cb_image_menu(callback: CallbackQuery):
    u = db_get_user(callback.from_user.id)
    if not u["vip"]:
        await callback.answer(
            f"Изображения только для VIP. Купи за {VIP_PRICE_STARS} ⭐ или используй промо.",
            show_alert=True,
        )
        return
    db_update_user(callback.from_user.id, mode="image", started=1)
    await callback.message.answer(
        f"🎨 Режим: {IMAGE_MODEL['name']}\n\n"
        f"Отправляй промпты. Чтобы вернуться — нажми «🆕 Новый чат»."
    )
    await callback.answer()


@dp.callback_query(F.data == "buy_vip")
async def cb_buy_vip(callback: CallbackQuery):
    prices = [LabeledPrice(label="VIP доступ", amount=VIP_PRICE_STARS)]
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="VIP доступ X1 AI",
        description="Безлимитный текст + генерация изображений",
        payload="vip_purchase",
        provider_token="",
        currency="XTR",
        prices=prices,
    )
    await callback.answer()


@dp.callback_query(F.data == "settings")
async def cb_settings(callback: CallbackQuery):
    u = db_get_user(callback.from_user.id)
    if u["mode"] == "neurohumor":
        mode_name = "🎭 Нейрохам"
    elif u["mode"] == "image":
        mode_name = "🖼 Изображения"
    else:
        mode_name = "💬 Классика"

    await callback.message.answer(
        f"⚙️ Настройки\n"
        f"• Текущий режим: {mode_name}\n"
        f"• VIP: {'да' if u['vip'] else 'нет'}\n"
        f"• Сообщений сегодня: {u['daily_count']}/{FREE_DAILY_LIMIT}\n\n"
        f"Выбери режим:",
        reply_markup=settings_menu(u["mode"]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("set_mode:"))
async def cb_set_mode(callback: CallbackQuery):
    mode = callback.data.split(":", 1)[1]
    if mode not in ("classic", "neurohumor"):
        await callback.answer("Неизвестный режим.")
        return

    db_update_user(callback.from_user.id, mode=mode, started=1)
    name = "🎭 Нейрохам" if mode == "neurohumor" else "💬 Классика"

    try:
        await callback.message.delete()
    except Exception:
        pass

    await callback.message.answer(
        f"⚙️ Настройки\n• Текущий режим: {name}\n\nВыбери режим:",
        reply_markup=settings_menu(mode),
    )
    await callback.answer(f"✅ Установлен режим: {name}")


@dp.callback_query(F.data.startswith("copy:"))
async def cb_copy_code(callback: CallbackQuery):
    idx = int(callback.data.split(":", 1)[1])
    codes = pending_codes.get(callback.message.message_id, [])
    if idx >= len(codes):
        await callback.answer("Код не найден.", show_alert=True)
        return
    await callback.message.answer(
        f"<pre><code>{escape_html(codes[idx])}</code></pre>",
        parse_mode="HTML",
    )
    await callback.answer("Код отправлен ниже 👇")


# ─── Оплата ───
@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)


@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    db_update_user(message.from_user.id, vip=1, daily_count=0, started=1)
    await message.answer("🎉 VIP активирован! Изображения и безлимитный текст доступны.")


# ─── Фото ───
@dp.message(F.photo)
async def on_photo(message: Message):
    user_id = message.from_user.id
    u = db_get_user(user_id)

    if not u["started"] or u["current_chat_id"] == 0:
        await message.answer("⚠️ Сначала нажми «🆕 Новый чат».", reply_markup=main_menu(user_id))
        return

    allowed, error = check_limit(user_id)
    if not allowed:
        await message.answer(error)
        return

    status = await message.answer("👁 Смотрю картинку...")
    await bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = BytesIO()
        await bot.download_file(file.file_path, buf)
        img_bytes = buf.getvalue()

        user_prompt = message.caption if message.caption else None
        description = await describe_image(img_bytes, user_prompt)

        await status.edit_text("✍️ Пишем...")

        db_save_message(user_id, u["current_chat_id"], "user", f"[фото] {user_prompt or 'что на картинке?'}")
        db_save_message(user_id, u["current_chat_id"], "assistant", description)

        await status.delete()
        chunks = [description[i:i+3800] for i in range(0, len(description), 3800)]
        for chunk in chunks:
            await message.answer(format_for_telegram(chunk), parse_mode="HTML")

    except Exception as e:
        log.error(f"Ошибка фото: {e}")
        await status.edit_text(f"Ошибка распознавания: {e}")


# ─── Голосовые ───
@dp.message(F.voice)
async def on_voice(message: Message):
    user_id = message.from_user.id
    u = db_get_user(user_id)

    if not u["started"] or u["current_chat_id"] == 0:
        await message.answer("⚠️ Сначала нажми «🆕 Новый чат».", reply_markup=main_menu(user_id))
        return

    allowed, error = check_limit(user_id)
    if not allowed:
        await message.answer(error)
        return

    status = await message.answer("🎙 Слушаю голосовое...")

    try:
        voice = message.voice
        file = await bot.get_file(voice.file_id)
        buf = BytesIO()
        await bot.download_file(file.file_path, buf)
        voice_bytes = buf.getvalue()

        transcript = await transcribe_voice(voice_bytes)

        if transcript.startswith("Не смог"):
            await status.edit_text(transcript)
            return

        await status.edit_text(f"🎙 Распознано:\n\n{transcript}")

        task = asyncio.create_task(
            _stream_job(message.chat.id, user_id, transcript, u["mode"], u["current_chat_id"])
        )
        active_tasks[user_id] = task

    except Exception as e:
        log.error(f"Ошибка голосового: {e}")
        try:
            await status.edit_text(f"Ошибка: {e}")
        except Exception:
            await message.answer(f"Ошибка: {e}")


# ─── Текстовые сообщения ───
@dp.message(F.text & ~F.text.startswith("/"))
async def on_message(message: Message):
    user_id = message.from_user.id
    u = db_get_user(user_id)

    if not u["started"] or u["current_chat_id"] == 0:
        await message.answer(
            "⚠️ Сначала нажми «🆕 Новый чат».",
            reply_markup=main_menu(user_id),
        )
        return

    existing = active_tasks.get(user_id)
    if existing and not existing.done():
        await message.answer("⏳ Я ещё пишу прошлый ответ. Нажми «⛔ Остановить» или подожди.")
        return

    if u["mode"] == "image":
        if not u["vip"]:
            await message.answer("Изображения только для VIP.")
            db_update_user(user_id, mode="classic")
            return
        status = await message.answer("🖼 Генерирую изображение...")
        await bot.send_chat_action(message.chat.id, ChatAction.UPLOAD_PHOTO)
        try:
            img_bytes = await generate_image(message.text)
            await status.delete()
            photo = BufferedInputFile(img_bytes, filename="gen.png")
            await message.answer_photo(
                photo=photo,
                caption=f"Готово ✅ ({IMAGE_MODEL['name']})\n"
                        f"Отправь ещё промпт или нажми «🆕 Новый чат» для выхода.",
            )
            db_save_message(user_id, u["current_chat_id"], "image_prompt", message.text)
        except Exception as e:
            log.error(f"Ошибка генерации: {e}")
            await status.edit_text("❌ Модель генерации сейчас недоступна. Попробуй позже.")
        return

    allowed, error = check_limit(user_id)
    if not allowed:
        await message.answer(error)
        return

    task = asyncio.create_task(
        _stream_job(message.chat.id, user_id, message.text, u["mode"], u["current_chat_id"])
    )
    active_tasks[user_id] = task


if __name__ == "__main__":
    dp.run_polling(bot)
