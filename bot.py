import asyncio
import base64
import io
import os
import re
import time
from contextlib import asynccontextmanager

import asyncpg
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

DATABASE_URL = os.getenv("DATABASE_URL")
FREE_LIMIT = 3
COUNTER_SEED = 8341

_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                requests_count INTEGER DEFAULT 0,
                is_subscribed INTEGER DEFAULT 0,
                last_interaction BIGINT DEFAULT 0,
                reminder_sent INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT,
                who TEXT,
                concern TEXT,
                analysis_text TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS stats (
                key TEXT PRIMARY KEY,
                value INTEGER DEFAULT 0
            )
        """)
        await conn.execute("""
            INSERT INTO stats (key, value)
            VALUES ('total_analyses', $1)
            ON CONFLICT (key) DO NOTHING
        """, COUNTER_SEED)


async def get_user(telegram_id: int) -> dict:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT requests_count, is_subscribed FROM users WHERE telegram_id = $1",
                telegram_id
            )
            if row:
                return {"requests_count": row["requests_count"], "is_subscribed": row["is_subscribed"]}
            return {"requests_count": 0, "is_subscribed": 0}
    except Exception:
        return {"requests_count": 0, "is_subscribed": 0}


async def increment_requests(telegram_id: int):
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO users (telegram_id, requests_count, last_interaction)
                VALUES ($1, 1, $2)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    requests_count = users.requests_count + 1,
                    last_interaction = $2
            """, telegram_id, int(time.time()))
            await conn.execute(
                "UPDATE stats SET value = value + 1 WHERE key = 'total_analyses'"
            )
    except Exception:
        pass


async def update_last_interaction(telegram_id: int):
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_interaction = $1, reminder_sent = 0 WHERE telegram_id = $2",
                int(time.time()), telegram_id
            )
    except Exception:
        pass


async def get_users_for_reminder(hours: int = 2) -> list:
    try:
        pool = await get_pool()
        cutoff = int(time.time()) - (hours * 3600)
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT telegram_id FROM users
                WHERE last_interaction > 0
                AND last_interaction < $1
                AND reminder_sent = 0
                AND is_subscribed = 0
            """, cutoff)
            return [row["telegram_id"] for row in rows]
    except Exception:
        return []


async def mark_reminder_sent(telegram_id: int):
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET reminder_sent = 1 WHERE telegram_id = $1",
                telegram_id
            )
    except Exception:
        pass


async def save_analysis(telegram_id: int, who: str, concern: str, text: str):
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO analyses (telegram_id, who, concern, analysis_text) VALUES ($1, $2, $3, $4)",
                telegram_id, who, concern, text
            )
    except Exception:
        pass


async def get_history(telegram_id: int, limit: int = 3) -> list:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT who, concern, analysis_text, created_at
            FROM analyses
            WHERE telegram_id = $1
            ORDER BY created_at DESC
            LIMIT $2
        """, telegram_id, limit)
        return [{"who": r["who"], "concern": r["concern"], "analysis": r["analysis_text"], "date": r["created_at"]}
                for r in rows]


async def get_total_analyses() -> int:
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT value FROM stats WHERE key='total_analyses'")
            return row["value"] if row else COUNTER_SEED
    except Exception:
        return COUNTER_SEED

# ─── АНТИСПАМ ────────────────────────────────────────────────────────────────
import time
_last_request: dict[int, float] = {}
RATE_LIMIT_SECONDS = 5  # минимум секунд между анализами

# ─── АЛЬБОМЫ (несколько фото сразу) ──────────────────────────────────────────
_album_pending: dict[str, dict] = {}  # media_group_id → {photo_ids, state, message, data}

# ─── ГРУППИРОВКА ОДИНОЧНЫХ ФОТО (отправлены по очереди) ─────────────────────
_single_photo_pending: dict[int, dict] = {}  # user_id → {photo_ids, state, message, data, task, time}

# ─── ГРУППИРОВКА ТЕКСТА ──────────────────────────────────────────────────────
_text_pending: dict[int, dict] = {}  # user_id → {texts, state, message, data, task, time}

# ─── ОТЛОЖЕННЫЕ ФОТО (пришли до/после рестарта, без стейта) ──────────────────
_pending_photos: dict[int, str] = {}  # user_id → file_id последнего фото

def is_rate_limited(telegram_id: int) -> bool:
    now = time.time()
    last = _last_request.get(telegram_id, 0)
    if now - last < RATE_LIMIT_SECONDS:
        return True
    _last_request[telegram_id] = now
    return False

# ─── ПОЛ АНАЛИЗИРУЕМОГО (по выбору "кого разбираем") ────────────────────────

WHO_GENDER = {
    # female user
    "мой парень":              "он (мужчина)",
    "муж / живём вместе":      "он (мужчина)",
    "нравится, но не вместе":  "он (мужчина)",
    "бывший":                  "он (мужчина)",
    "подруга":                 "она (женщина)",
    "коллега":                 "пол неизвестен",
    "начальник":               "он (мужчина)",
    "другой человек":          "пол неизвестен",
    # male user
    "моя девушка":             "она (женщина)",
    "жена / живём вместе":     "она (женщина)",
    "бывшая":                  "она (женщина)",
    "друг":                    "он (мужчина)",
    "начальница":              "она (женщина)",
}

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
    waiting_for_situation = State()
    post_analysis = State()
    waiting_for_compare = State()   # сравнение старой переписки с новой

# ─── ПРОМПТЫ ──────────────────────────────────────────────────────────────────

SITUATION_PROMPT = """Ты — эксперт по отношениям и анализу поведения. \
Говори уверенно, {persona}.

Пользователь: {user_label}
Кого анализируем: {who} ({who_gender})
Что беспокоит: {concern}

Описание ситуации:
{situation}

Проанализируй ситуацию и дай совет. БЕЗ звёздочек и решёток. Только текст и эмодзи.

🔍 ЧТО ПРОИСХОДИТ
(твой анализ ситуации — что видишь)

⚠️ КЛЮЧЕВЫЕ МОМЕНТЫ
(2-3 важных момента на которые стоит обратить внимание)

📊 ВЕРДИКТ: Ситуация нормальная / Есть проблемы / Серьёзная проблема

💡 ЧТО ДЕЛАТЬ
(1-2 конкретных совета — что сделать в ближайшее время)"""


