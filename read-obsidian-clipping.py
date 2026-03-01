#!/usr/bin/env -S PATH=${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH} uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["python-dotenv", "requests"]
# ///

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Read Obsidian Clipping
# @raycast.mode fullOutput
# @raycast.argument1 { "type": "text", "placeholder": "Search query" }

# Optional parameters:
# @raycast.icon 📖

# Documentation:
# @raycast.author Arman
# @raycast.authorURL https://github.com/armanjr/raycast-scripts

import logging
import os
import re
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).resolve().parent
CLIPPINGS_DIR = Path.home() / "Documents" / "Shared" / "Clippings"
LOG_DIR = SCRIPT_DIR / "logs"
LOG_FILE = LOG_DIR / "read-obsidian-clipping.log"

logger = logging.getLogger("clipping")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    )

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(logging.Formatter("%(message)s"))

    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)
    logger.addHandler(stdout_handler)


def truncate(text: str, max_len: int = 500) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + "..."


def load_config() -> dict:
    env_path = SCRIPT_DIR / ".env"
    load_dotenv(env_path)

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    elevenlabs_key = os.getenv("ELEVENLABS_API_KEY")

    if not openrouter_key:
        logger.error("OPENROUTER_API_KEY not set in .env")
        sys.exit(1)
    if not elevenlabs_key:
        logger.error("ELEVENLABS_API_KEY not set in .env")
        sys.exit(1)

    voice_id = os.getenv("ELEVENLABS_VOICE_ID", "cYoQX00i43EQyxfhxF3y")

    config = {
        "openrouter_key": openrouter_key,
        "elevenlabs_key": elevenlabs_key,
        "voice_id": voice_id,
    }
    logger.debug("Config loaded, voice_id=%s", voice_id)
    return config


def find_clipping(query: str) -> Path:
    words = query.lower().split()
    logger.debug("Searching clippings for words: %s", words)

    candidates = []
    for md_file in CLIPPINGS_DIR.glob("*.md"):
        name_lower = md_file.name.lower()
        if name_lower.startswith("[fa]"):
            continue
        if all(w in name_lower for w in words):
            candidates.append(md_file)

    if not candidates:
        logger.error("No clipping found matching: %s", query)
        print(f"No clipping found matching: {query}")
        sys.exit(1)

    # Pick newest by mtime
    best = max(candidates, key=lambda p: p.stat().st_mtime)
    logger.info("Found clipping: %s", best.name)
    return best


def check_cache(original_path: Path) -> tuple[Path | None, Path | None]:
    stem = original_path.stem
    parent = original_path.parent
    cached_md = parent / f"[FA] {stem}.md"
    cached_mp3 = parent / f"[FA] {stem}.mp3"

    md_result = cached_md if cached_md.exists() else None
    mp3_result = cached_mp3 if cached_mp3.exists() else None

    logger.debug(
        "Cache check: md=%s, mp3=%s",
        "hit" if md_result else "miss",
        "hit" if mp3_result else "miss",
    )
    return md_result, mp3_result


def read_and_clean(path: Path) -> tuple[str, str]:
    raw = path.read_text(encoding="utf-8")
    logger.debug("Read file: %s (%d chars)", path.name, len(raw))

    # 1. Extract title from YAML frontmatter
    title = ""
    body = raw
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
    if fm_match:
        frontmatter = fm_match.group(1)
        title_match = re.search(r'^title:\s*"?([^"\n]+)"?', frontmatter, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()
        body = raw[fm_match.end() :]
    logger.debug("Extracted title: %s", title)

    # 2. Cut CTA/footer — remove everything from first ## heading
    lines = body.split("\n")
    cut_idx = None
    for i, line in enumerate(lines):
        if line.startswith("## ") and i > 5:
            cut_idx = i
            break
    if cut_idx is not None:
        logger.debug(
            "Cutting CTA/footer at line %d: %s", cut_idx, truncate(lines[cut_idx], 80)
        )
        lines = lines[:cut_idx]

    # 3. Trim trailing image gallery — pop lines matching `- ![...](...)` from bottom
    while lines and re.match(r"^\s*-\s*!\[.*?\]\(.*?\)\s*$", lines[-1]):
        lines.pop()
    # Also pop any trailing blank lines after gallery removal
    while lines and not lines[-1].strip():
        lines.pop()

    # 4. Remove image lines and caption lines
    caption_keywords = [
        "Photograph:",
        "Composite:",
        "Illustration:",
        "Image:",
        "Photo:",
    ]
    image_pattern = re.compile(r"^!\[.*?\]\(.*?\)\s*$")
    lines = [
        line
        for line in lines
        if not image_pattern.match(line.strip())
        and not any(kw in line for kw in caption_keywords)
    ]

    text = "\n".join(lines)

    # 5. Convert markdown links [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)

    # 6. Convert wiki links [[Name]] → Name
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)

    # 7. Strip bold/italic markers
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)

    # 8. Remove bare URLs
    text = re.sub(r"https?://\S+", "", text)

    # 9. Remove emojis (broad Unicode ranges)
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map
        "\U0001f1e0-\U0001f1ff"  # flags
        "\U00002702-\U000027b0"
        "\U000024c2-\U0001f251"
        "\U0001f900-\U0001f9ff"  # supplemental symbols
        "\U0001fa00-\U0001fa6f"  # chess symbols
        "\U0001fa70-\U0001faff"  # symbols extended-A
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)

    # 10. Collapse blank lines (3+ newlines → 2) and strip
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    logger.info("Cleaned text: %d chars", len(text))
    logger.debug("Cleaned text preview: %s", truncate(text))
    return title, text


