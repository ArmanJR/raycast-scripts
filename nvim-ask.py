#!/usr/bin/env -S PATH=${HOME}/.local/bin:/opt/homebrew/bin:/usr/local/bin:${PATH} uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Ask Neovim
# @raycast.mode silent
# @raycast.argument1 { "type": "text", "placeholder": "Question about Neovim" }

# Optional parameters:
# @raycast.icon ⌨️
# @raycast.packageName Neovim
# @raycast.description Ask Claude Haiku a quick Neovim question with your current init.lua attached.

# Documentation:
# @raycast.author Arman

import json
import logging
import re
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
LOG_FILE = LOG_DIR / "nvim-ask.log"

NVIM_DIR = Path.home() / ".config" / "nvim"
GHOSTTY_CONFIG = Path.home() / ".config" / "ghostty" / "config"
CLAUDE_TIMEOUT_SECS = 120

SYSTEM_PROMPT = (
    "The user is asking a question about Neovim. The user's Neovim config "
    "(init.lua plus every .lua file under lua/) and terminal config (ghostty) "
    "are attached after their question, each prefixed with a `===== <path> =====` "
    "header. These files are the SOURCE OF TRUTH — they define this user's "
    "actual keybindings, leader key, plugins, options, and terminal shortcuts. "
    "Before answering, scan ALL attached files for any custom mapping, plugin "
    "keymap, which-key entry, or option that relates to what they asked — "
    "answers may live in lua/custom/plugins/*.lua or lua/kickstart/plugins/*.lua, "
    "not just init.lua. If a custom mapping exists, return THAT — not the Neovim "
    "default. Translate <leader> to its actual key (look for vim.g.mapleader / "
    "vim.g.maplocalleader; if leader is space, write it as <space>). Only fall "
    "back to Neovim defaults when no attached file customizes the relevant action.\n\n"
    "Output rules: plain text only — no markdown, no code fences, no backticks, "
    "no preamble, no labels, no quotes. BE EXTREMELY TERSE. Hard cap: 12 words. "
    "For keybinding questions, output ONLY the raw keys with no surrounding "
    "punctuation (examples of correct format: <C-w>   or   dw   or   <space>e   "
    "or   :wq). Nothing else — no description, no alternatives unless the user "
    "asked for them. For non-keybinding questions, answer in one short fragment."
)

logger = logging.getLogger("nvim_ask")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=512 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    )
    logger.setLevel(logging.DEBUG)
    logger.addHandler(file_handler)


RELEVANT_RE = re.compile(
    r"vim\.keymap\.set"
    r"|vim\.api\.nvim_(buf_)?set_keymap"
    r"|vim\.g\.(map)?(local)?leader"
    r"|<leader>|<localleader>"
    r"|\bkeys\s*="
    r"|\bmappings\s*="
    r"|\bmap\s*\("
    r"|which[-_]?key"
    r"|\bwk\.register"
    r"|\bdesc\s*="
    r"|vim\.opt\."
    r"|vim\.o\."
)
COMMENT_RE = re.compile(r"^\s*--")


def slim_init_lua(text: str) -> str:
    """Keep only lines likely to influence answers: leader, keymaps, plugin
    specs, options, and the immediately preceding comment (often a desc).
    For lines opening a brace, extend to the matching closing brace so that
    multi-line keymap tables stay intact."""
    lines = text.splitlines()
    n = len(lines)
    keep = [False] * n

    for i, line in enumerate(lines):
        if COMMENT_RE.match(line):
            # Commented-out keymaps confuse the model. Pure-comment lines are
            # only kept as context for a following kept line (handled below).
            continue
        if RELEVANT_RE.search(line):
            keep[i] = True

    for i in range(n):
        if not keep[i]:
            continue
        stripped = lines[i].rstrip()
        if not stripped.endswith("{") and not stripped.endswith("("):
            continue
        depth = stripped.count("{") + stripped.count("(") \
            - stripped.count("}") - stripped.count(")")
        j = i + 1
        guard = 0
        while j < n and depth > 0 and guard < 80:
            depth += lines[j].count("{") + lines[j].count("(")
            depth -= lines[j].count("}") + lines[j].count(")")
            keep[j] = True
            j += 1
            guard += 1

    for i in range(1, n):
        if keep[i] and not keep[i - 1] and COMMENT_RE.match(lines[i - 1]):
            keep[i - 1] = True

    out: list[str] = []
    last = -2
    for i in range(n):
        if keep[i]:
            if last >= 0 and i > last + 1:
                out.append("")
            out.append(lines[i])
            last = i
    return "\n".join(out)