ANALYSIS_PROMPT = """Ты — эксперт по анализу манипуляций и лжи в переписке. \
Говори уверенно, {persona}.

Пользователь: {user_label}
Кого анализируем: {who} ({who_gender})
Что беспокоит: {concern}
{material_label}{material}

Перед анализом обрати внимание на временны́е метки если они есть: \
долгие паузы (несколько часов без ответа), резкое изменение скорости ответов, \
сообщения посреди ночи или в нетипичное время — это важные сигналы, упомяни их отдельно.

Начни с одной короткой фразы поддержки — адаптируй под ситуацию: \
если переписка тревожная — сочувствие ("понимаю, это тяжело читать"), \
если всё в порядке — ободрение ("хорошие новости"), \
если неоднозначно — нейтральное ("{resolved}"). \
Не повторяй одну и ту же фразу каждый раз.

Затем выдай анализ строго в этом формате — БЕЗ звёздочек, БЕЗ решёток, БЕЗ тире-разделителей. \
Только текст и эмодзи. Используй правильный род: {who_gender}.

Правила анализа:
— Цитируй реальные фразы из переписки как доказательства, не придумывай
— Объясняй ПОЧЕМУ конкретная фраза или пауза является сигналом — логика, не просто утверждение
— Если видишь противоречие ("говорит одно, делает другое") — назови это прямо
— Уровень тревоги ставь честно: не занижай чтобы не расстраивать, не завышай без оснований

🔍 ВЕРДИКТ
(2-3 предложения — главный вывод и одна ключевая причина)

⚠️ СИГНАЛЫ
(2-4 конкретных признака. Для каждого: цитата → что это означает → почему это тревожно или нет)

🕐 ПАУЗЫ И ВРЕМЯ
(только если есть временны́е метки: опиши паттерн ответов. Пропусти этот блок если меток нет)

📊 УРОВЕНЬ ТРЕВОГИ: Низкий / Средний / Высокий

💡 ЧТО СДЕЛАТЬ ПРЯМО СЕЙЧАС
(1 конкретное действие — не "проверь", а "напиши вот это" / "замолчи на 2 дня" / "задай прямой вопрос: ...")"""

REPLY_PROMPT = """Ты — эксперт по отношениям. На основе анализа дай 3 коротких варианта ответного сообщения.

Контекст: {who}, беспокоило — {concern}
Анализ: {analysis}

Формат — строго такой, без отступлений:

1️⃣ Мягкий
"текст сообщения"
→ Зачем: одна фраза

2️⃣ Прямой
"текст"
→ Зачем: одна фраза

3️⃣ Жёсткий
"текст"
→ Зачем: одна фраза

БЕЗ звёздочек, решёток, длинных вступлений. Только сообщение и одна строка зачем."""

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


def friendly_error(e: Exception) -> str:
    """Translate exceptions to user-friendly Russian messages."""
    import anthropic
    import openai as openai_module

    if isinstance(e, anthropic.APIStatusError):
        if e.status_code in (529, 503):
            return "Сервис временно перегружен. Попробуй через минуту."
        elif e.status_code == 401:
            return "Проблема с API ключом. Напиши @zellvany1"
    elif isinstance(e, anthropic.RateLimitError):
        return "Слишком много запросов. Подожди минуту."
    elif isinstance(e, openai_module.RateLimitError):
        return "На аккаунте OpenAI закончились кредиты. Зайди на platform.openai.com → Billing."
    elif isinstance(e, openai_module.APIStatusError):
        if e.status_code == 401:
            return "Проблема с API ключом. Напиши @zellvany1"
        elif e.status_code in (529, 503):
            return "Сервис перегружен. Попробуй через минуту."
        elif e.status_code == 400:
            return "Не удалось расшифровать голосовое. Попробуй ещё раз или напиши текстом."
    elif isinstance(e, openai_module.BadRequestError):
        return "Не удалось расшифровать голосовое. Попробуй ещё раз или напиши текстом."
    elif isinstance(e, openai_module.AuthenticationError):
        return "Проблема с API ключом. Напиши @zellvany1"
    elif isinstance(e, openai_module.APIError):
        return "Ошибка сервиса. Попробуй через минуту."

    return "Что-то пошло не так. Попробуй ещё раз."


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
        raise RuntimeError("Голосовые не настроены. Напиши @zellvany1")
    
    buf = io.BytesIO(file_bytes)
    buf.seek(0)
    
    try:
        transcript = await openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=(filename, buf),
        )
        return transcript.text
    except openai.RateLimitError:
        raise RuntimeError("На аккаунте OpenAI закончились кредиты. Зайди на platform.openai.com → Billing.")
    except openai.AuthenticationError:
        raise RuntimeError("Проблема с API ключом. Напиши @zellvany1")
    except openai.APIStatusError as e:
        if e.status_code == 400:
            raise RuntimeError("Не удалось расшифровать голосовое. Попробуй ещё раз или напиши текстом.")
        raise RuntimeError(f"Ошибка сервиса ({e.status_code}). Попробуй через минуту.")
    except Exception as e:
        raise RuntimeError(f"Не удалось расшифровать. Напиши текстом.")


