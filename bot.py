import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

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

@dp.message(Command("start"))
async def start(message: Message):
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
async def start_analysis(callback: CallbackQuery):
    await callback.message.answer(
        "Кого будем разбирать?",
        reply_markup=who_menu()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("who_"))
async def choose_who(callback: CallbackQuery):
    who_map = {
        "who_boyfriend": "парня/мужа",
        "who_friend": "подругу",
        "who_colleague": "коллегу",
        "who_other": "этого человека"
    }
    who = who_map.get(callback.data, "этого человека")
    await callback.message.answer(
        f"Что тебя беспокоит в поведении {who}?",
        reply_markup=concern_menu()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("concern_"))
async def choose_concern(callback: CallbackQuery):
    await callback.message.answer(
        "Поняла. Теперь скидывай материал:\n\n"
        "Текст переписки — просто скопируй и вставь\n"
        "Скрин — отправь фото\n"
        "Голосовое — перешли сообщение\n\n"
        "Жду."
    )
    await callback.answer()

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())