import json
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SCENARIOS_PATH = Path(__file__).parent / "scenarios.json"

def load_scenarios():
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return data["scenarios"]

def build_keyboard(scenarios):
    rows = [[KeyboardButton(text=s["buttonTitle"])] for s in scenarios]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    scenarios = load_scenarios()
    kb = build_keyboard(scenarios)
    title_to_id = {s["buttonTitle"]: s["id"] for s in scenarios}

    @dp.message(CommandStart())
    async def start(message: Message):
        await message.answer(
            "Привет! Отправь голосовое или файл, затем выбери сценарий кнопкой.\n"
            "Пока это тестовый каркас: кнопки работают, STT/LLM добавим следующим шагом.",
            reply_markup=kb,
        )

    @dp.message(F.text.in_(list(title_to_id.keys())))
    async def scenario_selected(message: Message):
        scenario_id = title_to_id[message.text]
        await message.answer(f"Выбран сценарий: {message.text} (id={scenario_id}).")

    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