async def analyze_with_claude(who: str, concern: str, material_label: str,
                               material: str, image_b64: str = None,
                               images_b64: list = None,
                               user_gender: str = "female",
                               side: str = "right") -> str:
    """
    side="right"  — правые сообщения принадлежат пользователю (Telegram по умолчанию)
    side="left"   — левые сообщения принадлежат пользователю
    """
    gf = GENDER_FORMS.get(user_gender, GENDER_FORMS["female"])
    user_label = "мужчина" if user_gender == "male" else "женщина"
    who_gender = WHO_GENDER.get(who, "пол неизвестен")

    # Нормализуем: image_b64 → images_b64
    all_images = images_b64 or ([image_b64] if image_b64 else [])

    if all_images:
        if side == "left":
            user_side, other_side = "СЛЕВА", "СПРАВА"
        else:
            user_side, other_side = "СПРАВА", "СЛЕВА"
        n = len(all_images)
        photos_note = f"{n} скриншота(ов)" if n > 1 else "скриншоте"
        screen_hint = (
            f"\nВАЖНО для {photos_note}: сообщения {user_side} — это пользователь ({user_label}). "
            f"Сообщения {other_side} — это {who} ({who_gender}). "
            f"Не путай кто есть кто при анализе."
        )
        extra_material = material + screen_hint
    else:
        # Для текстовых переписок проверяем наличие временны́х меток
        has_timestamps = bool(re.search(
            r'\b(\d{1,2}[:.]\d{2}|\d{1,2}\s*(ч|час|мин|min|am|pm)|\bвчера\b|\bсегодня\b)',
            material, re.IGNORECASE
        ))
        if has_timestamps:
            time_hint = (
                "\nВ переписке есть временны́е метки — обязательно проанализируй паттерн ответов: "
                "долгие паузы, резкое замедление, ночные сообщения."
            )
            extra_material = material + time_hint
        else:
            extra_material = material

    prompt = ANALYSIS_PROMPT.format(
        who=who, concern=concern,
        material_label=material_label, material=extra_material,
        persona=gf["persona"], resolved=gf["resolved"],
        user_label=user_label, who_gender=who_gender
    )

    if all_images:
        content = []
        for b64 in all_images:
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
            })
        content.append({"type": "text", "text": prompt})
    else:
        content = prompt

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

def who_menu(user_gender: str = "female"):
    builder = InlineKeyboardBuilder()
    if user_gender == "male":
        builder.button(text="Моя девушка",          callback_data="who_girlfriend")
        builder.button(text="Жена / живём вместе",  callback_data="who_wife")
        builder.button(text="Нравится, не вместе",  callback_data="who_crush")
        builder.button(text="Бывшая",               callback_data="who_ex")
        builder.button(text="Подруга",              callback_data="who_friend")
        builder.button(text="Коллега",              callback_data="who_colleague")
        builder.button(text="Начальница",           callback_data="who_boss")
        builder.button(text="Другой",               callback_data="who_other")
    else:
        builder.button(text="Мой парень",           callback_data="who_boyfriend")
        builder.button(text="Муж / живём вместе",   callback_data="who_husband")
        builder.button(text="Нравится, не вместе",  callback_data="who_crush")
        builder.button(text="Бывший",               callback_data="who_ex")
        builder.button(text="Подруга",              callback_data="who_friend")
        builder.button(text="Коллега",              callback_data="who_colleague")
        builder.button(text="Начальник",            callback_data="who_boss")
        builder.button(text="Другой",               callback_data="who_other")
    builder.adjust(2)
    return builder.as_markup()

def concern_menu(user_gender: str = "female"):
    builder = InlineKeyboardBuilder()
    # Глаголы меняются под пол анализируемого
    cold    = "Стала холоднее"     if user_gender == "male" else "Стал холоднее"
    ghost   = "Пропала / не отвечает" if user_gender == "male" else "Пропал / не отвечает"
    interest = "Хочу вернуть её интерес" if user_gender == "male" else "Хочу вернуть его интерес"
    attract  = "Хочу чтобы я ей нравился" if user_gender == "male" else "Хочу чтобы я ему нравилась"
    builder.button(text="Чувствую ложь",       callback_data="concern_lie")
    builder.button(text="Манипулирует мной",   callback_data="concern_manipulation")
    builder.button(text=cold,                  callback_data="concern_cold")
    builder.button(text=ghost,                 callback_data="concern_ghost")
    builder.button(text="Боюсь что изменяет",  callback_data="concern_cheat")
    builder.button(text="Что-то скрывает",     callback_data="concern_hiding")
    builder.button(text=interest,              callback_data="concern_interest")
    builder.button(text=attract,               callback_data="concern_attract")
    builder.button(text="Не понимаю как себя вести", callback_data="concern_confused")
    builder.button(text="Другое",              callback_data="concern_other")
    builder.adjust(2)
    return builder.as_markup()

def after_menu(who: str = "", show_flip: bool = False, show_ask: bool = True):
    """who — кого анализировали. show_flip=True — добавить кнопку "стороны перепутались"."""
    female_targets = {"подруга", "начальница", "моя девушка",
                      "жена / живём вместе", "бывшая"}
    him_her = "ей" if who in female_targets else "ему"
    builder = InlineKeyboardBuilder()
    builder.button(text=f"✍️ Что ответить {him_her}", callback_data="get_reply")
    builder.button(text="🔍 Разобрать другого",        callback_data="start_analysis")
    if show_ask:
        builder.button(text="💬 Задать вопрос",        callback_data="ask_question")
    if show_flip:
        builder.button(text="🔄 Мои сообщения были слева", callback_data="flip_sides")
    builder.adjust(1)
    return builder.as_markup()


def more_questions_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Да, есть вопросы",  callback_data="more_questions_yes")
    builder.button(text="❌ Нет, разобрать ещё", callback_data="more_questions_no")
    builder.adjust(1)
    return builder.as_markup()

def after_reply_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Разобрать другого", callback_data="start_analysis")
    builder.button(text="💬 Задать вопрос",     callback_data="ask_question")
    builder.adjust(1)
    return builder.as_markup()


def situation_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="📝 Описать ситуацию", callback_data="describe_situation")
    builder.button(text="📷 Скинуть скрин/переписку", callback_data="send_material")
    builder.adjust(1)
    return builder.as_markup()

