import asyncio
import base64
import io
import os
import re
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
from anthropic import AsyncAnthropic
import openai

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
claude = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

_openai_key = os.getenv("OPENAI_API_KEY")
openai_client = openai.AsyncOpenAI(api_key=_openai_key) if _openai_key else None

DB_PATH = "bot.db"
FREE_LIMIT = 3          # бесплатных анализов
COUNTER_SEED = 8341     # стартовый счётчик для соцдоказательства

# ─── БАЗА ДАННЫХ ──────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                requests_count INTEGER DEFAULT 0,
                is_subscribed INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER,
                who TEXT,
                concern TEXT,
                analysis_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        # Засеиваем счётчик если первый запуск
        await db.execute(
            "INSERT OR IGNORE INTO stats (key, value) VALUES ('total_analyses', ?)",
            (COUNTER_SEED,)
        )
        await db.commit()


async def get_user(telegram_id: int) -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT requests_count, is_subscribed FROM users WHERE telegram_id = ?",
            (telegram_id,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return {"requests_count": row[0], "is_subscribed": row[1]}
            return {"requests_count": 0, "is_subscribed": 0}


async def increment_requests(telegram_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (telegram_id, requests_count)
            VALUES (?, 1)
            ON CONFLICT(telegram_id) DO UPDATE SET requests_count = requests_count + 1
        """, (telegram_id,))
        await db.execute(
            "UPDATE stats SET value = value + 1 WHERE key = 'total_analyses'"
        )
        await db.commit()


async def save_analysis(telegram_id: int, who: str, concern: str, text: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO analyses (telegram_id, who, concern, analysis_text) VALUES (?,?,?,?)",
            (telegram_id, who, concern, text)
        )
        await db.commit()


async def get_history(telegram_id: int, limit: int = 3) -> list:
    """Последние N анализов пользователя для сравнения динамики."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT who, concern, analysis_text, created_at
            FROM analyses
            WHERE telegram_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (telegram_id, limit)) as cur:
            rows = await cur.fetchall()
            return [{"who": r[0], "concern": r[1], "analysis": r[2], "date": r[3]}
                    for r in rows]


async def get_total_analyses() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM stats WHERE key='total_analyses'") as cur:
            row = await cur.fetchone()
            return row[0] if row else COUNTER_SEED

# ─── ГЕНДЕРНЫЕ ФОРМЫ ─────────────────────────────────────────────────────────

GENDER_FORMS = {
    "female": {
        "persona":        "как умная подруга которая не жалеет, а говорит правду",
        "resolved":       "разобралась, вот что вижу",
        "paywall_used":   "использовала",
        "paywall_right":  "каждый раз я была права",
        "paywall_friend": "похода к подруге за советом",
    },
    "male": {
        "persona":        "как опытный аналитик который говорит правду без прикрас",
        "resolved":       "разобрался, вот что вижу",
        "paywall_used":   "использовал",
        "paywall_right":  "каждый раз анализ был точным",
        "paywall_friend": "одной консультации со специалистом",
    },
    "other": {
        "persona":        "как опытный аналитик который говорит правду без прикрас",
        "resolved":       "разобрал, вот что вижу",
        "paywall_used":   "использовал(а)",
        "paywall_right":  "каждый раз анализ был точным",
        "paywall_friend": "одной консультации",
    },
}

# ─── FSM ──────────────────────────────────────────────────────────────────────

class AnalysisState(StatesGroup):
    waiting_for_material = State()
    post_analysis = State()
    waiting_for_compare = State()   # сравнение старой переписки с новой

# ─── ПРОМПТЫ ──────────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """Ты — эксперт по анализу манипуляций и лжи в переписке. \
Говори уверенно, {persona}.

Кого анализируем: {who}
Что беспокоит: {concern}
{material_label}{material}

Начни с одной короткой фразы поддержки — адаптируй под ситуацию: \
если переписка тревожная — сочувствие ("понимаю, это тяжело читать"), \
если всё в порядке — ободрение ("хорошие новости"), \
если неоднозначно — нейтральное ("{resolved}"). \
Не повторяй одну и ту же фразу каждый раз.

Затем выдай анализ строго в этом формате — БЕЗ звёздочек, БЕЗ решёток, БЕЗ тире-разделителей. \
Только текст и эмодзи:

🔍 ВЕРДИКТ
(1-2 предложения, прямо и по делу)

⚠️ СИГНАЛЫ
(2-4 конкретных признака с цитатами и объяснением)

📊 УРОВЕНЬ ТРЕВОГИ: Низкий / Средний / Высокий

💡 ЧТО СДЕЛАТЬ ПРЯМО СЕЙЧАС
(1 конкретное действие — не "проверь", а "напиши вот это" / "замолчи на 2 дня" / "задай прямой вопрос")"""

REPLY_PROMPT = """Ты — эксперт по отношениям. На основе анализа переписки напиши конкретный черновик ответного сообщения.

Контекст: {who}, беспокоило — {concern}
Результат анализа:
{analysis}

Напиши 2-3 варианта ответного сообщения — от мягкого к прямому. \
Каждый вариант с коротким пояснением зачем он работает. \
БЕЗ звёздочек и решёток."""

COMPARE_PROMPT = """Ты — эксперт по динамике отношений.

Кого анализируем: {who}
Старая переписка (как было раньше): {old_text}
Новая переписка (как сейчас): {new_text}

Сравни динамику: что изменилось в тоне, частоте, вовлечённости. \
Будь конкретной — цифры, цитаты, факты. \
Начни с главного вывода. БЕЗ звёздочек и решёток. Только текст и эмодзи.

📉 КАК ИЗМЕНИЛОСЬ
(конкретно что стало хуже или лучше)

🔍 КЛЮЧЕВЫЕ ОТЛИЧИЯ
(2-3 цитаты — раньше vs сейчас)

📊 ВЕРДИКТ ПО ДИНАМИКЕ: Холодает / Стабильно / Теплеет

💡 ЧТО ЭТО ЗНАЧИТ"""

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────────────────────────────────

def clean_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s+', '', text)
    text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)
    return text.strip()


async def send_long(message: Message, text: str, **kwargs):
    text = clean_markdown(text)
    if len(text) <= 4000:
        await message.answer(text, **kwargs)
        return
    paragraphs = text.split("\n\n")
    chunk = ""
    chunks = []
    for para in paragraphs:
        if len(chunk) + len(para) + 2 > 4000:
            if chunk:
                chunks.append(chunk.strip())
            chunk = para
        else:
            chunk += ("\n\n" + para) if chunk else para
    if chunk:
        chunks.append(chunk.strip())
    for i, part in enumerate(chunks):
        if i == len(chunks) - 1:
            await message.answer(part, **kwargs)
        else:
            await message.answer(part)


async def transcribe_audio(file_bytes: bytes, filename: str) -> str:
    if not openai_client:
        raise RuntimeError("Голосовые не настроены. Добавь OPENAI_API_KEY на Railway.")
    buf = io.BytesIO(file_bytes)
    try:
        transcript = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, buf),
            language="ru"
        )
        return transcript.text
    except openai.RateLimitError:
        raise RuntimeError(
            "На аккаунте OpenAI закончились кредиты. "
            "Зайди на platform.openai.com → Billing и пополни баланс."
        )


async def analyze_with_claude(who: str, concern: str, material_label: str,
                               material: str, image_b64: str = None,
                               user_gender: str = "female") -> str:
    gf = GENDER_FORMS.get(user_gender, GENDER_FORMS["female"])
    prompt = ANALYSIS_PROMPT.format(
        who=who, concern=concern,
        material_label=material_label, material=material,
        persona=gf["persona"], resolved=gf["resolved"]
    )
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
        {"type": "text", "text": prompt}
    ] if image_b64 else prompt

    response = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": content}]
    )
    return response.content[0].text


async def check_paywall(message: Message, telegram_id: int,
                        user_gender: str = "female") -> bool:
    """True = можно продолжать. False = показали пейволл."""
    user = await get_user(telegram_id)
    if user["is_subscribed"]:
        return True
    if user["requests_count"] >= FREE_LIMIT:
        gf = GENDER_FORMS.get(user_gender, GENDER_FORMS["female"])
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Оформить за 299 ₽/мес", callback_data="subscribe")
        builder.adjust(1)
        await message.answer(
            f"Ты уже знаешь что что-то не так.\n\n"
            f"Ты {gf['paywall_used']} {FREE_LIMIT} бесплатных анализа — "
            f"и {gf['paywall_right']}.\n\n"
            f"Не останавливайся на полпути. За 299 ₽/месяц — безлимит.\n"
            f"Это дешевле {gf['paywall_friend']}.",
            reply_markup=builder.as_markup()
        )
        return False
    return True

# ─── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────

def gender_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="👩 Девушка",  callback_data="gender_female")
    builder.button(text="👨 Парень",   callback_data="gender_male")
    builder.button(text="Другое",      callback_data="gender_other")
    builder.adjust(2, 1)
    return builder.as_markup()

def main_menu(total: int = COUNTER_SEED):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Начать анализ", callback_data="start_analysis")
    builder.button(text="📊 Сравнить динамику", callback_data="start_compare")
    builder.button(text="Как это работает?", callback_data="how_it_works")
    builder.adjust(1)
    # Счётчик вшит в текст сообщения, не в кнопку
    return builder.as_markup()

def who_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Парень / муж", callback_data="who_boyfriend")
    builder.button(text="Подруга",       callback_data="who_friend")
    builder.button(text="Коллега",       callback_data="who_colleague")
    builder.button(text="Другой",        callback_data="who_other")
    builder.adjust(2)
    return builder.as_markup()

def concern_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Чувствую ложь",  callback_data="concern_lie")
    builder.button(text="Манипуляции",    callback_data="concern_manipulation")
    builder.button(text="Стал холоднее",  callback_data="concern_cold")
    builder.button(text="Другое",         callback_data="concern_other")
    builder.adjust(2)
    return builder.as_markup()

def after_menu(who: str = ""):
    """who — кого анализировали, чтобы подставить ему/ей в кнопку."""
    him_her = "ей" if who in ("подруга",) else "ему"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✍️ Что ответить {him_her}", callback_data="get_reply")
    builder.button(text="🔍 Разобрать другого",        callback_data="start_analysis")
    builder.button(text="💬 Задать вопрос",            callback_data="ask_question")
    builder.adjust(1)
    return builder.as_markup()

def after_reply_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Разобрать другого", callback_data="start_analysis")
    builder.button(text="💬 Задать вопрос",     callback_data="ask_question")
    builder.adjust(1)
    return builder.as_markup()

# ─── /START ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    total = await get_total_analyses()
    await message.answer(
        f"Привет. Я всё знаю.\n\n"
        f"Без розовых очков. Без «ну может он просто занят».\n"
        f"Холодный взгляд на факты — скажу тебе правду.\n\n"
        f"Уже разобрано {total:,} переписок.\n\n"
        f"Скинь переписку, скрин или голосовое — разберём что происходит.",
        reply_markup=main_menu(total)
    )

# ─── КОЛБЭКИ: ОНБОРДИНГ ───────────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "how_it_works")
async def how_it_works(callback: CallbackQuery):
    await callback.message.answer(
        "Ты скидываешь переписку, скрин или голосовое.\n"
        "Я анализирую — нахожу признаки лжи и манипуляций.\n"
        "Выдаю вердикт с цитатами + черновик ответа.\n\n"
        "Переписки не хранятся. Всё конфиденциально."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "subscribe")
async def subscribe(callback: CallbackQuery):
    await callback.message.answer(
        "Оплата через Telegram Stars — скоро будет доступна.\n"
        "Следи за обновлениями."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "start_analysis")
async def start_analysis(callback: CallbackQuery, state: FSMContext):
    # Сохраняем гендер если уже выбирали раньше — не спрашиваем повторно
    data = await state.get_data()
    existing_gender = data.get("user_gender")
    await state.clear()
    if existing_gender:
        await state.update_data(user_gender=existing_gender)
        await callback.message.answer("Кого будем разбирать?", reply_markup=who_menu())
    else:
        await callback.message.answer("Ты кто?", reply_markup=gender_menu())
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("gender_"))
async def choose_gender(callback: CallbackQuery, state: FSMContext):
    gender_map = {
        "gender_female": "female",
        "gender_male":   "male",
        "gender_other":  "other",
    }
    gender = gender_map.get(callback.data, "female")
    await state.update_data(user_gender=gender)
    await callback.message.answer("Кого будем разбирать?", reply_markup=who_menu())
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("who_"))
async def choose_who(callback: CallbackQuery, state: FSMContext):
    who_map = {
        "who_boyfriend": "парень/муж",
        "who_friend":    "подруга",
        "who_colleague": "коллега",
        "who_other":     "другой человек"
    }
    await state.update_data(who=who_map.get(callback.data, "другой человек"))
    await callback.message.answer("Что беспокоит?", reply_markup=concern_menu())
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("concern_"))
async def choose_concern(callback: CallbackQuery, state: FSMContext):
    concern_map = {
        "concern_lie":          "чувствую ложь",
        "concern_manipulation": "манипуляции",
        "concern_cold":         "стал холоднее",
        "concern_other":        "другое"
    }
    await state.update_data(concern=concern_map.get(callback.data, "другое"))
    await state.set_state(AnalysisState.waiting_for_material)
    await callback.message.answer(
        "Жду материал.\n\n"
        "Скопируй переписку и вставь сюда, отправь скрин или голосовое."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "ask_question")
async def ask_question_cb(callback: CallbackQuery, state: FSMContext):
    current = await state.get_state()
    if current != AnalysisState.post_analysis:
        await state.set_state(AnalysisState.post_analysis)
    await callback.message.answer("Задавай — я в контексте, отвечу.")
    await callback.answer()


@dp.callback_query(lambda c: c.data == "get_reply")
async def get_reply(callback: CallbackQuery, state: FSMContext):
    """Кнопка 'Что ответить' — генерирует черновик ответного сообщения."""
    data = await state.get_data()
    who           = data.get("who", "этот человек")
    concern       = data.get("concern", "")
    last_analysis = data.get("last_analysis", "")

    if not last_analysis:
        await callback.message.answer("Сначала отправь переписку для анализа.")
        await callback.answer()
        return

    await callback.message.answer("Пишу варианты ответа...")
    try:
        prompt = REPLY_PROMPT.format(
            who=who, concern=concern, analysis=last_analysis
        )
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        result = clean_markdown(response.content[0].text)
        await send_long(callback.message, result, reply_markup=after_reply_menu())
    except Exception as e:
        await callback.message.answer(f"Ошибка. Попробуй ещё раз.\n\nТехнически: {e}")
    await callback.answer()

# ─── СРАВНЕНИЕ ДИНАМИКИ ───────────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "start_compare")
async def start_compare(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(AnalysisState.waiting_for_compare)
    await state.update_data(compare_step="old")
    await callback.message.answer(
        "Сравним как он писал раньше и сейчас.\n\n"
        "Шаг 1 из 2 — отправь СТАРУЮ переписку (как было раньше)."
    )
    await callback.answer()


@dp.message(AnalysisState.waiting_for_compare, F.text)
async def compare_step(message: Message, state: FSMContext):
    data = await state.get_data()
    step = data.get("compare_step", "old")

    if step == "old":
        await state.update_data(old_text=message.text, compare_step="new")
        await message.answer(
            "Получила. Теперь шаг 2 из 2 — отправь НОВУЮ переписку (как сейчас)."
        )
        return

    # step == "new"
    if not await check_paywall(message, message.from_user.id):
        await state.clear()
        return

    old_text = data.get("old_text", "")
    await message.answer("Анализирую динамику...")

    try:
        prompt = COMPARE_PROMPT.format(
            who=data.get("who", "этот человек"),
            old_text=old_text,
            new_text=message.text
        )
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        result = response.content[0].text
        await increment_requests(message.from_user.id)
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(last_analysis=result, who="этот человек", concern="динамика")
        await send_long(message, result, reply_markup=after_menu("этот человек"))
    except Exception as e:
        await message.answer(f"Ошибка при анализе. Попробуй ещё раз.\n\nТехнически: {e}")

# ─── АНАЛИЗ МАТЕРИАЛА ─────────────────────────────────────────────────────────

async def _run_analysis(message: Message, state: FSMContext,
                        who: str, concern: str,
                        material_label: str, material: str,
                        image_b64: str = None):
    """Единый финальный шаг анализа с пейволлом и сохранением."""
    data = await state.get_data()
    user_gender = data.get("user_gender", "female")

    if not await check_paywall(message, message.from_user.id, user_gender):
        await state.clear()
        return
    try:
        result = await analyze_with_claude(
            who=who, concern=concern,
            material_label=material_label, material=material,
            image_b64=image_b64, user_gender=user_gender
        )
        await increment_requests(message.from_user.id)
        await save_analysis(message.from_user.id, who, concern, result)
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(who=who, concern=concern, last_analysis=result,
                                user_gender=user_gender)
        await send_long(message, result, reply_markup=after_menu(who))
    except Exception as e:
        await message.answer(f"Ошибка при анализе. Попробуй ещё раз.\n\nТехнически: {e}")


@dp.message(AnalysisState.waiting_for_material, F.text)
async def analyze_text(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("Анализирую...")
    await _run_analysis(
        message, state,
        who=data.get("who", "этот человек"),
        concern=data.get("concern", "подозрительное поведение"),
        material_label="Переписка:\n",
        material=message.text
    )


@dp.message(AnalysisState.waiting_for_material, F.photo)
async def analyze_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("Анализирую скриншот...")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        buf.seek(0)
        image_b64 = base64.standard_b64encode(buf.read()).decode("utf-8")
        extra = f"\nДополнительный контекст: {message.caption}" if message.caption else ""
        await _run_analysis(
            message, state,
            who=data.get("who", "этот человек"),
            concern=data.get("concern", "подозрительное поведение"),
            material_label="На скриншоте переписка. Прочитай все сообщения.\n",
            material=extra,
            image_b64=image_b64
        )
    except Exception as e:
        await message.answer(f"Не смогла прочитать скрин.\n\nТехнически: {e}")


async def _handle_voice_file(message: Message, state: FSMContext,
                              file_id: str, filename: str,
                              label: str, who: str, concern: str):
    try:
        file_info = await bot.get_file(file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        transcription = await transcribe_audio(buf.getvalue(), filename)

        if not transcription.strip():
            await message.answer("Не смогла разобрать — слишком тихо или пусто. Попробуй снова.")
            return

        await message.answer(f"Расшифровала:\n\n{transcription}\n\nАнализирую...")
        await _run_analysis(
            message, state,
            who=who, concern=concern,
            material_label=label,
            material=transcription
        )
    except RuntimeError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка при расшифровке.\n\nТехнически: {e}")


@dp.message(AnalysisState.waiting_for_material, F.voice)
async def analyze_voice(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("Расшифровываю голосовое...")
    await _handle_voice_file(
        message, state,
        file_id=message.voice.file_id,
        filename="voice.ogg",
        label="Голосовое сообщение (расшифровка):\n",
        who=data.get("who", "этот человек"),
        concern=data.get("concern", "подозрительное поведение")
    )


@dp.message(AnalysisState.waiting_for_material, F.video_note)
async def analyze_video_note(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("Расшифровываю кружок...")
    await _handle_voice_file(
        message, state,
        file_id=message.video_note.file_id,
        filename="video.mp4",
        label="Кружок (расшифровка):\n",
        who=data.get("who", "этот человек"),
        concern=data.get("concern", "подозрительное поведение")
    )

# ─── ДИАЛОГ ПОСЛЕ АНАЛИЗА ────────────────────────────────────────────────────

@dp.message(AnalysisState.post_analysis, F.text)
async def post_analysis_chat(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("Думаю...")
    try:
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Ты — эксперт по отношениям и манипуляциям. "
                        f"Говори прямо, уверенно. БЕЗ звёздочек и решёток.\n\n"
                        f"Контекст: разбирали {data.get('who','')}, "
                        f"беспокоило — {data.get('concern','')}.\n"
                        f"Анализ:\n{data.get('last_analysis','')}"
                    )
                },
                {"role": "assistant", "content": "Поняла контекст. Слушаю."},
                {"role": "user",      "content": message.text}
            ]
        )
        result = clean_markdown(response.content[0].text)
        await send_long(message, result, reply_markup=after_menu(data.get("who", "")))
    except Exception as e:
        await message.answer(f"Ошибка. Попробуй ещё раз.\n\nТехнически: {e}")


@dp.message(AnalysisState.post_analysis, F.voice)
async def post_analysis_voice(message: Message, state: FSMContext):
    await message.answer("Расшифровываю вопрос...")
    try:
        file_info = await bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        transcription = await transcribe_audio(buf.getvalue(), "voice.ogg")
        if not transcription.strip():
            await message.answer("Не смогла разобрать. Напиши текстом.")
            return
        message.text = transcription
        await post_analysis_chat(message, state)
    except RuntimeError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка при расшифровке.\n\nТехнически: {e}")

# ─── FALLBACK ─────────────────────────────────────────────────────────────────

@dp.message(F.voice | F.video_note)
async def fallback_audio(message: Message, state: FSMContext):
    if await state.get_state() is not None:
        return
    is_vn = message.video_note is not None
    await message.answer("Расшифровываю..." if not is_vn else "Расшифровываю кружок...")
    try:
        if is_vn:
            fid, fname, label = message.video_note.file_id, "video.mp4", "Кружок:\n"
        else:
            fid, fname, label = message.voice.file_id, "voice.ogg", "Голосовое:\n"
        file_info = await bot.get_file(fid)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        transcription = await transcribe_audio(buf.getvalue(), fname)
        if not transcription.strip():
            await message.answer("Не смогла разобрать. Попробуй снова.")
            return
        await message.answer(f"Расшифровала:\n\n{transcription}\n\nАнализирую...")
        await _run_analysis(
            message, state,
            who="этот человек", concern="подозрительное поведение",
            material_label=label, material=transcription
        )
    except RuntimeError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка.\n\nТехнически: {e}")


@dp.message(F.text | F.photo)
async def fallback(message: Message, state: FSMContext):
    if await state.get_state() is None:
        total = await get_total_analyses()
        await message.answer(
            f"Нажми «Начать анализ» — выбери кого разбираем и скидывай материал.\n\n"
            f"Уже разобрано {total:,} переписок.",
            reply_markup=main_menu(total)
        )

# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

async def main():
    await init_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
