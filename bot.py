import asyncio
import os
from aiogram import Bot, Dispatcher
from aiogram.types import Message
from aiogram.filters import Command
from dotenv import load_dotenv

load_dotenv()

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer("Privet. Ya vsyo znayu. Skoro vsyo budet gotovo.")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())