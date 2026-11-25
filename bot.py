import asyncio
import io
import logging
import os
import re
import sqlite3
import tempfile
from dotenv import load_dotenv
from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

ALLOWED_USER_IDS: set[int] = {
    int(uid.strip())
    for uid in os.environ.get("ALLOWED_USER_IDS", "").split(",")
    if uid.strip().isdigit()
}

STICKER_IDS: list[str] = [
    s.strip() for s in os.environ.get("STICKER_IDS", "").split(",") if s.strip()
]
GIF_IDS: list[str] = [
    g.strip() for g in os.environ.get("GIF_IDS", "").split(",") if g.strip()
]

CHAT_MODEL = os.environ.get("GEMINI_CHAT_MODEL", "gemini-2.5-flash")
IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "imagen-3.0-generate-002")
DB_PATH = os.environ.get("DB_PATH", "buddy_bot.db")
HISTORY_LIMIT = 24

BASE_FRIEND_PERSONA = os.environ.get(
    "FRIEND_PERSONA",
    (
        "تو یک دوست خیلی صمیمی، باحال، شوخ‌طبع، باهوش و همراه واقعی برای کاربر هستی. "
        "مثل یک رفیق نزدیک و خودمانی به فارسی روان و طبیعی حرف بزن؛ رسمی و خشک نباش. "
        "اگه موقعیت مناسب بود، می‌تونی آخر پیام با تگ [STICKER] یا [GIF] اشاره کنی که استیکر یا گیف بفرسته، "
        "ولی زیاده‌روی نکن. هیچ‌وقت خودت رو به عنوان ربات یا مدل خشک معرفی نکن مگر اینکه لازم باشه."
    ),
)

client = genai.Client(api_key=GEMINI_API_KEY)


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS learned_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                fact TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()


def save_message(user_id: int, role: str, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        conn.commit()


def clear_history(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        conn.commit()


def add_fact(user_id: int, fact: str) -> None:
    clean = fact.strip()
    if not clean:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO learned_facts (user_id, fact) VALUES (?, ?)",
            (user_id, clean),
        )
        conn.commit()


def get_facts(user_id: int) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT fact FROM learned_facts WHERE user_id = ? ORDER BY id ASC",
            (user_id,),
        ).fetchall()
    return [r[0] for r in rows]


def clear_facts(user_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM learned_facts WHERE user_id = ?", (user_id,))
        conn.commit()


def build_system_instruction(user_id: int) -> str:
    facts = get_facts(user_id)
    if not facts:
        return BASE_FRIEND_PERSONA
    facts_block = "\n".join(f"- {f}" for f in facts)
    return (
        f"{BASE_FRIEND_PERSONA}\n\n"
        f"نکات و چیزهایی که تا الان یاد گرفتی و باید همیشه در نظر داشته باشی:\n{facts_block}"
    )


def build_history_contents(user_id: int, current_parts: list[types.Part]) -> list[types.Content]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM messages
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, HISTORY_LIMIT),
        ).fetchall()

    rows.reverse()
    contents: list[types.Content] = []
    for role, content in rows:
        gemini_role = "user" if role == "user" else "model"
        contents.append(
            types.Content(
                role=gemini_role,
                parts=[types.Part.from_text(text=content)],
            )
        )

    contents.append(types.Content(role="user", parts=current_parts))
    return contents


async def check_access(update: Update) -> bool:
    user = update.effective_user
    if not user or not ALLOWED_USER_IDS or user.id not in ALLOWED_USER_IDS:
        if update.message:
            await update.message.reply_text("سیکیم ابلفضلی")
        return False
    return True


async def send_long_text(update: Update, text: str) -> None:
    max_len = 4000
    if not text:
        text = "حرفی نزدم، یه بار دیگه بگو!"
    for i in range(0, len(text), max_len):
        await update.message.reply_text(text[i : i + max_len])


async def send_media_tags_if_any(update: Update, context: ContextTypes.DEFAULT_TYPE, raw_text: str) -> str:
    want_sticker = "[STICKER]" in raw_text
    want_gif = "[GIF]" in raw_text
    cleaned = raw_text.replace("[STICKER]", "").replace("[GIF]", "").strip()

    if want_sticker and STICKER_IDS:
        idx = context.chat_data.get("sticker_idx", 0) % len(STICKER_IDS)
        context.chat_data["sticker_idx"] = idx + 1
        try:
            await update.message.reply_sticker(STICKER_IDS[idx])
        except Exception as exc:
            logger.warning("Failed sending sticker: %s", exc)

    if want_gif and GIF_IDS:
        idx = context.chat_data.get("gif_idx", 0) % len(GIF_IDS)
        context.chat_data["gif_idx"] = idx + 1
        try:
            await update.message.reply_animation(GIF_IDS[idx])
        except Exception as exc:
            logger.warning("Failed sending gif: %s", exc)

    return cleaned