# ─── /START ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await update_last_interaction(message.from_user.id)
    total = await get_total_analyses()
    total_str = f"{total:,}".replace(",", " ")
    await message.answer(
        f"Привет. Я всё знаю.\n\n"
        f"Без розовых очков. Без «ну может он просто занят».\n"
        f"Холодный взгляд на факты — скажу тебе правду.\n\n"
        f"Уже разобрано {total_str} переписок.\n\n"
        f"Скинь переписку, скрин или голосовое — разберём что происходит.",
        reply_markup=main_menu(total)
    )


@dp.message(Command("help"))
async def help_cmd(message: Message, state: FSMContext):
    await message.answer(
        "Как пользоваться:\n\n"
        "1. Нажми «Начать анализ»\n"
        "2. Выбери кого разбираем и что беспокоит\n"
        "3. Скинь переписку текстом, скриншотом или голосовым\n"
        "4. Получи анализ + черновик ответа\n\n"
        "После анализа можешь задавать вопросы — бот помнит контекст.\n\n"
        "Команды:\n"
        "/start — начать заново\n"
        "/help — эта справка\n"
        "/reset — сбросить текущий диалог",
        reply_markup=main_menu()
    )


@dp.message(Command("reset"))
async def reset_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Сброшено. Начинай заново.", reply_markup=main_menu())

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
        await callback.message.answer("Кого будем разбирать?",
                                      reply_markup=who_menu(existing_gender))
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
    
    # Проверяем есть ли pending фото
    user_id = callback.from_user.id
    has_pending = user_id in _pending_photos and _pending_photos[user_id]
    
    if has_pending:
        n = len(_pending_photos[user_id])
        await callback.message.answer(
            f"Запомнила {n} скрин(а). Кого будем разбирать?",
            reply_markup=who_menu(gender)
        )
    else:
        await callback.message.answer("Кого будем разбирать?", reply_markup=who_menu(gender))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("who_"))
async def choose_who(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    gender = data.get("user_gender", "female")
    ex_label      = "бывшая"     if gender == "male" else "бывший"
    friend_label  = "подруга"   if gender == "male" else "подруга"
    boss_label    = "начальница" if gender == "male" else "начальник"
    who_map = {
        "who_boyfriend": "мой парень",
        "who_husband":   "муж / живём вместе",
        "who_girlfriend": "моя девушка",
        "who_wife":      "жена / живём вместе",
        "who_crush":     "нравится, но не вместе",
        "who_ex":        ex_label,
        "who_friend":    friend_label,
        "who_colleague": "коллега",
        "who_boss":      boss_label,
        "who_other":     "другой человек",
    }
    await state.update_data(who=who_map.get(callback.data, "другой человек"))
    await callback.message.answer("Что беспокоит?", reply_markup=concern_menu(gender))
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("concern_"))
async def choose_concern(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    gender = data.get("user_gender", "female")
    is_male = gender == "male"   # пользователь — мужчина, значит анализируем женщину

    concern_map = {
        "concern_lie":          "чувствую ложь",
        "concern_manipulation": "манипулирует мной",
        "concern_cold":         "стала холоднее"         if is_male else "стал холоднее",
        "concern_ghost":        "пропала / не отвечает"  if is_male else "пропал / не отвечает",
        "concern_cheat":        "боюсь что изменяет",
        "concern_hiding":       "что-то скрывает",
        "concern_interest":     "хочу вернуть её интерес" if is_male else "хочу вернуть его интерес",
        "concern_attract":      "хочу чтобы я ей нравился" if is_male else "хочу чтобы я ему нравилась",
        "concern_confused":     "не понимаю как себя вести",
        "concern_other":        "другое",
    }
    await state.update_data(concern=concern_map.get(callback.data, "другое"))
    await state.set_state(AnalysisState.waiting_for_material)

    # Если до онбординга пользователь уже скинул скрин(ы) — анализируем сразу
    user_id = callback.from_user.id
    pending_photos = _pending_photos.pop(user_id, None)
    
    if pending_photos:
        if isinstance(pending_photos, str):
            pending_photos = [pending_photos]
        
        n = len(pending_photos)
        noun = "скрин" if n == 1 else ("скрина" if 2 <= n <= 4 else "скринов")
        await callback.message.answer(f"Вижу {n} {noun}. Разбираю...")
        
        fresh = await state.get_data()
        try:
            if n == 1:
                image_b64 = await _download_photo(pending_photos[0])
                await _run_analysis(
                    callback.message, state,
                    who=fresh.get("who", "этот человек"),
                    concern=fresh.get("concern", "подозрительное поведение"),
                    material_label="На скриншоте переписка. Прочитай все сообщения.\n",
                    material="",
                    image_b64=image_b64
                )
            else:
                # Несколько фото — анализируем сразу вместе
                images_b64 = []
                for fid in pending_photos:
                    try:
                        images_b64.append(await _download_photo(fid))
                    except Exception:
                        pass
                
                if images_b64:
                    await _run_analysis(
                        callback.message, state,
                        who=fresh.get("who", "этот человек"),
                        concern=fresh.get("concern", "подозрительное поведение"),
                        material_label=f"На {n} скриншотах — переписка. Прочитай все.\n",
                        material="",
                        images_b64=images_b64
                    )
                else:
                    await callback.message.answer("Не получилось скачать скрины. Попробуй ещё раз.")
        except Exception as e:
            await callback.message.answer(friendly_error(e))
        await callback.answer()
        return

    await callback.message.answer(
        "Жду материал.\n\n"
        "Скинь переписку текстом, скрин или голосовое. "
        "Или опиши ситуацию своими словами — разберём.",
        reply_markup=situation_menu()
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "describe_situation")
async def describe_situation_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AnalysisState.waiting_for_situation)
    await callback.message.answer(
        "Опиши ситуацию своими словами.\n\n"
        "Расскажи что произошло, как ведёт себя человек, что тебя беспокоит."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "send_material")
