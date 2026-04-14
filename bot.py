import asyncio
import base64
import io
import os
import re

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

# OpenAI для Whisper — инициализируем только если ключ есть
_openai_key = os.getenv("OPENAI_API_KEY")
openai_client = openai.AsyncOpenAI(api_key=_openai_key) if _openai_key else None

# ─── FSM ──────────────────────────────────────────────────────────────────────

class AnalysisState(StatesGroup):
    waiting_for_material = State()   # ждём текст / фото / голосовое
    post_analysis = State()          # свободный диалог после анализа

# ─── ПРОМПТ ───────────────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """Ты — эксперт по анализу манипуляций и лжи в переписке. \
Говори уверенно, как умная подруга которая не жалеет, а говорит правду.

Кого анализируем: {who}
Что беспокоит: {concern}
{material_label}{material}

Выдай анализ строго в этом формате — БЕЗ звёздочек, БЕЗ решёток, БЕЗ тире-разделителей. \
Только текст и эмодзи:

🔍 ВЕРДИКТ
(1-2 предложения, прямо и по делу)

⚠️ СИГНАЛЫ
(2-4 конкретных признака с цитатами и объяснением)

📊 УРОВЕНЬ ТРЕВОГИ: Низкий / Средний / Высокий

💡 ЧТО ПРОВЕРИТЬ
(1-2 конкретных действия)"""

# ─── ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ──────────────────────────────────────────────────

def clean_markdown(text: str) -> str:
    """Убирает markdown на случай если Claude всё равно его вернул."""
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)   # **жирный** → жирный
    text = re.sub(r'\*(.*?)\*', r'\1', text)        # *курсив* → курсив
    text = re.sub(r'#{1,6}\s+', '', text)           # ## Заголовок → Заголовок
    text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)  # --- разделители
    return text.strip()


async def send_long_message(message: Message, text: str, **kwargs):
    """Отправляет сообщение, разбивая на части если > 4096 символов."""
    text = clean_markdown(text)
    if len(text) <= 4000:
        await message.answer(text, **kwargs)
        return

    # Разбиваем по абзацам
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
    """Расшифровывает аудио через OpenAI Whisper."""
    if not openai_client:
        raise RuntimeError(
            "Голосовые не настроены. Добавь OPENAI_API_KEY в переменные на Railway."
        )
    buf = io.BytesIO(file_bytes)
    transcript = await openai_client.audio.transcriptions.create(
        model="whisper-1",
        file=(filename, buf),
        language="ru"
    )
    return transcript.text


async def analyze_with_claude(who: str, concern: str, material_label: str,
                               material: str, image_b64: str = None) -> str:
    """Единая функция анализа — текст или текст+картинка."""
    prompt = ANALYSIS_PROMPT.format(
        who=who,
        concern=concern,
        material_label=material_label,
        material=material
    )

    if image_b64:
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": image_b64,
                },
            },
            {"type": "text", "text": prompt}
        ]
    else:
        content = prompt

    response = await claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": content}]
    )
    return response.content[0].text

# ─── КЛАВИАТУРЫ ───────────────────────────────────────────────────────────────

def main_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Начать анализ", callback_data="start_analysis")
    builder.button(text="Как это работает?", callback_data="how_it_works")
    builder.adjust(1)
    return builder.as_markup()

def who_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Парень / муж", callback_data="who_boyfriend")
    builder.button(text="Подруга", callback_data="who_friend")
    builder.button(text="Коллега", callback_data="who_colleague")
    builder.button(text="Другой", callback_data="who_other")
    builder.adjust(2)
    return builder.as_markup()

def concern_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Чувствую ложь", callback_data="concern_lie")
    builder.button(text="Манипуляции", callback_data="concern_manipulation")
    builder.button(text="Стал холоднее", callback_data="concern_cold")
    builder.button(text="Другое", callback_data="concern_other")
    builder.adjust(2)
    return builder.as_markup()

def after_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Разобрать другого", callback_data="start_analysis")
    builder.button(text="Задать вопрос", callback_data="ask_question")
    builder.adjust(1)
    return builder.as_markup()

# ─── /START ───────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет. Я всё знаю.\n\n"
        "Без розовых очков. Без «ну может он просто занят».\n"
        "Холодный взгляд на факты — скажу тебе правду.\n\n"
        "Скинь переписку, скрин или голосовое — разберём что происходит.",
        reply_markup=main_menu()
    )

# ─── КОЛБЭКИ ──────────────────────────────────────────────────────────────────

@dp.callback_query(lambda c: c.data == "how_it_works")
async def how_it_works(callback: CallbackQuery):
    await callback.message.answer(
        "Ты скидываешь переписку, скрин или голосовое.\n"
        "Я анализирую — нахожу признаки лжи и манипуляций.\n"
        "Выдаю вердикт с цитатами.\n\n"
        "Переписки не хранятся. Всё конфиденциально."
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "start_analysis")
async def start_analysis(callback: CallbackQuery, state: FSMContext):
    await state.clear()
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
        "concern_lie":           "чувствую ложь",
        "concern_manipulation":  "манипуляции",
        "concern_cold":          "стал холоднее",
        "concern_other":         "другое"
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
    """Кнопка 'Задать вопрос' — остаёмся в post_analysis, контекст сохранён."""
    current = await state.get_state()
    if current != AnalysisState.post_analysis:
        await state.set_state(AnalysisState.post_analysis)
    await callback.message.answer("Задавай — я в контексте, отвечу.")
    await callback.answer()

# ─── АНАЛИЗ ТЕКСТА ────────────────────────────────────────────────────────────

@dp.message(AnalysisState.waiting_for_material, F.text)
async def analyze_text(message: Message, state: FSMContext):
    data = await state.get_data()
    who     = data.get("who", "этот человек")
    concern = data.get("concern", "подозрительное поведение")

    await message.answer("Анализирую...")
    try:
        result = await analyze_with_claude(
            who=who, concern=concern,
            material_label="Переписка:\n",
            material=message.text
        )
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(last_analysis=result)
        await send_long_message(message, result, reply_markup=after_menu())
    except Exception as e:
        await message.answer(f"Что-то пошло не так при анализе. Попробуй ещё раз.\n\nТехнически: {e}")

# ─── АНАЛИЗ ФОТО (СКРИНШОТ) ───────────────────────────────────────────────────

@dp.message(AnalysisState.waiting_for_material, F.photo)
async def analyze_photo(message: Message, state: FSMContext):
    data = await state.get_data()
    who     = data.get("who", "этот человек")
    concern = data.get("concern", "подозрительное поведение")

    await message.answer("Анализирую скриншот...")
    try:
        photo    = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        buf.seek(0)
        image_b64 = base64.standard_b64encode(buf.read()).decode("utf-8")

        # Подпись к скрину — дополнительный контекст
        extra = f"\nДополнительный контекст: {message.caption}" if message.caption else ""

        result = await analyze_with_claude(
            who=who, concern=concern,
            material_label="На скриншоте переписка. Прочитай все сообщения.\n",
            material=extra,
            image_b64=image_b64
        )
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(last_analysis=result)
        await send_long_message(message, result, reply_markup=after_menu())
    except Exception as e:
        await message.answer(f"Не смогла прочитать скрин. Попробуй ещё раз.\n\nТехнически: {e}")

# ─── АНАЛИЗ ГОЛОСОВОГО СООБЩЕНИЯ (ГС) ────────────────────────────────────────

@dp.message(AnalysisState.waiting_for_material, F.voice)
async def analyze_voice(message: Message, state: FSMContext):
    data = await state.get_data()
    who     = data.get("who", "этот человек")
    concern = data.get("concern", "подозрительное поведение")

    await message.answer("Расшифровываю голосовое...")
    try:
        file_info = await bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        audio_bytes = buf.getvalue()

        transcription = await transcribe_audio(audio_bytes, "voice.ogg")

        if not transcription.strip():
            await message.answer("Не смогла разобрать голосовое — слишком тихо или пусто. Попробуй снова.")
            return

        await message.answer(f"Расшифровала:\n\n{transcription}\n\nАнализирую...")

        result = await analyze_with_claude(
            who=who, concern=concern,
            material_label="Голосовое сообщение (расшифровка):\n",
            material=transcription
        )
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(last_analysis=result, transcription=transcription)
        await send_long_message(message, result, reply_markup=after_menu())
    except RuntimeError as e:
        # OpenAI ключ не настроен
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка при расшифровке. Попробуй ещё раз.\n\nТехнически: {e}")

# ─── АНАЛИЗ КРУЖКА (VIDEO NOTE) ───────────────────────────────────────────────

@dp.message(AnalysisState.waiting_for_material, F.video_note)
async def analyze_video_note(message: Message, state: FSMContext):
    data = await state.get_data()
    who     = data.get("who", "этот человек")
    concern = data.get("concern", "подозрительное поведение")

    await message.answer("Расшифровываю кружок...")
    try:
        file_info = await bot.get_file(message.video_note.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        audio_bytes = buf.getvalue()

        transcription = await transcribe_audio(audio_bytes, "video.mp4")

        if not transcription.strip():
            await message.answer("Не смогла разобрать кружок — слишком тихо или пусто. Попробуй снова.")
            return

        await message.answer(f"Расшифровала:\n\n{transcription}\n\nАнализирую...")

        result = await analyze_with_claude(
            who=who, concern=concern,
            material_label="Кружок (расшифровка):\n",
            material=transcription
        )
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(last_analysis=result, transcription=transcription)
        await send_long_message(message, result, reply_markup=after_menu())
    except RuntimeError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка при расшифровке кружка. Попробуй ещё раз.\n\nТехнически: {e}")

# ─── ДИАЛОГ ПОСЛЕ АНАЛИЗА ────────────────────────────────────────────────────

@dp.message(AnalysisState.post_analysis, F.text)
async def post_analysis_chat(message: Message, state: FSMContext):
    """Свободный диалог — бот в контексте прошлого анализа."""
    data = await state.get_data()
    who           = data.get("who", "этот человек")
    concern       = data.get("concern", "")
    last_analysis = data.get("last_analysis", "")

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
                        f"Говори прямо, как умная подруга. Без звёздочек и решёток.\n\n"
                        f"Контекст: разбирали {who}, беспокоило — {concern}.\n"
                        f"Результат анализа:\n{last_analysis}"
                    )
                },
                {"role": "assistant", "content": "Поняла контекст. Слушаю."},
                {"role": "user",      "content": message.text}
            ]
        )
        result = clean_markdown(response.content[0].text)
        # Остаёмся в post_analysis — можно задавать несколько вопросов подряд
        await send_long_message(message, result, reply_markup=after_menu())
    except Exception as e:
        await message.answer(f"Ошибка. Попробуй ещё раз.\n\nТехнически: {e}")


@dp.message(AnalysisState.post_analysis, F.voice)
async def post_analysis_voice(message: Message, state: FSMContext):
    """Голосовой вопрос после анализа."""
    await message.answer("Расшифровываю вопрос...")
    try:
        file_info = await bot.get_file(message.voice.file_id)
        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        transcription = await transcribe_audio(buf.getvalue(), "voice.ogg")

        if not transcription.strip():
            await message.answer("Не смогла разобрать. Попробуй написать текстом.")
            return

        # Подменяем message.text и вызываем обработчик диалога
        message.text = transcription
        await post_analysis_chat(message, state)
    except RuntimeError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка при расшифровке. Попробуй текстом.\n\nТехнически: {e}")

# ─── FALLBACK: медиа вне FSM (пользователь не прошёл онбординг) ──────────────

@dp.message(F.voice | F.video_note)
async def fallback_audio(message: Message, state: FSMContext):
    """Голосовое/кружок без онбординга — расшифруем и проанализируем с дефолтным контекстом."""
    current = await state.get_state()
    if current is not None:
        return  # уже обрабатывается другим хендлером

    is_video_note = message.video_note is not None
    await message.answer("Расшифровываю..." if not is_video_note else "Расшифровываю кружок...")

    try:
        if is_video_note:
            file_info = await bot.get_file(message.video_note.file_id)
            fname = "video.mp4"
            label = "Кружок (расшифровка):\n"
        else:
            file_info = await bot.get_file(message.voice.file_id)
            fname = "voice.ogg"
            label = "Голосовое (расшифровка):\n"

        buf = io.BytesIO()
        await bot.download_file(file_info.file_path, destination=buf)
        transcription = await transcribe_audio(buf.getvalue(), fname)

        if not transcription.strip():
            await message.answer("Не смогла разобрать. Попробуй снова.")
            return

        await message.answer(f"Расшифровала:\n\n{transcription}\n\nАнализирую...")

        result = await analyze_with_claude(
            who="этот человек", concern="подозрительное поведение",
            material_label=label, material=transcription
        )
        await state.set_state(AnalysisState.post_analysis)
        await state.update_data(
            who="этот человек", concern="подозрительное поведение",
            last_analysis=result
        )
        await send_long_message(message, result, reply_markup=after_menu())
    except RuntimeError as e:
        await message.answer(str(e))
    except Exception as e:
        await message.answer(f"Ошибка. Попробуй ещё раз.\n\nТехнически: {e}")


@dp.message(F.text | F.photo)
async def fallback(message: Message, state: FSMContext):
    """Текст/фото без онбординга — направляем в меню."""
    current = await state.get_state()
    if current is None:
        await message.answer(
            "Нажми «Начать анализ» — выбери кого разбираем, и скидывай материал.",
            reply_markup=main_menu()
        )

# ─── ЗАПУСК ───────────────────────────────────────────────────────────────────

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