def read_nvim_config() -> str:
    """Read init.lua + all .lua files under ~/.config/nvim/lua/, slim each,
    prepend a path header, and concatenate. Also append the ghostty config raw
    (small, not Lua) so terminal-bound shortcuts like Ctrl-` are answerable."""
    if not NVIM_DIR.exists():
        logger.warning("nvim config dir not found at %s", NVIM_DIR)
        return ""

    init = NVIM_DIR / "init.lua"
    files: list[Path] = []
    if init.exists():
        files.append(init)
    for path in sorted(NVIM_DIR.rglob("*.lua")):
        if path == init:
            continue
        files.append(path)

    chunks: list[str] = []
    total_raw = 0
    total_slim = 0
    for path in files:
        try:
            raw = path.read_text(encoding="utf-8")
        except Exception:
            logger.exception("failed to read %s", path)
            continue
        total_raw += len(raw)
        slim = slim_init_lua(raw)
        if not slim.strip():
            continue
        total_slim += len(slim)
        rel = path.relative_to(NVIM_DIR)
        chunks.append(f"-- ===== {rel} =====\n{slim}")

    if GHOSTTY_CONFIG.exists():
        try:
            ghostty_raw = GHOSTTY_CONFIG.read_text(encoding="utf-8")
            chunks.append(f"# ===== ghostty/config =====\n{ghostty_raw}")
            total_raw += len(ghostty_raw)
            total_slim += len(ghostty_raw)
        except Exception:
            logger.exception("failed to read ghostty config")

    combined = "\n\n".join(chunks)
    logger.info(
        "Loaded %d files: %d chars raw → %d chars after slimming (%.0f%%)",
        len(files),
        total_raw,
        len(combined),
        100.0 * len(combined) / max(total_raw, 1),
    )
    return combined


def build_prompt(question: str, config: str) -> str:
    if not config:
        return question
    return (
        f"{question}\n\n"
        f"--- BEGIN user's Neovim + terminal config ---\n"
        f"{config}\n"
        f"--- END user's Neovim + terminal config ---\n"
    )


def run_claude(prompt: str) -> str:
    cmd = [
        "claude",
        "-p",
        prompt,
        "--model",
        "haiku",
        "--system-prompt",
        SYSTEM_PROMPT,
        "--tools",
        "",
    ]
    logger.info(
        "Calling claude (model=haiku, prompt=%d chars, timeout=%ds)",
        len(prompt),
        CLAUDE_TIMEOUT_SECS,
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLAUDE_TIMEOUT_SECS,
            check=False,
        )
    except FileNotFoundError:
        logger.exception("`claude` binary not found on PATH")
        raise
    except subprocess.TimeoutExpired:
        logger.exception("claude timed out after %ds", CLAUDE_TIMEOUT_SECS)
        raise

    logger.debug(
        "claude finished: rc=%d, stdout=%d chars, stderr=%d chars",
        result.returncode,
        len(result.stdout),
        len(result.stderr),
    )

    if result.returncode != 0:
        logger.error(
            "claude exited with code %d: %s",
            result.returncode,
            result.stderr.strip()[:500],
        )
        raise RuntimeError(
            f"claude exited with code {result.returncode}: "
            f"{result.stderr.strip() or '<no stderr>'}"
        )

    answer = result.stdout.strip().strip("`").strip('"').strip("'").strip()
    if not answer:
        logger.error(
            "claude returned empty stdout. stderr=%s", result.stderr.strip()[:500]
        )
        raise RuntimeError("claude returned an empty response")

    logger.info("Got answer: %d chars", len(answer))
    return answer


def notify(answer: str) -> None:
    """Show a macOS notification banner with the answer."""
    # Angle brackets ("<C-w>") can be stripped by NSUserNotification's HTML-ish
    # content sanitization; swap them for full-width look-alikes for display.
    display = answer.replace("<", "＜").replace(">", "＞")

    notifier = shutil.which("terminal-notifier")
    if notifier:
        cmd = [
            notifier,
            "-title", display,
            "-message", " ",
            "-group", "nvim-ask",
            "-sender", "com.apple.Terminal",
        ]
        logger.info("Posting notification via terminal-notifier")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            logger.error(
                "terminal-notifier failed: rc=%d, stderr=%s",
                result.returncode,
                result.stderr.strip()[:300],
            )
        return

    logger.warning(
        "terminal-notifier not installed; falling back to osascript. "
        "Install with: brew install terminal-notifier"
    )
    try:
        script = f'display notification " " with title {json.dumps(display)}'
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        logger.exception("osascript notification failed")


def main() -> None:
    setup_logging()
    logger.info("=" * 60)
    logger.info("Starting Ask Neovim (argv=%s)", sys.argv[1:])

    if len(sys.argv) < 2 or not sys.argv[1].strip():
        logger.error("No question provided")
        print("Please provide a question.")
        sys.exit(1)

    question = sys.argv[1].strip()
    logger.info("Question: %s", question)

    try:
        config = read_nvim_config()
        prompt = build_prompt(question, config)
        answer = run_claude(prompt)
        notify(answer)
    except FileNotFoundError:
        print("Error: `claude` CLI not found. Is Claude Code installed and on PATH?")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"Error: claude timed out after {CLAUDE_TIMEOUT_SECS}s.")
        sys.exit(1)
    except Exception as e:
        logger.exception("Unexpected error")
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