async def send_material_cb(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AnalysisState.waiting_for_material)
    await callback.message.answer(
        "Жду переписку — скинь текстом, скрин или голосовое."
    )
    await callback.answer()


@dp.message(AnalysisState.waiting_for_situation, F.text)
async def analyze_situation(message: Message, state: FSMContext):
    data = await state.get_data()
    if len(message.text) < 20:
        await message.answer("Опиши подробнее, пожалуйста.")
        return
    await _analyze_as_situation(message, state, data, message.text)


@dp.callback_query(lambda c: c.data == "ask_question")
async def ask_question_cb(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("last_analysis"):
        await callback.message.answer(
            "Сначала скинь переписку — я её разберу, а потом задавай вопросы.",
            reply_markup=main_menu()
        )
        await callback.answer()
        return
    # Сбрасываем счётчик вопросов при явном нажатии "Задать вопрос"
    await state.update_data(questions_count=0)
    await state.set_state(AnalysisState.post_analysis)
    await callback.message.answer("Задавай вопрос по этому анализу.")
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
        await callback.message.answer(friendly_error(e))
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
        await message.answer(friendly_error(e))


@dp.message(AnalysisState.waiting_for_compare, F.photo)
async def compare_photo(message: Message, state: FSMContext):
    await message.answer(
        "В режиме сравнения нужен текст, не скрин.\n\n"
        "Скопируй переписку текстом и вставь сюда."
    )

# ─── АНАЛИЗ МАТЕРИАЛА ─────────────────────────────────────────────────────────

async def _download_photo(file_id: str) -> str:
    """Скачивает фото по file_id и возвращает base64."""
    file_info = await bot.get_file(file_id)
    buf = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=buf)
    buf.seek(0)
    return base64.standard_b64encode(buf.read()).decode("utf-8")


async def _flush_album(mgid: str):
    """Ждёт 1.2с пока придут все фото альбома, потом анализирует их вместе."""
    await asyncio.sleep(1.2)
    info = _album_pending.pop(mgid, None)
    if not info:
        return

    message: Message = info["message"]
    state: FSMContext = info["state"]
    data: dict      = info["data"]
    photo_ids: list = info["photo_ids"][:5]
    force_side = info.get("force_side")

    n = len(photo_ids)
    if n > 1:
        await message.answer(
            f"⚠️ В альбоме {n} фото. "
            f"Все будут разобраны как один человек.\n"
            f"Если хочешь разобрать разных — отправь их по отдельности."
        )
        await message.answer("Разбираю все фото вместе...")
    else:
        await message.answer("Разбираю скриншот...")

    try:
        images_b64 = []
        for fid in photo_ids:
            try:
                images_b64.append(await _download_photo(fid))
            except Exception:
                pass

        if not images_b64:
            await message.answer("Не получилось скачать скрины. Попробуй по одному.")
            return

        extra = f"\nДополнительный контекст: {message.caption}" if message.caption else ""
        await _run_analysis(
            message, state,
            who=data.get("who", "этот человек"),
            concern=data.get("concern", "подозрительное поведение"),
            material_label=f"На {n} скриншоте(ах) — переписка в хронологическом порядке. Прочитай все.\n",
            material=extra,
            images_b64=images_b64,
            side=force_side or "right"
        )
    except Exception as e:
        await message.answer(friendly_error(e))


async def _analyze_as_situation(message: Message, state: FSMContext, data: dict, text: str):
    """Анализирует текст как описание ситуации."""
    if is_rate_limited(message.from_user.id):
        await message.answer("Подожди немного перед следующим запросом.")
        return
    
    user_gender = data.get("user_gender", "female")
    gf = GENDER_FORMS.get(user_gender, GENDER_FORMS["female"])
    user_label = "мужчина" if user_gender == "male" else "женщина"
    who_gender = WHO_GENDER.get(data.get("who", ""), "пол неизвестен")
    
    await message.answer("Анализирую ситуацию...")
    
    try:
        prompt = SITUATION_PROMPT.format(
            persona=gf["persona"],
            user_label=user_label,
            who=data.get("who", "этот человек"),
            who_gender=who_gender,
            concern=data.get("concern", ""),
            situation=text
        )
        
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        result = clean_markdown(response.content[0].text)
        
        await increment_requests(message.from_user.id)
        await update_last_interaction(message.from_user.id)
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(
            who=data.get("who"),
            concern=data.get("concern"),
            last_analysis=result,
            user_gender=user_gender,
            is_photo=False,
            questions_count=0,
            chat_history=[]
        )
        await send_long(message, result, reply_markup=after_menu(data.get("who", ""), show_flip=False))
    except Exception as e:
        await message.answer(friendly_error(e))


async def _run_analysis(message: Message, state: FSMContext,
                        who: str, concern: str,
                        material_label: str, material: str,
                        image_b64: str = None, images_b64: list = None,
                        side: str = "right"):
    """Единый финальный шаг анализа с пейволлом и сохранением."""
    if is_rate_limited(message.from_user.id):
        await message.answer("Подожди немного перед следующим запросом.")
        return
    data = await state.get_data()
    user_gender = data.get("user_gender", "female")

    if not await check_paywall(message, message.from_user.id, user_gender):
        await state.clear()
        return
    is_photo = bool(image_b64 or images_b64)
    try:
        result = await analyze_with_claude(
            who=who, concern=concern,
            material_label=material_label, material=material,
            image_b64=image_b64, images_b64=images_b64,
            user_gender=user_gender, side=side
        )
        await increment_requests(message.from_user.id)
        await update_last_interaction(message.from_user.id)
        await save_analysis(message.from_user.id, who, concern, result)
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(who=who, concern=concern, last_analysis=result,
                                user_gender=user_gender, is_photo=is_photo,
                                last_side=side, last_material=material,
                                last_material_label=material_label,
                                questions_count=0, chat_history=[])
        await send_long(message, result,
                        reply_markup=after_menu(who, show_flip=is_photo))
    except Exception as e:
        await message.answer(friendly_error(e))