async def generate_and_send_image(update: Update, context: ContextTypes.DEFAULT_TYPE, prompt: str) -> None:
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="upload_photo")

    try:
        result = await asyncio.to_thread(
            client.models.generate_images,
            model=IMAGE_MODEL,
            prompt=prompt,
            config=types.GenerateImagesConfig(
                number_of_images=1,
                output_mime_type="image/jpeg",
            ),
        )
        if not result.generated_images:
            await update.message.reply_text("نتونستم عکسو بسازم، یه جور دیگه توضیح بده.")
            return

        img_bytes = result.generated_images[0].image.image_bytes
        bio = io.BytesIO(img_bytes)
        bio.name = "generated.jpg"
        await update.message.reply_photo(photo=bio, caption=f"اینم از این: {prompt}")
        save_message(user_id, "user", f"[درخواست ساخت عکس: {prompt}]")
        save_message(user_id, "model", f"[یک عکس با موضوع «{prompt}» ساختم و فرستادم]")
    except Exception as exc:
        logger.exception("Image generation error: %s", exc)
        await update.message.reply_text("موقع ساخت عکس یه مشکلی پیش اومد، دوباره امتحان کن.")


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    await update.message.reply_text(
        "هورا! رسیدم رفیق 👋\n"
        "می‌تونی ویس، ویدیو مسیج، عکس یا متن بفرستی؛ مثل رفیق باهات گپ می‌زنم.\n"
        "دستورها و قابلیت‌ها:\n"
        "• یاد بگیر: <نکته> -> یادم می‌مونه\n"
        "• /facts -> چیزهایی که یاد گرفتم\n"
        "• /forget -> پاک کردن چیزهای یادگرفته‌شده\n"
        "• /draw <توضیح> -> ساخت عکس\n"
        "• /sticker و /gif -> فرستادن استیکر یا گیف\n"
        "• /reset -> پاک کردن تاریخچه چت"
    )


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    clear_history(update.effective_user.id)
    await update.message.reply_text("صفحه رو تمیز کردم، از صفر شروع کنیم!")


async def facts_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    facts = get_facts(update.effective_user.id)
    if not facts:
        await update.message.reply_text("هنوز چیز خاصی یاد نگرفتم! هر وقت خواستی بگو: یاد بگیر: ...")
        return
    msg = "این چیزا تو ذهنمه:\n" + "\n".join(f"{i+1}. {f}" for i, f in enumerate(facts))
    await send_long_text(update, msg)


async def forget_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    clear_facts(update.effective_user.id)
    await update.message.reply_text("همه نکات یادگرفته‌شده رو پاک کردم.")


async def draw_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    prompt = " ".join(context.args).strip()
    if not prompt:
        await update.message.reply_text("بگو چی بکشم! مثلاً: /draw یک گربه فضایی در کهکشان")
        return
    await generate_and_send_image(update, context, prompt)


async def sticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if not STICKER_IDS:
        await update.message.reply_text("هنوز STICKER_IDS توی تنظیمات نذاشتی رفیق.")
        return
    idx = context.chat_data.get("sticker_idx", 0) % len(STICKER_IDS)
    context.chat_data["sticker_idx"] = idx + 1
    await update.message.reply_sticker(STICKER_IDS[idx])


async def gif_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if not GIF_IDS:
        await update.message.reply_text("هنوز GIF_IDS توی تنظیمات نذاشتی رفیق.")
        return
    idx = context.chat_data.get("gif_idx", 0) % len(GIF_IDS)
    context.chat_data["gif_idx"] = idx + 1
    await update.message.reply_animation(GIF_IDS[idx])


