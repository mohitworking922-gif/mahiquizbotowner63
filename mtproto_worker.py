import asyncio
import logging
import re
import time
from typing import List, Dict, Any, Callable, Optional

import config
from parser import clean_question_text

logger = logging.getLogger(__name__)

def is_mtproto_configured() -> bool:
    """Returns True if MTProto credentials are configured in environment variables."""
    return bool(config.TELEGRAM_API_ID and config.TELEGRAM_API_HASH and config.MTPROTO_SESSION_STRING)

def format_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Generates a visual progress bar e.g. [████░░░░░░] 40%"""
    if total <= 0:
        return "[░░░░░░░░░░] 0%"
    pct = min(100, max(0, int(current / total * 100)))
    filled = int(round(length * current / total))
    filled = min(length, max(0, filled))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}] {pct}%"

def format_eta(current: int, total: int, avg_seconds_per_item: float = 4.5) -> str:
    """Formats estimated remaining time e.g. 2m 15s"""
    remaining = max(0, total - current)
    total_seconds = int(round(remaining * avg_seconds_per_item))
    mins = total_seconds // 60
    secs = total_seconds % 60
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"

async def clone_quiz_from_token(
    token: str,
    progress_callback: Optional[Callable[[int, int, str], Any]] = None
) -> List[Dict[str, Any]]:
    """
    Clones a @QuizBot quiz sequentially using Pyrogram MTProto Client.
    Returns list of question dicts.
    """
    if not is_mtproto_configured():
        raise RuntimeError("MTProto configuration missing. Please set TELEGRAM_API_ID, TELEGRAM_API_HASH, and MTPROTO_SESSION_STRING in .env.")

    try:
        from pyrogram import Client
    except ImportError as e:
        raise RuntimeError("Pyrogram package not installed. Run: pip install pyrogram tgcrypto") from e

    questions: List[Dict[str, Any]] = []

    # Initialize Pyrogram client in-memory using string session
    app = Client(
        "quizbot_cloner",
        api_id=config.TELEGRAM_API_ID,
        api_hash=config.TELEGRAM_API_HASH,
        session_string=config.MTPROTO_SESSION_STRING,
        in_memory=True
    )

    total_q_detected = 35

    try:
        logger.info("[CLONE DEBUG 3/4] MTProto worker starting Pyrogram client...")
        await app.start()
        logger.info("[CLONE DEBUG 4/4] Pyrogram client started. Sending /start to @QuizBot...")

        quiz_bot = "QuizBot"
        
        # Send /start <token> to QuizBot
        await app.send_message(quiz_bot, f"/start {token}")
        await asyncio.sleep(1.5)

        # Get recent messages from QuizBot
        history = []
        async for m in app.get_chat_history(quiz_bot, limit=5):
            history.append(m)

        # Detect total questions from QuizBot announcement text if available
        for m in history:
            if m.text:
                q_match = re.search(r"(\d+)\s*questions", m.text, re.IGNORECASE)
                if q_match:
                    total_q_detected = int(q_match.group(1))
                    break

        # Click 'I'm ready' button or send callback if present
        for m in history:
            if m.reply_markup and m.reply_markup.inline_keyboard:
                for row in m.reply_markup.inline_keyboard:
                    for btn in row:
                        if "ready" in btn.text.lower():
                            try:
                                await m.click(btn.text)
                                await asyncio.sleep(1.0)
                            except Exception as ce:
                                logger.warning(f"Failed to click ready button: {ce}")

        # Collect polls sequentially
        collected = 0
        consecutive_timeouts = 0

        while collected < total_q_detected and consecutive_timeouts < 3:
            latest_poll_msg = None
            async for m in app.get_chat_history(quiz_bot, limit=3):
                if m.poll:
                    latest_poll_msg = m
                    break

            if latest_poll_msg and latest_poll_msg.poll:
                poll = latest_poll_msg.poll
                raw_q = poll.question or f"Question {collected + 1}"
                clean_q = clean_question_text(raw_q)
                
                raw_opts = [opt.text for opt in poll.options]
                correct_id = 0

                # Vote on option 0 to reveal correct answer
                try:
                    await latest_poll_msg.vote(0)
                    await asyncio.sleep(1.0)
                except Exception as ve:
                    logger.debug(f"Vote attempt on poll: {ve}")

                # Re-fetch poll after voting to get correct answer index if revealed
                try:
                    async for m_after in app.get_chat_history(quiz_bot, limit=2):
                        if m_after.poll and m_after.poll.correct_option_id is not None:
                            correct_id = m_after.poll.correct_option_id
                            break
                except Exception:
                    pass

                q_dict = {
                    "question_text": clean_q,
                    "options": raw_opts,
                    "correct_option_id": correct_id
                }
                questions.append(q_dict)
                collected += 1
                consecutive_timeouts = 0

                if progress_callback:
                    try:
                        await progress_callback(collected, total_q_detected, token)
                    except Exception as p_err:
                        logger.warning(f"Progress callback error: {p_err}")

                await asyncio.sleep(2.0)
            else:
                consecutive_timeouts += 1
                await asyncio.sleep(2.0)

    except Exception as err:
        logger.error(f"MTProto worker cloning error: {err}")
        if not questions:
            raise
    finally:
        try:
            await app.stop()
        except Exception:
            pass

    return questions