@dp.message(AnalysisState.waiting_for_material, F.text)
async def analyze_text(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("who"):
        await message.answer(
            "Сначала выбери кого разбираем — нажми «Начать анализ».",
            reply_markup=main_menu()
        )
        await state.clear()
        return

    user_id = message.from_user.id
    now = time.time()

    # Длинный текст (описание ситуации) — анализируем сразу как ситуацию
    if len(message.text) > 100 and len(message.text) <= 8000:
        await _analyze_as_situation(message, state, data, message.text)
        return

    # Группируем текст в течение 3 сек
    if user_id in _text_pending:
        pending = _text_pending[user_id]
        prev = pending.get("task")
        if prev and not prev.done():
            prev.cancel()
        pending["texts"].append(message.text)
        pending["time"] = now
        pending["task"] = asyncio.create_task(_flush_text(user_id))
        return

    # Первое сообщение — запускаем таймер
    if len(message.text) > 8000:
        await message.answer(
            "Переписка слишком длинная — скопируй самый важный кусок (последние 30-50 сообщений)."
        )
        return

    _text_pending[user_id] = {
        "texts":  [message.text],
        "state":  state,
        "message": message,
        "data":   data,
        "task":   None,
        "time":   now,
    }
    _text_pending[user_id]["task"] = asyncio.create_task(_flush_text(user_id))


async def _flush_text(user_id: int):
    """Ждём 3 сек пока придут все текстовые сообщения, потом анализируем вместе."""
    await asyncio.sleep(3)
    info = _text_pending.pop(user_id, None)
    if not info:
        return

    message: Message = info["message"]
    state: FSMContext = info["state"]
    data: dict = info["data"]
    texts: list = info["texts"]

    combined_text = "\n\n".join(texts)
    n = len(texts)

    if n == 1:
        await message.answer("Анализирую...")
    else:
        await message.answer(f"Разбираю {n} сообщений...")

    try:
        await _run_analysis(
            message, state,
            who=data.get("who", "этот человек"),
            concern=data.get("concern", "подозрительное поведение"),
            material_label="Переписка:\n",
            material=combined_text
        )
    except Exception as e:
        await message.answer(friendly_error(e))


@dp.message(AnalysisState.waiting_for_material, F.photo)
async def analyze_photo(message: Message, state: FSMContext):
    data = await state.get_data()

    if message.media_group_id:
        mgid = message.media_group_id
        if mgid not in _album_pending:
            _album_pending[mgid] = {
                "photo_ids":   [],
                "state":       state,
                "message":     message,
                "data":        data,
                "task":        None,
                "force_side":  data.get("force_side"),
            }
        _album_pending[mgid]["photo_ids"].append(message.photo[-1].file_id)

        # Перезапускаем таймер — ждём ещё 1.2с с последнего фото
        prev = _album_pending[mgid].get("task")
        if prev and not prev.done():
            prev.cancel()
        _album_pending[mgid]["task"] = asyncio.create_task(_flush_album(mgid))
        return

    # Одиночное фото — группируем если пришло в течение 3 сек после предыдущего
    user_id = message.from_user.id
    now = time.time()
    
    if user_id in _single_photo_pending:
        pending = _single_photo_pending[user_id]
        prev = pending.get("task")
        if prev and not prev.done():
            prev.cancel()
        pending["photo_ids"].append(message.photo[-1].file_id)
        if message.caption:
            pending["caption"] = message.caption
        pending["time"] = now
        pending["task"] = asyncio.create_task(_flush_single_photos(user_id))
        return

    # Первое фото — запускаем таймер ожидания
    side = data.get("force_side", "right")
    await state.update_data(force_side=None)
    
    _single_photo_pending[user_id] = {
        "photo_ids":  [message.photo[-1].file_id],
        "state":      state,
        "message":    message,
        "data":       data,
        "force_side": side,
        "caption":    message.caption if message.caption else None,
        "task":       None,
        "time":       now,
    }
    _single_photo_pending[user_id]["task"] = asyncio.create_task(_flush_single_photos(user_id))


async def _flush_single_photos(user_id: int):
    """Ждём 3 сек пока придут все фото от одного пользователя, потом анализируем вместе."""
    await asyncio.sleep(3)
    info = _single_photo_pending.pop(user_id, None)
    if not info:
        return

    message: Message = info["message"]
    state: FSMContext = info["state"]
    data: dict = info["data"]
    photo_ids: list = info["photo_ids"][:5]
    force_side = info.get("force_side", "right")
    caption = info.get("caption")

    n = len(photo_ids)
    if n == 1:
        await message.answer("Разбираю скриншот...")
        try:
            image_b64 = await _download_photo(photo_ids[0])
            extra = f"\nДополнительный контекст: {caption}" if caption else ""
            await _run_analysis(
                message, state,
                who=data.get("who", "этот человек"),
                concern=data.get("concern", "подозрительное поведение"),
                material_label="На скриншоте переписка. Прочитай все сообщения.\n",
                material=extra,
                image_b64=image_b64,
                side=force_side
            )
        except Exception as e:
            await message.answer(friendly_error(e))
    else:
        if n > 1:
            await message.answer(
                f"⚠️ В альбоме {n} фото. "
                f"Все будут разобраны как один человек.\n"
                f"Если хочешь разобрать разных — отправь их по отдельности."
            )
        await message.answer("Разбираю все фото вместе...")
        try:
            images_b64 = []
            for fid in photo_ids:
                try:
                    images_b64.append(await _download_photo(fid))
                except Exception:
                    pass

            if not images_b64:
                await message.answer("Не получилось скачать скрины. Попробуй по одному.")
                return

            extra = f"\nДополнительный контекст: {caption}" if caption else ""
            await _run_analysis(
                message, state,
                who=data.get("who", "этот человек"),
                concern=data.get("concern", "подозрительное поведение"),
                material_label=f"На {n} скриншоте(ах) — переписка в хронологическом порядке. Прочитай все.\n",
                material=extra,
                images_b64=images_b64,
                side=force_side
            )
        except Exception as e:
            await message.answer(friendly_error(e))


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
        await message.answer(friendly_error(e))


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

MAX_QUESTIONS = 2  # максимум вопросов без анализа новой переписки

@dp.message(AnalysisState.post_analysis, F.text)
async def post_analysis_chat(message: Message, state: FSMContext):
    await state.set_state(AnalysisState.post_analysis)
    data = await state.get_data()

    if not data.get("last_analysis"):
        await message.answer(
            "Потеряла контекст после перезапуска.\n\n"
            "Напиши вопрос сюда — @zellvany1"
        )
        await state.clear()
        return

    if len(message.text) > 400:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔍 Да, разобрать это", callback_data="reanalyze_text")
        builder.button(text="💬 Нет, это вопрос", callback_data="dismiss_reanalyze")
        builder.adjust(1)
        await state.update_data(pending_text=message.text)
        await message.answer(
            "Похоже, ты скинула новую переписку.\nРазобрать её?",
            reply_markup=builder.as_markup()
        )
        return

    user_gender = data.get("user_gender", "female")
    gf = GENDER_FORMS.get(user_gender, GENDER_FORMS["female"])
    questions_count = data.get("questions_count", 0)

    if questions_count >= MAX_QUESTIONS:
        await message.answer(
            "Ты уже задал(а) максимум вопросов по этому анализу.\n\n"
            "Если хочешь разобрать ещё — скинь новую переписку.",
            reply_markup=after_menu(data.get("who", ""), show_ask=False)
        )
        return

    await message.answer("Думаю...")

    # Собираем историю переписки
    chat_history = data.get("chat_history", [])
    messages_for_claude = [
        {
            "role": "user",
            "content": (
                f"Ты — эксперт по отношениям и манипуляциям. "
                f"Говори {gf['persona']}. БЕЗ звёздочек и решёток.\n\n"
                f"Контекст: разбирали {data.get('who','')}, "
                f"беспокоило — {data.get('concern','')}.\n"
                f"Анализ:\n{data.get('last_analysis','')}"
            )
        }
    ]
    for q, a in chat_history:
        messages_for_claude.append({"role": "user", "content": q})
        messages_for_claude.append({"role": "assistant", "content": a})
    messages_for_claude.append({"role": "user", "content": message.text})

    try:
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=messages_for_claude
        )
        result = clean_markdown(response.content[0].text)

        # Обновляем историю
        new_history = chat_history + [(message.text, result)]
        new_count = questions_count + 1

        await state.update_data(
            chat_history=new_history,
            questions_count=new_count
        )
        await state.set_state(AnalysisState.post_analysis)
        await update_last_interaction(message.from_user.id)

        if new_count >= MAX_QUESTIONS:
            await send_long(
                message, result,
                reply_markup=after_menu(data.get("who", ""), show_ask=False)
            )
        else:
            remaining = MAX_QUESTIONS - new_count
            await send_long(message, result)
            await message.answer(
                f"Будут ещё вопросы по этому человеку? "
                f"(осталось {remaining} {'вопрос' if remaining == 1 else 'вопроса'})",
                reply_markup=more_questions_menu()
            )
    except Exception as e:
        await state.set_state(AnalysisState.post_analysis)
        await message.answer(friendly_error(e))


