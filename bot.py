import asyncio
import json
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SCENARIOS_PATH = Path(__file__).parent / "scenarios.json"
TMP_DIR = Path(__file__).parent / "tmp"
TMP_DIR.mkdir(exist_ok=True)


def load_scenarios():
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return data["scenarios"], data.get("defaults", {})


def build_keyboard(scenarios):
    rows = [[KeyboardButton(text=s["buttonTitle"])] for s in scenarios]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


class UserState(StatesGroup):
    waiting_for_scenario = State()


async def transcribe(client: AsyncOpenAI, audio_path: Path, language: str = "ru") -> str:
    with open(audio_path, "rb") as f:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language=language,
        )
    return result.text


async def apply_llm(client: AsyncOpenAI, transcript: str, prompt: str) -> str:
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": transcript},
        ],
    )
    return response.choices[0].message.content


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set in .env")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    oai = AsyncOpenAI(api_key=OPENAI_API_KEY)

    scenarios, defaults = load_scenarios()
    kb = build_keyboard(scenarios)
    title_to_scenario = {s["buttonTitle"]: s for s in scenarios}
    language = defaults.get("language", "ru")

    @dp.message(CommandStart())
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "Привет! Отправь голосовое сообщение или аудиофайл — я его расшифрую.\n"
            "После этого выбери сценарий обработки.",
        )

    @dp.message(F.voice | F.audio)
    async def handle_audio(message: Message, state: FSMContext):
        file_id = message.voice.file_id if message.voice else message.audio.file_id
        await state.update_data(file_id=file_id)
        await state.set_state(UserState.waiting_for_scenario)
        await message.answer("Аудио получено. Выбери сценарий:", reply_markup=kb)

    @dp.message(UserState.waiting_for_scenario, F.text.in_(list(title_to_scenario.keys())))
    async def process_scenario(message: Message, state: FSMContext):
        data = await state.get_data()
        file_id = data.get("file_id")
        scenario = title_to_scenario[message.text]
        await state.clear()

        status_msg = await message.answer("⏳ Скачиваю и расшифровываю...")

        tg_file = await bot.get_file(file_id)
        suffix = Path(tg_file.file_path).suffix or ".ogg"
        tmp_path = TMP_DIR / f"{file_id}{suffix}"
        await bot.download_file(tg_file.file_path, destination=tmp_path)

        try:
            transcript = await transcribe(oai, tmp_path, language=language)

            if "llm" in scenario.get("pipeline", []):
                await status_msg.edit_text("⏳ Обрабатываю текст...")
                result_text = await apply_llm(oai, transcript, scenario["prompt"])
            else:
                result_text = transcript

            if scenario.get("deliver") == "file_txt":
                txt_path = TMP_DIR / f"{file_id}.txt"
                txt_path.write_text(result_text, encoding="utf-8")
                await status_msg.delete()
                await message.answer_document(FSInputFile(txt_path), caption="Дословная расшифровка")
                txt_path.unlink(missing_ok=True)
            else:
                await status_msg.edit_text(result_text)

        finally:
            tmp_path.unlink(missing_ok=True)

    @dp.message(F.text.in_(list(title_to_scenario.keys())))
    async def scenario_without_audio(message: Message):
        await message.answer("Сначала отправь голосовое сообщение или аудиофайл.")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
