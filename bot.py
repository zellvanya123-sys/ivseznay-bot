import asyncio
import os
import tempfile
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import anthropic

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

class AnalysisState(StatesGroup):
    waiting_for_who = State()
    waiting_for_concern = State()
    waiting_for_material = State()

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

def after_analysis_menu():
    builder = InlineKeyboardBuilder()
    builder.button(text="Разобрать другого человека", callback_data="start_analysis")
    builder.button(text="Задать вопрос по ситуации", callback_data="ask_question")
    builder.adjust(1)
    return builder.as_markup()

@dp.message(Command("start"))
async def start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет. Я всё знаю.\n\n"
        "Без розовых очков. Без 'ну может он просто занят'.\n"
        "Холодный взгляд на факты — скажу тебе правду.\n\n"
        "Скинь переписку, скрин или голосовое — разберём что происходит.",
        reply_markup=main_menu()
    )

@dp.callback_query(lambda c: c.data == "how_it_works")
async def how_it_works(callback: CallbackQuery):
    await callback.message.answer(
        "Ты скидываешь переписку, скрин или голосовое.\n"
        "Я анализирую — нахожу признаки лжи, манипуляций и скрытых паттернов.\n"
        "Выдаю вердикт с конкретными цитатами и объяснением.\n\n"
        "Переписки не хранятся. Всё конфиденциально."
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "start_analysis")
async def start_analysis(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AnalysisState.waiting_for_who)
    await callback.message.answer("Кого будем разбирать?", reply_markup=who_menu())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("who_"))
async def choose_who(callback: CallbackQuery, state: FSMContext):
    who_map = {
        "who_boyfriend": "парень/муж",
        "who_friend": "подруга",
        "who_colleague": "коллега",
        "who_other": "другой человек"
    }
    who = who_map.get(callback.data, "другой человек")
    await state.update_data(who=who)
    await state.set_state(AnalysisState.waiting_for_concern)
    await callback.message.answer(f"Что беспокоит?", reply_markup=concern_menu())
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("concern_"))
async def choose_concern(callback: CallbackQuery, state: FSMContext):
    concern_map = {
        "concern_lie": "чувствую ложь",
        "concern_manipulation": "манипуляции",
        "concern_cold": "стал холоднее",
        "concern_other": "другое"
    }
    concern = concern_map.get(callback.data, "другое")
    await state.update_data(concern=concern)
    await state.set_state(AnalysisState.waiting_for_material)
    await callback.message.answer(
        "Жду материал.\n\n"
        "Скопируй переписку и вставь сюда — или отправь скрин."
    )
    await callback.answer()

async def analyze_with_claude(text: str, who: str, concern: str) -> str:
    prompt = f"""Ты — эксперт по анализу манипуляций и лжи в переписке. Говори уверенно, как умная подруга которая не жалеет а говорит правду.

Кого анализируем: {who}
Что беспокоит: {concern}
Материал для анализа: {text}

Выдай анализ строго в этом формате:

🔍 ВЕРДИКТ
(1-2 предложения, прямо и по делу)

⚠️ СИГНАЛЫ
(2-4 конкретных признака с цитатами из текста и объяснением)

📊 УРОВЕНЬ ТРЕВОГИ: Низкий / Средний / Высокий
(1 предложение почему)

💡 ЧТО ПРОВЕРИТЬ
(1-2 конкретных действия)

Говори уверенно. Используй цитаты. Без воды."""

    response = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

@dp.message(AnalysisState.waiting_for_material, F.text)
async def analyze_text(message: Message, state: FSMContext):
    data = await state.get_data()
    who = data.get("who", "этот человек")
    concern = data.get("concern", "подозрительное поведение")

    await message.answer("Анализирую...")

    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, analyze_with_claude, message.text, who, concern
        )
        await message.answer(result, reply_markup=after_analysis_menu())
    except Exception as e:
        await message.answer("Что-то пошло не так. Попробуй ещё раз.")

    await state.clear()

@dp.callback_query(lambda c: c.data == "ask_question")
async def ask_question(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Задавай вопрос — отвечу.")
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())