@dp.callback_query(lambda c: c.data == "reanalyze_text")
async def reanalyze_text_cb(callback: CallbackQuery, state: FSMContext):
    """Пользователь подтвердил что хочет разобрать новый текст."""
    data = await state.get_data()
    pending = data.get("pending_text", "")
    if not pending:
        await callback.message.answer("Текст не найден. Отправь снова.")
        await callback.answer()
        return
    await state.update_data(pending_text=None)
    await callback.message.answer("Анализирую...")
    await _run_analysis(
        callback.message, state,
        who=data.get("who", "этот человек"),
        concern=data.get("concern", "подозрительное поведение"),
        material_label="Переписка:\n",
        material=pending
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "dismiss_reanalyze")
async def dismiss_reanalyze_cb(callback: CallbackQuery, state: FSMContext):
    """Пользователь сказал что это вопрос, а не новая переписка — отвечаем в контексте."""
    data = await state.get_data()
    pending = data.get("pending_text", "")
    await state.update_data(pending_text=None)

    user_gender = data.get("user_gender", "female")
    gf = GENDER_FORMS.get(user_gender, GENDER_FORMS["female"])

    await callback.message.answer("Думаю...")
    try:
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Ты — эксперт по отношениям и манипуляциям. "
                        f"Говори {gf['persona']}. БЕЗ звёздочек и решёток.\n\n"
                        f"Контекст: разбирали {data.get('who','')}, "
                        f"беспокоило — {data.get('concern','')}.\n"
                        f"Анализ:\n{data.get('last_analysis','')}"
                    )
                },
                {"role": "assistant", "content": "Поняла контекст. Слушаю."},
                {"role": "user",      "content": pending}
            ]
        )
        result = clean_markdown(response.content[0].text)
        await state.set_state(AnalysisState.post_analysis)
        await send_long(callback.message, result,
                        reply_markup=after_menu(data.get("who", "")))
    except Exception as e:
        await state.set_state(AnalysisState.post_analysis)
        await callback.message.answer(friendly_error(e))
    await callback.answer()