async def run_chat_turn(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    parts: list[types.Part],
    memory_summary: str,
) -> None:
    user_id = update.effective_user.id
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    try:
        sys_inst = build_system_instruction(user_id)
        contents = build_history_contents(user_id, parts)
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=CHAT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=sys_inst,
            ),
        )
        raw_reply = (response.text or "").strip()
        if not raw_reply:
            raw_reply = "یه لحظه هنگ کردم، دوباره بگو!"

        final_reply = await send_media_tags_if_any(update, context, raw_reply)
        save_message(user_id, "user", memory_summary)
        save_message(user_id, "model", final_reply or "[استیکر/گیف فرستاد]")
        await send_long_text(update, final_reply)
    except Exception as exc:
        logger.exception("Chat turn failed: %s", exc)
        await update.message.reply_text("یه خطای کوچیک پیش اومد، یه بار دیگه بفرست.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text:
        return

    user_id = update.effective_user.id

    learn_match = re.match(r"^(?:یاد بگیر|یادت باشه|حفظ کن)\s*[:：]\s*(.+)$", text, flags=re.DOTALL)
    if learn_match:
        fact_text = learn_match.group(1).strip()
        add_fact(user_id, fact_text)
        await update.message.reply_text(f"اوکی، تو کلم ثبت شد: «{fact_text}»")
        return

    draw_match = re.match(r"^(?:عکس بساز|تصویر بساز|بکش)\s*[:：]\s*(.+)$", text, flags=re.DOTALL)
    if draw_match:
        await generate_and_send_image(update, context, draw_match.group(1).strip())
        return

    await run_chat_turn(
        update=update,
        context=context,
        parts=[types.Part.from_text(text=text)],
        memory_summary=text,
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await check_access(update):
        return
    msg = update.message
    if not msg:
        return

    caption = (msg.caption or "").strip()
    temp_path: str | None = None
    uploaded_file = None

    try:
        if msg.photo:
            photo = msg.photo[-1]
            tg_file = await context.bot.get_file(photo.file_id)
            img_bytes = await tg_file.download_as_bytearray()
            prompt_txt = caption if caption else "این عکسو ببین و مثل یک دوست صمیمی درباره‌ش نظر بده یا جوابمو بده."
            parts = [
                types.Part.from_bytes(data=bytes(img_bytes), mime_type="image/jpeg"),
                types.Part.from_text(text=prompt_txt),
            ]
            await run_chat_turn(update, context, parts, f"[عکس فرستاد] {caption}".strip())
            return

        if msg.voice:
            tg_file = await context.bot.get_file(msg.voice.file_id)
            with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tf:
                temp_path = tf.name
            await tg_file.download_to_drive(temp_path)
            uploaded_file = await asyncio.to_thread(client.files.upload, file=temp_path)
            prompt_txt = caption if caption else "این ویس رو گوش بده و مثل یک رفیق صمیمی جوابمو بده."
            parts = [
                uploaded_file,
                types.Part.from_text(text=prompt_txt),
            ]
            await run_chat_turn(update, context, parts, f"[ویس فرستاد] {caption}".strip())
            return

        if msg.video_note or msg.video:
            media_obj = msg.video_note or msg.video
            tg_file = await context.bot.get_file(media_obj.file_id)
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
                temp_path = tf.name
            await tg_file.download_to_drive(temp_path)
            uploaded_file = await asyncio.to_thread(client.files.upload, file=temp_path)
            default_q = (
                "این ویدیو مسیج رو ببین و بشنو و صمیمی جوابمو بده."
                if msg.video_note
                else "این ویدیو رو ببین و مثل یک دوست درباره‌ش واکنش نشون بده یا جوابمو بده."
            )
            prompt_txt = caption if caption else default_q
            parts = [
                uploaded_file,
                types.Part.from_text(text=prompt_txt),
            ]
            tag = "[ویدیو مسیج فرستاد]" if msg.video_note else "[ویدیو فرستاد]"
            await run_chat_turn(update, context, parts, f"{tag} {caption}".strip())
            return

    except Exception as exc:
        logger.exception("Media handling error: %s", exc)
        await update.message.reply_text("نتونستم این فایل رو درست پردازش کنم، دوباره بفرست.")
    finally:
        if uploaded_file:
            try:
                await asyncio.to_thread(client.files.delete, name=uploaded_file.name)
            except Exception:
                pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass


def main() -> None:
    if not ALLOWED_USER_IDS:
        raise ValueError("ALLOWED_USER_IDS تنظیم نشده است.")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    init_db()
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("facts", facts_cmd))
    app.add_handler(CommandHandler("forget", forget_cmd))
    app.add_handler(CommandHandler("draw", draw_cmd))
    app.add_handler(CommandHandler("sticker", sticker_cmd))
    app.add_handler(CommandHandler("gif", gif_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.VOICE | filters.VIDEO_NOTE | filters.VIDEO,
            handle_media,
        )
    )

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