TTS_CHAR_LIMIT = 5000

SUMMARIZE_SYSTEM_PROMPT = """You are a skilled editor. Condense the following article into 3-4 paragraphs.

Rules:
- Output ONLY the condensed article. No titles, headings, labels, or extra formatting.
- Keep the SAME narrative voice and point of view. If the author writes in first person, the condensed version must also be in first person.
- Do NOT describe what the author says from the outside (e.g. "the author argues..."). Write AS the author.
- Preserve the original tone — if it's personal and conversational, the condensed version must feel the same.
- Keep the best anecdotes, quotes, or examples. Cut the rest.
- The result must be significantly shorter than the original."""


def summarize(text: str, config: dict) -> str:
    logger.info("Summarizing text (%d chars) via OpenRouter...", len(text))
    logger.debug("Summarize input: %s", truncate(text))

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['openrouter_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "google/gemini-2.5-pro-preview",
        "messages": [
            {"role": "system", "content": SUMMARIZE_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.3,
        "reasoning": {"effort": "medium"},
    }

    logger.debug("POST %s, model=%s, input_len=%d", url, payload["model"], len(text))
    resp = requests.post(url, headers=headers, json=payload, timeout=300)
    logger.debug(
        "Summarize response: status=%d, body_len=%d", resp.status_code, len(resp.text)
    )

    if resp.status_code != 200:
        logger.error("OpenRouter summarize error: %s", truncate(resp.text, 1000))
    resp.raise_for_status()

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("No choices in OpenRouter summarize response")

    summary = choices[0]["message"]["content"]
    if not summary.strip():
        raise ValueError("Empty summary returned from OpenRouter")

    logger.info("Summary complete: %d chars (was %d)", len(summary), len(text))
    logger.debug("Summary output: %s", truncate(summary))
    return summary


TRANSLATION_SYSTEM_PROMPT = """You are a professional translator. Translate the following English article into casual, spoken Persian (Farsi).

Rules:
- Output ONLY the translated article in Persian. No explanations, notes, or extra formatting.
- Use casual, informal, spoken Persian — like how you'd tell a friend about the article. Avoid formal or literary Persian."""


def translate(text: str, config: dict) -> str:
    logger.info("Translating text (%d chars) via OpenRouter...", len(text))
    logger.debug("Translation input: %s", truncate(text))

    url = "https://openrouter.ai/api/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {config['openrouter_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "google/gemini-2.5-pro-preview",
        "messages": [
            {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0.5,
        "reasoning": {"effort": "medium"},
    }

    logger.debug("POST %s, model=%s, input_len=%d", url, payload["model"], len(text))
    resp = requests.post(url, headers=headers, json=payload, timeout=300)
    logger.debug(
        "Translation response: status=%d, body_len=%d", resp.status_code, len(resp.text)
    )

    if resp.status_code != 200:
        logger.error("OpenRouter error: %s", truncate(resp.text, 1000))
    resp.raise_for_status()

    data = resp.json()
    choices = data.get("choices", [])
    if not choices:
        raise ValueError("No choices in OpenRouter response")

    translated = choices[0]["message"]["content"]
    if not translated.strip():
        raise ValueError("Empty translation returned from OpenRouter")

    logger.info("Translation complete: %d chars", len(translated))
    logger.debug("Translation output: %s", truncate(translated))
    return translated


def clean_for_tts(text: str) -> str:
    # Arabic → Persian char normalization
    replacements = {
        "\u0643": "\u06a9",  # Arabic kaf → Persian kaf
        "\u064a": "\u06cc",  # Arabic yeh → Persian yeh
    }
    for arabic, persian in replacements.items():
        text = text.replace(arabic, persian)

    # Remove bidi marks and tatweel
    text = text.replace("\u200f", "")  # RTL mark
    text = text.replace("\u200e", "")  # LTR mark
    text = text.replace("\u200b", "")  # zero-width space
    text = text.replace("\u200c", " ")  # zero-width non-joiner → space
    text = text.replace("\u0640", "")  # tatweel

    # Remove emojis
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"
        "\U0001f300-\U0001f5ff"
        "\U0001f680-\U0001f6ff"
        "\U0001f1e0-\U0001f1ff"
        "\U00002702-\U000027b0"
        "\U000024c2-\U0001f251"
        "\U0001f900-\U0001f9ff"
        "\U0001fa00-\U0001fa6f"
        "\U0001fa70-\U0001faff"
        "]+",
        flags=re.UNICODE,
    )
    text = emoji_pattern.sub("", text)

    # Remove URLs
    text = re.sub(r"https?://\S+", "", text)

    # Remove markdown artifacts
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"#{1,6}\s+", "", text)

    # Normalize punctuation
    text = text.replace("...", ".")
    text = re.sub(r"[–—]", "،", text)  # em/en dash → Persian comma

    # Normalize whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()

    logger.debug("TTS-cleaned text: %d chars", len(text))
    return text


def generate_audio(text: str, config: dict) -> bytes:
    logger.info("Generating audio (%d chars) via ElevenLabs...", len(text))

    voice_id = config["voice_id"]
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format=mp3_44100_192"
    headers = {
        "xi-api-key": config["elevenlabs_key"],
        "Content-Type": "application/json",
    }
    payload = {
        "text": text,
        "model_id": "eleven_v3",
        "language_code": "fa",
        "voice_settings": {
            "speed": 1.0,
        },
    }

    logger.debug("POST %s, voice=%s, text_len=%d", url, voice_id, len(text))
    resp = requests.post(url, headers=headers, json=payload, timeout=600)
    logger.debug(
        "ElevenLabs response: status=%d, content_len=%d",
        resp.status_code,
        len(resp.content),
    )

    if resp.status_code != 200:
        logger.error("ElevenLabs error: %s", truncate(resp.text, 1000))
    resp.raise_for_status()

    audio_data = resp.content
    if not audio_data:
        raise ValueError("Empty audio data from ElevenLabs")

    logger.info("Audio generated: %d bytes", len(audio_data))
    return audio_data


def save_outputs(
    original_path: Path, translated: str | None, audio: bytes | None
) -> tuple[Path | None, Path | None]:
    stem = original_path.stem
    parent = original_path.parent
    md_path = None
    mp3_path = None

    if translated is not None:
        md_path = parent / f"[FA] {stem}.md"
        md_path.write_text(translated, encoding="utf-8")
        logger.info(
            "Saved translation: %s (%d bytes)", md_path.name, md_path.stat().st_size
        )

    if audio is not None:
        mp3_path = parent / f"[FA] {stem}.mp3"
        mp3_path.write_bytes(audio)
        logger.info(
            "Saved audio: %s (%d bytes)", mp3_path.name, mp3_path.stat().st_size
        )

    return md_path, mp3_path


def play_audio(path: Path) -> None:
    logger.info("Playing audio: %s", path)
    subprocess.run(["open", "-a", "QuickTime Player", str(path)])


def main() -> None:
    setup_logging()
    logger.info("=" * 60)
    logger.info("Starting Read Obsidian Clipping")

    if len(sys.argv) < 2 or not sys.argv[1].strip():
        logger.error("No search query provided")
        print("Please provide a search query.")
        sys.exit(1)

    query = sys.argv[1].strip()
    logger.info("Query: %s", query)

    try:
        config = load_config()

        # Find the clipping
        filepath = find_clipping(query)
        print(f"Found: {filepath.name}")

        # Check cache
        cached_md, cached_mp3 = check_cache(filepath)

        if cached_md and cached_mp3:
            print("Using cached translation and audio")
            logger.info("Full cache hit — skipping all API calls")
            play_audio(cached_mp3)
            return

        if cached_md:
            print("Using cached translation, generating audio...")
            logger.info("Partial cache hit — reading cached translation")
            translated = cached_md.read_text(encoding="utf-8")
            tts_text = clean_for_tts(translated)
            audio_data = generate_audio(tts_text, config)
            _, mp3_path = save_outputs(filepath, None, audio_data)
            assert mp3_path is not None
            print(f"Audio saved: {mp3_path.name}")
            play_audio(mp3_path)
            return

        # Full pipeline
        print("Processing clipping...")

        # Clean
        title, cleaned = read_and_clean(filepath)
        if title:
            print(f"Title: {title}")
        logger.debug("Cleaned text length: %d", len(cleaned))

        # Summarize if too long for TTS
        if len(cleaned) > TTS_CHAR_LIMIT:
            print(f"Text too long ({len(cleaned)} chars), summarizing first...")
            cleaned = summarize(cleaned, config)

        # Translate
        print("Translating to Persian...")
        translated = translate(cleaned, config)

        # Save translation
        md_path, _ = save_outputs(filepath, translated, None)
        assert md_path is not None
        print(f"Translation saved: {md_path.name}")

        # TTS
        print("Generating audio...")
        tts_text = clean_for_tts(translated)
        audio_data = generate_audio(tts_text, config)

        # Save audio
        _, mp3_path = save_outputs(filepath, None, audio_data)
        assert mp3_path is not None
        print(f"Audio saved: {mp3_path.name}")

        # Play
        play_audio(mp3_path)
        print("Done!")

    except requests.exceptions.HTTPError as e:
        logger.exception("HTTP error during pipeline")
        print(f"API error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error during pipeline")
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