@dp.callback_query(lambda c: c.data == "more_questions_yes")
async def more_questions_yes(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    remaining = MAX_QUESTIONS - data.get("questions_count", 0)
    if remaining <= 0:
        await callback.message.answer(
            "Ты уже задал(а) максимум вопросов по этому анализу.",
            reply_markup=after_menu(data.get("who", ""), show_ask=False)
        )
        await callback.answer()
        return
    await callback.message.answer(
        f"Задавай вопросы — разберём ситуацию.\n"
        f"(осталось {remaining} {'вопрос' if remaining == 1 else 'вопроса'})"
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "more_questions_no")
async def more_questions_no(callback: CallbackQuery, state: FSMContext):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔍 Разобрать другого", callback_data="start_analysis")
    builder.button(text="🏠 Главное меню", callback_data="go_home")
    builder.adjust(1)
    await callback.message.answer(
        "Выбери что делать дальше:",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "go_home")
async def go_home(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    total = await get_total_analyses()
    total_str = f"{total:,}".replace(",", " ")
    await callback.message.answer(
        f"Привет. Я всё знаю.\n\n"
        f"Без розовых очков. Без «ну может он просто занят».\n"
        f"Холодный взгляд на факты — скажу тебе правду.\n\n"
        f"Уже разобрано {total_str} переписок.\n\n"
        f"Скинь переписку, скрин или голосовое — разберём что происходит.",
        reply_markup=main_menu(total)
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "flip_sides")
async def flip_sides_cb(callback: CallbackQuery, state: FSMContext):
    """Пользователь говорит что его сообщения были СЛЕВА — переанализируем."""
    data = await state.get_data()
    who     = data.get("who", "этот человек")
    concern = data.get("concern", "подозрительное поведение")
    last_side = data.get("last_side", "right")

    if last_side == "left":
        await callback.message.answer("Уже анализировала с левой стороной. Попробуй скинуть скрин заново.")
        await callback.answer()
        return

    await callback.message.answer("Пересчитываю с правильными сторонами...")
    try:
        # Перечитываем изображение из Telegram если возможно — но оно уже не хранится.
        # Вместо этого: запрашиваем у пользователя скрин ещё раз с пометкой о сторонах.
        await state.set_state(AnalysisState.waiting_for_material)
        await state.update_data(force_side="left")
        await callback.message.answer(
            "Скинь скриншот ещё раз — теперь буду считать что твои сообщения слева."
        )
    except Exception as e:
        await callback.message.answer(friendly_error(e))
    await callback.answer()


@dp.message(AnalysisState.post_analysis, F.video_note)
async def post_analysis_video_note(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("last_analysis"):
        await message.answer(
            "Сначала скинь переписку для анализа.",
            reply_markup=main_menu()
        )
        await state.clear()
        return
    await message.answer("Расшифровываю кружок...")
    try:
        file_info = await bot.get_file(message.video_note.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        transcription = await transcribe_audio(buf.getvalue(), "video.mp4")
        if not transcription.strip():
            await message.answer("Не смогла разобрать. Напиши текстом.")
            return
        message.text = transcription
        await post_analysis_chat(message, state)
    except RuntimeError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(friendly_error(e))

# ─── FALLBACK ─────────────────────────────────────────────────────────────────

@dp.message(F.voice | F.video_note)
async def fallback_audio(message: Message, state: FSMContext):
    # Только если нет активного состояния — просим начать сначала
    current_state = await state.get_state()
    if current_state is not None:
        return
    is_vn = message.video_note is not None
    label = "кружок" if is_vn else "голосовое"
    await message.answer(
        f"Сначала выбери кого разбираем — нажми «Начать анализ», "
        f"потом скидывай {label}.",
        reply_markup=main_menu()
    )


@dp.message(AnalysisState.post_analysis, F.voice)
async def post_analysis_voice(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("last_analysis"):
        await message.answer(
            "Сначала скинь переписку для анализа.",
            reply_markup=main_menu()
        )
        await state.clear()
        return
    await message.answer("Расшифровываю голосовое...")
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
        await message.answer(friendly_error(e))


@dp.message(F.text | F.photo)
async def fallback(message: Message, state: FSMContext):
    if await state.get_state() is None:
        total = await get_total_analyses()
        total_str = f"{total:,}".replace(",", " ")

        # Фото без стейта — запоминаем и запускаем онбординг
        if message.photo:
            user_id = message.from_user.id
            is_first = user_id not in _pending_photos or not _pending_photos[user_id]
            
            if user_id not in _pending_photos:
                _pending_photos[user_id] = []
            
            _pending_photos[user_id].append(message.photo[-1].file_id)
            n = len(_pending_photos[user_id])
            
            if is_first:
                await message.answer(
                    f"Вижу {n} скрин. Сейчас разберём.\n\n"
                    "Потеряла контекст после перезапуска, нужно уточнить пару вещей.",
                    reply_markup=gender_menu()
                )
            else:
                noun = "скрин" if n == 1 else ("скрина" if 2 <= n <= 4 else "скринов")
                await message.answer(
                    f"Вижу {n} {noun}. Жду когда выберешь пол и кого разбираем."
                )
            return

        await message.answer(
            f"Нажми «Начать анализ» — выбери кого разбираем и скидывай материал.\n\n"
            f"Уже разобрано {total_str} переписок.",
            reply_markup=main_menu(total)
        )

# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

# ─── ФОНОВАЯ ЗАДАЧА: НАПОМИНАНИЯ ──────────────────────────────────────────────

REMINDER_TEXT = """Привет! 👋

У тебя остались вопросы по разобранным перепискам?

Не оставляй ситуацию без ответа — скинь новую переписку и я помогу разобраться.

💡 Помню контекст предыдущих анализов — могу сравнить динамику."""

async def send_reminders():
    """Проверяет пользователей и отправляет напоминания через 2 часа."""
    while True:
        await asyncio.sleep(300)
        try:
            users = await get_users_for_reminder(hours=2)
            for user_id in users:
                try:
                    await bot.send_message(
                        user_id,
                        REMINDER_TEXT,
                        reply_markup=main_menu()
                    )
                    await mark_reminder_sent(user_id)
                except Exception:
                    pass
        except Exception:
            pass


async def main():
    try:
        await init_db()
        asyncio.create_task(send_reminders())
        await dp.start_polling(bot)
    finally:
        await close_pool()

if __name__ == "__main__":
    asyncio.run(main())
