import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from openai import AsyncOpenAI

WHISPER_COST_PER_MIN = 0.006
GPT_INPUT_COST_PER_1K = 0.000150
GPT_OUTPUT_COST_PER_1K = 0.000600


@dataclass
class PipelineResult:
    transcript: str
    result_text: str
    input_tokens: int
    output_tokens: int
    deliver: str
    filename: str | None
    caption: str | None


def load_scenarios(path: Path) -> tuple[list[dict], dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["scenarios"], data.get("defaults", {})


def estimate_cost(audio_duration_sec: float, input_tokens: int, output_tokens: int) -> float:
    whisper = (audio_duration_sec / 60) * WHISPER_COST_PER_MIN
    gpt_in = (input_tokens / 1000) * GPT_INPUT_COST_PER_1K
    gpt_out = (output_tokens / 1000) * GPT_OUTPUT_COST_PER_1K
    return round(whisper + gpt_in + gpt_out, 6)


def render_text_artifact(output_dir: Path, filename: str, result_text: str) -> Path:
    artifact_path = output_dir / filename
    artifact_path.write_text(result_text, encoding="utf-8")
    return artifact_path


async def transcribe(client: AsyncOpenAI, audio_path: Path, language: str = "ru") -> str:
    with open(audio_path, "rb") as audio_file:
        result = await client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
            language=language,
        )
    return result.text


async def apply_llm(client: AsyncOpenAI, transcript: str, scenario: dict) -> tuple[str, int, int]:
    prompt = scenario["prompt"]
    template = scenario.get("template")
    if template:
        system = (
            f"{prompt}\n\n"
            "Заполни следующий шаблон на основе транскрипта. "
            "Выведи ТОЛЬКО заполненный шаблон:\n\n"
            f"{template}"
        )
    else:
        system = prompt

    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": transcript},
        ],
    )
    usage = response.usage
    return response.choices[0].message.content, usage.prompt_tokens, usage.completion_tokens


async def generate_filename(client: AsyncOpenAI, text: str, file_name_prompt: str) -> str:
    today = date.today().strftime("%d-%m-%Y")
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": file_name_prompt},
            {"role": "user", "content": text[:1000]},
        ],
        max_tokens=30,
    )
    name = response.choices[0].message.content.strip().replace(" ", "-").replace("/", "-")
    return f"{today}_{name}.txt"


async def run_pipeline(
    client: AsyncOpenAI,
    audio_path: Path,
    scenario: dict,
    language: str,
) -> PipelineResult:
    transcript = await transcribe(client, audio_path, language=language)

    input_tokens = 0
    output_tokens = 0
    if "llm" in scenario.get("pipeline", []):
        result_text, input_tokens, output_tokens = await apply_llm(client, transcript, scenario)
    else:
        result_text = transcript

    filename = None
    caption = None
    if scenario.get("deliver") == "file_txt":
        file_name_prompt = scenario.get("fileNamePrompt")
        if file_name_prompt:
            filename = await generate_filename(client, result_text, file_name_prompt)
        else:
            filename = f"{date.today().strftime('%d-%m-%Y')}_расшифровка.txt"
        caption = scenario.get("fileCaption", "Готово. См. файл .txt")

    return PipelineResult(
        transcript=transcript,
        result_text=result_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        deliver=scenario.get("deliver", "chat"),
        filename=filename,
        caption=caption,
    )
