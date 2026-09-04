import asyncio
import html
import logging
import math
import os
import re
import time
import httpx
from typing import Dict, Any

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Poll,
    InlineQueryResultArticle,
    InputTextMessageContent
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PollAnswerHandler,
    InlineQueryHandler,
    ContextTypes,
    filters
)
from telegram.error import RetryAfter, TimedOut, NetworkError

import config
import db
try:
    import mtproto_worker
except ImportError:
    mtproto_worker = None
from parser import parse_questions_message, clean_question_text
try:
    from leaderboard_image import generate_leaderboard_image
except ImportError:
    generate_leaderboard_image = None

import sys
import io

# Fix Windows console UTF-8 output, unbuffered line mode, and disable QuickEdit mode to prevent process freezes on click
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        hStdIn = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE = -10
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(hStdIn, ctypes.byref(mode)):
            # ENABLE_QUICK_EDIT_MODE = 0x0040, ENABLE_EXTENDED_FLAGS = 0x0080
            # On Windows, ENABLE_EXTENDED_FLAGS (0x0080) MUST be combined to disable QuickEdit mode successfully!
            new_mode = (mode.value & ~0x0040 & ~0x0020) | 0x0080
            kernel32.SetConsoleMode(hStdIn, new_mode)
    except Exception:
        pass

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', line_buffering=True)

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Suppress spammy HTTPX/httpcore poll logs that cause Windows console buffer scrolling freezes
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


async def global_update_logger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.poll_answer:
        logger.debug(f"[UPDATE] PollAnswer from user_id={update.poll_answer.user.id}: poll_id={update.poll_answer.poll_id}")
    elif update.message:
        logger.info(f"[UPDATE] Message from user_id={update.message.from_user.id} in chat_id={update.message.chat_id}: {update.message.text}")
    elif update.callback_query:
        logger.info(f"[UPDATE] CallbackQuery from user_id={update.callback_query.from_user.id}: data='{update.callback_query.data}'")
    else:
        logger.debug(f"[UPDATE] Received update_id={update.update_id}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    print(f"[ERROR] Exception while handling an update: {context.error}", flush=True)
    logger.exception(context.error)

# State management for private chat Quiz creation
# user_states[user_id] = { "step": "WAITING_NAME" | "WAITING_QUESTIONS" | "WAITING_TIMER", "name": str, "questions": list }
user_states: Dict[int, Dict[str, Any]] = {}

# Active quiz sessions
# active_quizzes[quiz_id] = { ... }
active_quizzes: Dict[str, Dict[str, Any]] = {}

# Mapping poll_id -> { quiz_id, q_idx, correct_option_id, poll_start_time }
poll_id_map: Dict[str, Dict[str, Any]] = {}


def is_owner(user_id: int) -> bool:
    if not config.OWNER_ID or config.OWNER_ID == 0:
        logger.error("OWNER_ID is not configured or set to 0. Authorization failed.")
        return False
    return user_id == config.OWNER_ID


async def is_admin_or_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return False

    if is_owner(user.id):
        return True

    if chat.type == "private":
        return True

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        if member.status in ["administrator", "creator"]:
            return True
    except Exception as e:
        logger.error(f"Error checking chat member status for user_id={user.id} in chat_id={chat.id}: {e}")

    return False


def is_authorized_group(chat_id: int) -> bool:
    return True


def format_time(seconds: float) -> str:
    total_sec = int(round(seconds))
    mins = total_sec // 60
    secs = total_sec % 60
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def truncate_text(text: str, max_len: int) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def format_quiz_to_txt(questions: list) -> str:
    blocks = []
    for q in questions:
        lines = [q["question_text"]]
        for idx, opt in enumerate(q["options"]):
            if idx == q["correct_option_id"]:
                lines.append(f"{opt} ✅")
            else:
                lines.append(opt)
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


import datetime
from zoneinfo import ZoneInfo

# Validate timezone availability at startup (Windows needs the 'tzdata' package).
try:
    IST_TZ = ZoneInfo("Asia/Kolkata")
except Exception as _tz_err:
    logger.critical(
        "FATAL: Timezone 'Asia/Kolkata' not found. "
        "Install the 'tzdata' package:  pip install tzdata"
    )
    raise SystemExit(
        "Missing timezone data. Run:  pip install tzdata"
    ) from _tz_err

# ==========================================
# PAUSE / RESUME / STOP HANDLERS
# ==========================================

async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ Only Group Admins or Bot Owner can use this command!")
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to pause.")
        return

    paused_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        session["paused"] = True
        paused_count += 1
        try:
            await context.bot.send_message(chat_id=target_group, text="⏸️ Quiz Paused!")
        except Exception as e:
            logger.error(f"Failed to send pause notice to group {target_group}: {e}")

    if paused_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to pause.")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ Only Group Admins or Bot Owner can use this command!")
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to resume.")
        return

    resumed_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        session["paused"] = False
        resumed_count += 1
        try:
            await context.bot.send_message(chat_id=target_group, text="▶️ Quiz Resumed!")
        except Exception as e:
            logger.error(f"Failed to send resume notice to group {target_group}: {e}")

    if resumed_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to resume.")


async def stop_command_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ Only Group Admins or Bot Owner can use this command!")
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to stop.")
        return

    stopped_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        session["stopped"] = True
        stopped_count += 1
        try:
            await context.bot.send_message(chat_id=target_group, text="⏹️ Quiz Stopped!")
        except Exception as e:
            logger.error(f"Failed to send stop notice to group {target_group}: {e}")

    if stopped_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to stop.")


async def fast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ Only Group Admins or Bot Owner can use this command!")
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to adjust speed.")
        return

    delta = 5
    if context.args and len(context.args) > 0:
        try:
            delta = int(context.args[0])
        except ValueError:
            delta = 5

    adjusted_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        curr_timer = session.get("timer", 15)
        new_timer = max(5, curr_timer - delta)
        session["timer"] = new_timer
        adjusted_count += 1
        try:
            await context.bot.send_message(
                chat_id=target_group,
                text=f"⚡ Quiz Speed Increased!\n⏱️ Per question timer is now {new_timer} seconds."
            )
        except Exception as e:
            logger.error(f"Failed to send fast notice to group {target_group}: {e}")

    if adjusted_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to adjust speed.")


async def slow_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ Only Group Admins or Bot Owner can use this command!")
        return

    if not active_quizzes:
        await update.message.reply_text("❌ No active quiz running to adjust speed.")
        return

    delta = 5
    if context.args and len(context.args) > 0:
        try:
            delta = int(context.args[0])
        except ValueError:
            delta = 5

    adjusted_count = 0
    for q_id, session in list(active_quizzes.items()):
        target_group = session.get("group_id", config.GROUP_ID)
        if chat.type != "private" and chat.id != target_group:
            continue
        curr_timer = session.get("timer", 15)
        new_timer = min(600, curr_timer + delta)
        session["timer"] = new_timer
        adjusted_count += 1
        try:
            await context.bot.send_message(
                chat_id=target_group,
                text=f"🐢 Quiz Speed Decreased!\n⏱️ Per question timer is now {new_timer} seconds."
            )
        except Exception as e:
            logger.error(f"Failed to send slow notice to group {target_group}: {e}")

    if adjusted_count == 0 and chat.type != "private":
        await update.message.reply_text("❌ No active quiz running in this chat to adjust speed.")


# ==========================================
# SCHEDULING HANDLERS
# ==========================================

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return

    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ Only Group Admins or Bot Owner can use this command!")
        return

    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("❌ Format: /schedule <quiz_id> <HH:MM> (e.g. /schedule GGN9PV0Q9 09:00)")
        return

    quiz_id = args[0].strip()
    time_str = args[1].strip()

    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(f"❌ Quiz ID {quiz_id} not found in database.")
        return

    try:
        parts = time_str.split(":")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ValueError
    except Exception:
        await update.message.reply_text("❌ Invalid time format. Use HH:MM in 24-hour format (e.g., 09:00 or 21:30).")
        return

    target_group = chat.id if chat.type != "private" else config.GROUP_ID

    now = datetime.datetime.now(IST_TZ)
    scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if scheduled_dt <= now:
        scheduled_dt += datetime.timedelta(days=1)

    epoch_timestamp = scheduled_dt.timestamp()
    db.save_schedule(quiz_id, epoch_timestamp, time_str, group_id=target_group)

    time_am_pm = scheduled_dt.strftime("%I:%M %p")
    day_month = scheduled_dt.strftime("%d %b")

    delta = scheduled_dt - now
    total_seconds = int(delta.total_seconds())
    hours_left = total_seconds // 3600
    minutes_left = (total_seconds % 3600) // 60

    quiz_name = quiz_data["name"]
    announcement = (
        f"✅ Scheduled!\n\n"
        f"📝 '{quiz_name}'\n"
        f"🕒 {time_am_pm}, {day_month}\n"
        f"⏱️ In {hours_left}h {minutes_left}m"
    )

    try:
        if target_group != 0:
            await context.bot.send_message(chat_id=target_group, text=announcement)
            await update.message.reply_text(f"✅ Quiz scheduled successfully and announcement posted in group!")
        else:
            await update.message.reply_text(f"✅ Quiz scheduled in DB for {time_am_pm}!")
    except Exception as e:
        await update.message.reply_text(f"✅ Quiz scheduled in DB, but failed to post announcement: {e}")


async def schedules_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    schedules = db.get_active_schedules()
    if not schedules:
        await update.message.reply_text("ℹ️ No active scheduled quizzes.")
        return

    lines = ["📅 Active Schedules:"]
    for s in schedules:
        quiz_id = s["quiz_id"]
        time_str = s["time_str"]
        ts = s["scheduled_timestamp"]
        dt = datetime.datetime.fromtimestamp(ts, tz=ZoneInfo("Asia/Kolkata"))
        time_am_pm = dt.strftime("%I:%M %p")
        day_month = dt.strftime("%d %b")
        lines.append(f"- ID: `{quiz_id}` | Time: `{time_str}` ({time_am_pm}, {day_month})")

    await update.message.reply_text("\n".join(lines))


async def unschedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if not await is_admin_or_owner(update, context):
        await update.message.reply_text("❌ Only Group Admins or Bot Owner can use this command!")
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format: /unschedule <quiz_id>")
        return

    quiz_id = args[0].strip()
    db.delete_schedule(quiz_id)
    await update.message.reply_text(f"✅ Schedule for Quiz ID `{quiz_id}` removed successfully.")


# ==========================================
# SCHEDULER BACKGROUND LOOP
# ==========================================

async def scheduler_loop(application):
    print("⏰ Starting background scheduler loop...", flush=True)
    try:
        while True:
            try:
                now_ts = time.time()
                schedules = await asyncio.to_thread(db.get_active_schedules)
                for s in schedules:
                    quiz_id = s["quiz_id"]
                    scheduled_ts = s["scheduled_timestamp"]
                    if now_ts >= scheduled_ts:
                        print(f"⏰ Triggering scheduled quiz: {quiz_id}", flush=True)
                        await asyncio.to_thread(db.delete_schedule, quiz_id)
                        quiz_data = await asyncio.to_thread(db.get_quiz, quiz_id)
                        target_group = s.get("group_id") or config.GROUP_ID
                        if quiz_data and target_group != 0:
                            asyncio.create_task(run_quiz_session(application.bot, target_group, quiz_data))
            except Exception as e:
                print(f"[ERROR] Exception in scheduler_loop: {e}", flush=True)
            await asyncio.sleep(10)
    except asyncio.CancelledError:
        print("⏰ Scheduler loop stopped cleanly.", flush=True)


# ==========================================
# COMMAND HANDLERS
# ==========================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user:
        return

    # Check for deep link arguments (e.g., /start quiz_GGN1NZBG4)
    args = context.args
    if args and len(args) > 0 and args[0].startswith("quiz_"):
        quiz_id = args[0].replace("quiz_", "").strip()
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await update.message.reply_text("❌ Quiz not found!")
            return

        if chat.type != "private":
            logger.info(f"Triggering Quiz {quiz_id} setup wizard in group chat_id={chat.id}")
            await send_launch_wizard_step1(context.bot, chat.id, quiz_data, update.message.message_id, initiator_user_id=user.id)
            return

        await send_quiz_created_screen(update, context, quiz_data)
        return

    if chat.type != "private":
        await update.message.reply_text(
            "👋 **Welcome to Quiz Bot!**\n\n"
            "• Group me kisi quiz ko chalane ke liye command format try karein: `/start quiz_ID`\n"
            "• Help ke liye: `/help`",
            parse_mode="Markdown"
        )
        return

    # Restrict Quiz creation in private chat to Bot Owner only (Silent return for non-owner)
    if not is_owner(user.id):
        return

    # Start new quiz creation flow for owner in private chat
    user_states[user.id] = {
        "step": "WAITING_NAME",
        "name": "",
        "questions": []
    }
    await update.message.reply_text("✅ Quiz name भेजें")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    if user.id not in user_states:
        await update.message.reply_text("ℹ️ No active quiz creation or editing session to cancel.")
        return

    state = user_states[user.id]
    step = state.get("step", "")

    del user_states[user.id]

    if step.startswith("EDIT_"):
        await update.message.reply_text("🚫 Quiz editing cancelled! Send /start to create or /edit to edit a Quiz.")
    else:
        await update.message.reply_text("🚫 Current Quiz creation cancelled! Send /start to begin a new Quiz.")


async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    state = user_states.get(user.id)
    if not state or state.get("step") not in ["WAITING_QUESTIONS", "WAITING_TIMER"]:
        await update.message.reply_text("❌ No active quiz creation. Send /start to create a quiz.")
        return

    questions = state.get("questions", [])
    if len(questions) < 1:
        await update.message.reply_text("❌ At least 1 question is required. Send questions or /cancel.")
        return

    state["step"] = "WAITING_TIMER"
    await update.message.reply_text("⏳ Timer in seconds (>10)")


async def handle_private_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text("Send /start to create a new Quiz or /edit to edit one.")
        return

    step = state.get("step")
    if step not in ["WAITING_QUESTIONS", "EDIT_ADD_QUESTIONS"]:
        await update.message.reply_text("❌ Document is not expected at this step.")
        return

    doc = update.message.document
    if not doc or not doc.file_name or not doc.file_name.lower().endswith('.txt'):
        await update.message.reply_text("❌ Please send a valid .txt file.")
        return

    try:
        # Download file
        telegram_file = await context.bot.get_file(doc.file_id)
        file_bytes = await telegram_file.download_as_bytearray()
        
        try:
            text_content = file_bytes.decode('utf-8-sig')
        except UnicodeDecodeError:
            try:
                text_content = file_bytes.decode('utf-8', errors='ignore')
            except Exception:
                text_content = file_bytes.decode('latin-1', errors='ignore')

        parsed = parse_questions_message(text_content)
        if not parsed:
            await update.message.reply_text("❌ No valid questions found in the file or incorrect format.")
            return

        if step == "WAITING_QUESTIONS":
            questions = state.get("questions", [])
            questions.extend(parsed)
            state["questions"] = questions
            await update.message.reply_text(
                f"✅ {len(parsed)} processed! Total: {len(questions)}\nSend more or /done"
            )
        elif step == "EDIT_ADD_QUESTIONS":
            quiz_id = state.get("quiz_id")
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data:
                questions = quiz_data.get("questions", [])
                questions.extend(parsed)
                db.update_quiz_questions(quiz_id, questions)
                await update.message.reply_text(
                    f"✅ Added {len(parsed)} questions! Total: {len(questions)}. Send more or type /done_edit"
                )
            else:
                await update.message.reply_text("❌ Quiz not found.")
    except Exception as e:
        logger.exception(e)
        await update.message.reply_text(f"❌ Failed to process document: {e}")


async def handle_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        await update.message.reply_text("Send /start to create a new Quiz or /edit to edit one.")
        return

    step = state.get("step")
    if step not in ["WAITING_QUESTIONS", "EDIT_ADD_QUESTIONS"]:
        await update.message.reply_text("❌ Photo is not expected at this step.")
        return

    photo_file_id = update.message.photo[-1].file_id
    caption = update.message.caption.strip() if update.message.caption else ""

    if caption:
        parsed = parse_questions_message(caption)
        if not parsed:
            await update.message.reply_text(
                "❌ Photo received, but caption format is invalid!\n\nFormat:\nQuestion text\nOption 1\nOption 2 ✅\nOption 3\nOption 4"
            )
            return

        for q in parsed:
            q["photo_file_id"] = photo_file_id

        if step == "WAITING_QUESTIONS":
            questions = state.get("questions", [])
            questions.extend(parsed)
            state["questions"] = questions
            await update.message.reply_text(
                f"🖼️ Question with Photo saved! Total: {len(questions)}\nSend more or /done"
            )
        elif step == "EDIT_ADD_QUESTIONS":
            quiz_id = state.get("quiz_id")
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data:
                questions = quiz_data.get("questions", [])
                questions.extend(parsed)
                db.update_quiz_questions(quiz_id, questions)
                await update.message.reply_text(
                    f"🖼️ Added Question with Photo! Total: {len(questions)}. Send more or type /done_edit"
                )
            else:
                await update.message.reply_text("❌ Quiz not found.")
    else:
        state["pending_photo_id"] = photo_file_id
        await update.message.reply_text(
            "🖼️ Photo received! Now send the Question text & Options for this photo."
        )


async def handle_private_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        if is_owner(user.id):
            await update.message.reply_text("Send /start to create a new Quiz or /edit to edit one.")
        return

    step = state.get("step")
    if step not in ["WAITING_QUESTIONS", "EDIT_ADD_QUESTIONS"]:
        await update.message.reply_text("❌ Poll is not expected at this step.")
        return

    poll = update.message.poll
    if not poll:
        return

    raw_question = poll.question.strip() if poll.question else ""
    question_text = clean_question_text(raw_question)
    raw_options = poll.options or []
    options = [opt.text.strip() for opt in raw_options if opt.text]
    correct_id = poll.correct_option_id

    if not question_text or len(options) < 2:
        await update.message.reply_text("❌ Poll must have a valid question text and at least 2 options.")
        return

    if correct_id is None or correct_id < 0 or correct_id >= len(options):
        correct_id = 0

    q_dict = {
        "question_text": question_text,
        "options": options,
        "correct_option_id": correct_id
    }
    if poll.explanation:
        q_dict["explanation"] = poll.explanation.strip()[:200]

    pending_photo_id = state.pop("pending_photo_id", None)
    if pending_photo_id:
        q_dict["photo_file_id"] = pending_photo_id

    if step == "WAITING_QUESTIONS":
        questions = state.get("questions", [])
        questions.append(q_dict)
        state["questions"] = questions
        await update.message.reply_text(
            f"✅ {len(questions)} saved! Send more or /done"
        )
    elif step == "EDIT_ADD_QUESTIONS":
        quiz_id = state.get("quiz_id")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            questions = quiz_data.get("questions", [])
            questions.append(q_dict)
            db.update_quiz_questions(quiz_id, questions)
            await update.message.reply_text(
                f"✅ Added 1 question! Total: {len(questions)}. Send more or type /done_edit"
            )
        else:
            await update.message.reply_text("❌ Quiz not found.")


# ==========================================
# MESSAGE HANDLER FOR QUIZ CREATION
# ==========================================

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    
    if not user or chat.type != "private":
        return

    state = user_states.get(user.id)
    if not state:
        if is_owner(user.id):
            await update.message.reply_text("Send /start to create a new Quiz.")
        return

    text = update.message.text.strip() if update.message.text else ""
    step = state.get("step")

    if step == "WAITING_NAME":
        if not text:
            await update.message.reply_text("Please send a valid Quiz name.")
            return

        state["name"] = text
        state["step"] = "WAITING_QUESTIONS"
        await update.message.reply_text(
            f"✅ Name: {text}\nQuestions भेजें, polls, testbook link, .txt या /cancel"
        )
        return

    elif step == "WAITING_QUESTIONS":
        if not text or text.startswith("/"):
            return


        parsed = parse_questions_message(text)
        if not parsed:
            await update.message.reply_text(
                "❌ Invalid question format!\n\nFormat:\nQuestion text\nOption 1\nOption 2 ✅\nOption 3\nOption 4"
            )
            return

        pending_photo_id = state.pop("pending_photo_id", None)
        if pending_photo_id:
            for q in parsed:
                q["photo_file_id"] = pending_photo_id

        questions = state.get("questions", [])
        questions.extend(parsed)
        state["questions"] = questions

        await update.message.reply_text(f"✅ {len(questions)} saved! Send more or /done")
        return

    elif step == "WAITING_TIMER":
        try:
            timer_val = int(text)
            if timer_val <= 10:
                await update.message.reply_text("⏳ Timer in seconds (>10)")
                return
        except ValueError:
            await update.message.reply_text("⏳ Timer in seconds (>10)")
            return

        state["timer"] = timer_val
        state["step"] = "WAITING_SEC_CHOICE"

        q_count = len(state.get("questions", []))
        keyboard = [
            [
                InlineKeyboardButton("🟢 Yes", callback_data="create_sec_yes"),
                InlineKeyboardButton("⚪ No", callback_data="create_sec_no")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"Questions completed\n\n"
            f"📚 Section Quiz?",
            reply_markup=reply_markup
        )
        return

    elif step == "CREATE_SEC_COUNT":
        try:
            sec_count = int(text)
            if sec_count <= 0:
                await update.message.reply_text("❌ Total sections must be at least 1.")
                return
        except ValueError:
            await update.message.reply_text("❌ Valid number send karein (e.g. 2).")
            return

        total_q = len(state.get("questions", []))
        if sec_count > total_q:
            await update.message.reply_text(f"❌ Sections count ({sec_count}) total questions ({total_q}) se zyada nahi ho sakta.")
            return

        state["sec_total_count"] = sec_count
        state["sec_current_idx"] = 0
        state["temp_sections"] = []
        state["step"] = "CREATE_SEC_NAME"
        await update.message.reply_text("📌 Section 1 Name:\nExample: 🏛️ History")
        return

    elif step == "CREATE_SEC_NAME":
        if not text:
            await update.message.reply_text("Please send a valid Section Name.")
            return

        state["curr_sec_name"] = text
        state["step"] = "CREATE_SEC_RANGE"
        idx = state.get("sec_current_idx", 0) + 1
        total_q = len(state.get("questions", []))
        await update.message.reply_text(
            f"🔢 Section {idx} Range:\nExample: 1-50"
        )
        return

    elif step == "CREATE_SEC_RANGE":
        import re
        total_q = len(state.get("questions", []))
        range_str = text.replace("to", "-").replace("TO", "-")
        nums = re.findall(r'\d+', range_str)
        if len(nums) != 2:
            await update.message.reply_text(f"❌ Range format invalid! Standard format send karein (e.g. 1-50):")
            return

        start_q, end_q = int(nums[0]), int(nums[1])
        curr_name = state.get("curr_sec_name", "Section")
        curr_idx = state.get("sec_current_idx", 0) + 1

        if start_q < 1 or end_q > total_q or start_q > end_q:
            await update.message.reply_text(
                f"❌ Invalid section range!\n"
                f"Start Q1 se kam nahi ho sakta aur End Q{total_q} se zyada nahi ho sakta.\n\n"
                f"Section {curr_idx} Range dubara send karein (e.g. 1-50):"
            )
            return

        # Range Overlap Validation
        temp_sections = state.get("temp_sections", [])
        overlap_found = False
        ov_sec_name = ""
        ov_range = ""
        for s in temp_sections:
            s_start = s["start"]
            s_end = s["end"]
            if max(s_start, start_q) <= min(s_end, end_q):
                overlap_found = True
                ov_sec_name = s["name"]
                ov_range = f"{s_start}-{s_end}"
                break

        if overlap_found:
            await update.message.reply_text(
                f"❌ Section ranges overlap!\n"
                f"{ov_sec_name}: {ov_range}\n"
                f"{curr_name}: {start_q}-{end_q}\n\n"
                f"Section {curr_idx} Range dubara send karein:"
            )
            return

        # Add section
        temp_sections.append({"name": curr_name, "start": start_q, "end": end_q})
        state["temp_sections"] = temp_sections
        state["sec_current_idx"] += 1

        if state["sec_current_idx"] < state["sec_total_count"]:
            state["step"] = "CREATE_SEC_NAME"
            next_idx = state["sec_current_idx"] + 1
            await update.message.reply_text(f"📌 Section {next_idx} Name:")
            return

        # All sections configured! Show Summary Confirmation Screen
        temp_sections.sort(key=lambda x: x["start"])
        sec_summary_lines = ["📚 SECTION SETUP\n"]
        for s in temp_sections:
            sec_summary_lines.append(f"{s['name']}")

        sec_summary_lines.append("\n━━━━━━━━━━━━━━━━━━")
        sec_summary_lines.append(f"❓ Total Questions: {total_q}")
        sec_summary_lines.append(f"📚 Total Sections: {len(temp_sections)}")
        sec_summary_lines.append("━━━━━━━━━━━━━━━━━━\n")

        msg_text = "\n".join(sec_summary_lines)
        keyboard = [
            [
                InlineKeyboardButton("💾 Save Quiz", callback_data="create_sec_confirm"),
                InlineKeyboardButton("❌ Cancel", callback_data="create_sec_cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(msg_text, reply_markup=reply_markup)
        state["step"] = "CREATE_SEC_CONFIRM"
        return

    elif step == "EDIT_NAME":
        quiz_id = state.get("quiz_id")
        editor_msg_id = state.get("editor_msg_id")
        prompt_msg_id = state.get("prompt_msg_id")
        if not text:
            await update.message.reply_text("Please send a valid Quiz name.")
            return
        db.update_quiz_name(quiz_id, text)
        del user_states[user.id]

        try:
            await update.message.delete()
        except Exception:
            pass
        if prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=prompt_msg_id)
            except Exception:
                pass

        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            if editor_msg_id:
                try:
                    quiz_id = quiz_data["quiz_id"]
                    name = quiz_data["name"]
                    q_count = len(quiz_data["questions"])
                    timer = quiz_data["timer"]
                    negative = float(quiz_data.get("negative", 0.0))
                    sec_enabled = quiz_data.get("sections_enabled", 0)
                    sections = quiz_data.get("sections", [])
                    sec_status_str = f"🟢 Enabled ({len(sections)} Sections)" if sec_enabled == 1 else "⚪ Disabled"
                    toggle_sec_text = "📚 Sections: 🟢 Enabled" if sec_enabled == 1 else "📚 Sections: ⚪ Disabled"

                    safe_name = html.escape(str(name))
                    safe_quiz_id = html.escape(str(quiz_id))

                    msg_text = (
                        f"🎯 <b>QUIZ EDITOR PANEL</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 <b>Quiz ID:</b> <code>{safe_quiz_id}</code>\n"
                        f"📌 <b>Name:</b> {safe_name}\n"
                        f"🔢 <b>Questions:</b> {q_count}\n"
                        f"⌚ <b>Timer:</b> {timer}s\n"
                        f"📚 <b>Sections:</b> {sec_status_str}\n"
                        f"➖ <b>Negative Marking:</b> {negative:.2f}\n"
                        f"💰 <b>Access Type:</b> Free\n"
                        f"📢 <b>Promo Banner:</b> ❌ None\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton("✏️ Edit Name", callback_data=f"ed_name_{quiz_id}"),
                            InlineKeyboardButton("⏱️ Edit Timer", callback_data=f"ed_timer_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("➕ Add Questions", callback_data=f"ed_addq_{quiz_id}"),
                            InlineKeyboardButton("🔀 Shuffle", callback_data=f"ed_shuf_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("➖ Negative Mark", callback_data=f"ed_neg_{quiz_id}"),
                            InlineKeyboardButton(toggle_sec_text, callback_data=f"sec_tog_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("👁️ View Questions", callback_data=f"ed_view_{quiz_id}"),
                            InlineKeyboardButton("📚 Manage Sections", callback_data=f"sec_mgr_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("📤 Export File", callback_data=f"ed_exp_{quiz_id}"),
                            InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"ed_del_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("❌ Close Editor", callback_data="ed_close")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.edit_message_text(
                        chat_id=chat.id,
                        message_id=editor_msg_id,
                        text=msg_text,
                        reply_markup=reply_markup,
                        parse_mode="HTML"
                    )
                    return
                except Exception as e:
                    logger.warning(f"Failed to edit editor card: {e}")

            await send_quiz_editor_screen(update, context, quiz_data)
        return

    elif step == "EDIT_TIMER":
        quiz_id = state.get("quiz_id")
        editor_msg_id = state.get("editor_msg_id")
        prompt_msg_id = state.get("prompt_msg_id")
        try:
            timer_val = int(text)
            if timer_val <= 10:
                await update.message.reply_text("⏳ Timer in seconds (>10)")
                return
        except ValueError:
            await update.message.reply_text("⏳ Timer in seconds (>10)")
            return
        db.update_quiz_timer(quiz_id, timer_val)
        del user_states[user.id]

        try:
            await update.message.delete()
        except Exception:
            pass
        if prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=prompt_msg_id)
            except Exception:
                pass

        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            if editor_msg_id:
                try:
                    quiz_id = quiz_data["quiz_id"]
                    name = quiz_data["name"]
                    q_count = len(quiz_data["questions"])
                    timer = quiz_data["timer"]
                    negative = float(quiz_data.get("negative", 0.0))
                    sec_enabled = quiz_data.get("sections_enabled", 0)
                    sections = quiz_data.get("sections", [])
                    sec_status_str = f"🟢 Enabled ({len(sections)} Sections)" if sec_enabled == 1 else "⚪ Disabled"
                    toggle_sec_text = "📚 Sections: 🟢 Enabled" if sec_enabled == 1 else "📚 Sections: ⚪ Disabled"

                    safe_name = html.escape(str(name))
                    safe_quiz_id = html.escape(str(quiz_id))

                    msg_text = (
                        f"🎯 <b>QUIZ EDITOR PANEL</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 <b>Quiz ID:</b> <code>{safe_quiz_id}</code>\n"
                        f"📌 <b>Name:</b> {safe_name}\n"
                        f"🔢 <b>Questions:</b> {q_count}\n"
                        f"⌚ <b>Timer:</b> {timer}s\n"
                        f"📚 <b>Sections:</b> {sec_status_str}\n"
                        f"➖ <b>Negative Marking:</b> {negative:.2f}\n"
                        f"💰 <b>Access Type:</b> Free\n"
                        f"📢 <b>Promo Banner:</b> ❌ None\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton("✏️ Edit Name", callback_data=f"ed_name_{quiz_id}"),
                            InlineKeyboardButton("⏱️ Edit Timer", callback_data=f"ed_timer_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("➕ Add Questions", callback_data=f"ed_addq_{quiz_id}"),
                            InlineKeyboardButton("🔀 Shuffle", callback_data=f"ed_shuf_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("➖ Negative Mark", callback_data=f"ed_neg_{quiz_id}"),
                            InlineKeyboardButton(toggle_sec_text, callback_data=f"sec_tog_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("👁️ View Questions", callback_data=f"ed_view_{quiz_id}"),
                            InlineKeyboardButton("📚 Manage Sections", callback_data=f"sec_mgr_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("📤 Export File", callback_data=f"ed_exp_{quiz_id}"),
                            InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"ed_del_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("❌ Close Editor", callback_data="ed_close")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.edit_message_text(
                        chat_id=chat.id,
                        message_id=editor_msg_id,
                        text=msg_text,
                        reply_markup=reply_markup,
                        parse_mode="HTML"
                    )
                    return
                except Exception as e:
                    logger.warning(f"Failed to edit editor card: {e}")

            await send_quiz_editor_screen(update, context, quiz_data)
        return

    elif step == "EDIT_NEGATIVE":
        quiz_id = state.get("quiz_id")
        editor_msg_id = state.get("editor_msg_id")
        prompt_msg_id = state.get("prompt_msg_id")
        try:
            neg_val = float(text)
            if neg_val < 0.0 or neg_val > 5.0:
                await update.message.reply_text("➖ Negative marking 0.0 aur 5.0 ke beech hona chahiye.")
                return
        except ValueError:
            await update.message.reply_text("➖ Valid number send karein (e.g. 0, 0.25, 0.50, 1.0):")
            return
        db.update_quiz_negative(quiz_id, neg_val)
        del user_states[user.id]

        try:
            await update.message.delete()
        except Exception:
            pass
        if prompt_msg_id:
            try:
                await context.bot.delete_message(chat_id=chat.id, message_id=prompt_msg_id)
            except Exception:
                pass

        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            if editor_msg_id:
                try:
                    quiz_id = quiz_data["quiz_id"]
                    name = quiz_data["name"]
                    q_count = len(quiz_data["questions"])
                    timer = quiz_data["timer"]
                    negative = float(quiz_data.get("negative", 0.0))
                    sec_enabled = quiz_data.get("sections_enabled", 0)
                    sections = quiz_data.get("sections", [])
                    sec_status_str = f"🟢 Enabled ({len(sections)} Sections)" if sec_enabled == 1 else "⚪ Disabled"
                    toggle_sec_text = "📚 Sections: 🟢 Enabled" if sec_enabled == 1 else "📚 Sections: ⚪ Disabled"

                    safe_name = html.escape(str(name))
                    safe_quiz_id = html.escape(str(quiz_id))

                    msg_text = (
                        f"🎯 <b>QUIZ EDITOR PANEL</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"🆔 <b>Quiz ID:</b> <code>{safe_quiz_id}</code>\n"
                        f"📌 <b>Name:</b> {safe_name}\n"
                        f"🔢 <b>Questions:</b> {q_count}\n"
                        f"⌚ <b>Timer:</b> {timer}s\n"
                        f"📚 <b>Sections:</b> {sec_status_str}\n"
                        f"➖ <b>Negative Marking:</b> {negative:.2f}\n"
                        f"💰 <b>Access Type:</b> Free\n"
                        f"📢 <b>Promo Banner:</b> ❌ None\n"
                        f"━━━━━━━━━━━━━━━━━━━━"
                    )

                    keyboard = [
                        [
                            InlineKeyboardButton("✏️ Edit Name", callback_data=f"ed_name_{quiz_id}"),
                            InlineKeyboardButton("⏱️ Edit Timer", callback_data=f"ed_timer_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("➕ Add Questions", callback_data=f"ed_addq_{quiz_id}"),
                            InlineKeyboardButton("🔀 Shuffle", callback_data=f"ed_shuf_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("➖ Negative Mark", callback_data=f"ed_neg_{quiz_id}"),
                            InlineKeyboardButton(toggle_sec_text, callback_data=f"sec_tog_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("👁️ View Questions", callback_data=f"ed_view_{quiz_id}"),
                            InlineKeyboardButton("📚 Manage Sections", callback_data=f"sec_mgr_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("📤 Export File", callback_data=f"ed_exp_{quiz_id}"),
                            InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"ed_del_{quiz_id}")
                        ],
                        [
                            InlineKeyboardButton("❌ Close Editor", callback_data="ed_close")
                        ]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await context.bot.edit_message_text(
                        chat_id=chat.id,
                        message_id=editor_msg_id,
                        text=msg_text,
                        reply_markup=reply_markup,
                        parse_mode="HTML"
                    )
                    return
                except Exception as e:
                    logger.warning(f"Failed to edit editor card: {e}")

            await send_quiz_editor_screen(update, context, quiz_data)
        return

    elif step == "EDIT_ADD_QUESTIONS":
        quiz_id = state.get("quiz_id")
        if not text:
            return
        parsed = parse_questions_message(text)
        if not parsed:
            await update.message.reply_text("❌ Invalid question format!")
            return

        pending_photo_id = state.pop("pending_photo_id", None)
        if pending_photo_id:
            for q in parsed:
                q["photo_file_id"] = pending_photo_id

        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            questions = quiz_data.get("questions", [])
            questions.extend(parsed)
            db.update_quiz_questions(quiz_id, questions)
            await update.message.reply_text(
                f"✅ Added {len(parsed)} questions! Total: {len(questions)}. Send more or type /done_edit"
            )
        return

    elif step == "ADD_SEC_NAME":
        quiz_id = state.get("quiz_id")
        if not text:
            await update.message.reply_text("Please send a valid Section Name.")
            return
        state["sec_name"] = text
        state["step"] = "ADD_SEC_START"
        quiz_data = db.get_quiz(quiz_id)
        total_q = len(quiz_data["questions"]) if quiz_data else 100
        await update.message.reply_text(f"🔢 Start Question Number send karein (1 to {total_q}):")
        return

    elif step == "ADD_SEC_START":
        quiz_id = state.get("quiz_id")
        sec_name = state.get("sec_name")
        quiz_data = db.get_quiz(quiz_id)
        total_q = len(quiz_data["questions"]) if quiz_data else 100
        try:
            start_q = int(text)
            if start_q < 1 or start_q > total_q:
                await update.message.reply_text(f"❌ Start Question Number 1 aur {total_q} ke beech hona chahiye.")
                return
        except ValueError:
            await update.message.reply_text(f"❌ Valid number send karein (1 to {total_q}).")
            return
        state["start_q"] = start_q
        state["step"] = "ADD_SEC_END"
        await update.message.reply_text(f"🔢 End Question Number send karein ({start_q} to {total_q}):")
        return

    elif step == "ADD_SEC_END":
        quiz_id = state.get("quiz_id")
        sec_name = state.get("sec_name")
        start_q = state.get("start_q")
        quiz_data = db.get_quiz(quiz_id)
        total_q = len(quiz_data["questions"]) if quiz_data else 100
        try:
            end_q = int(text)
            if end_q < start_q or end_q > total_q:
                await update.message.reply_text(f"❌ End Question Number {start_q} aur {total_q} ke beech hona chahiye.")
                return
        except ValueError:
            await update.message.reply_text(f"❌ Valid number send karein ({start_q} to {total_q}).")
            return

        del user_states[user.id]

        # Range Overlap Validation
        existing_sections = quiz_data.get("sections", [])
        overlap_found = False
        overlapping_sec_name = ""
        overlap_range = ""
        for s in existing_sections:
            s_start = s["start"]
            s_end = s["end"]
            if max(s_start, start_q) <= min(s_end, end_q):
                overlap_found = True
                overlapping_sec_name = s["name"]
                overlap_range = f"{s_start}–{s_end}"
                break

        if overlap_found:
            await update.message.reply_text(
                f"❌ Section ranges overlap!\n\n"
                f"Existing: {overlapping_sec_name} ({overlap_range})\n"
                f"Attempted: {sec_name} ({start_q}–{end_q})\n\n"
                f"Please re-add section with a non-overlapping range."
            )
            await send_section_manager_screen(update, context, quiz_data)
            return

        # Valid! Add section and sort by start question
        new_sec = {"name": sec_name, "start": start_q, "end": end_q}
        existing_sections.append(new_sec)
        existing_sections.sort(key=lambda x: x["start"])
        db.update_quiz_sections(quiz_id, existing_sections)
        db.update_quiz_sections_enabled(quiz_id, 1)

        await update.message.reply_text(f"✅ Section '{sec_name}' (Q{start_q}–Q{end_q}) added successfully!")
        quiz_data["sections"] = existing_sections
        quiz_data["sections_enabled"] = 1
        await send_section_manager_screen(update, context, quiz_data)
        return


async def inline_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query_text = update.inline_query.query.strip()
    results = []

    bot_obj = context.bot
    try:
        bot_username = bot_obj.username if getattr(bot_obj, "username", None) else (await bot_obj.get_me()).username
    except Exception:
        bot_username = ""

    quizzes_to_show = []
    if query_text:
        # If query matches exact quiz_id
        quiz_data = db.get_quiz(query_text)
        if quiz_data:
            quizzes_to_show.append(quiz_data)
        else:
            # Search quizzes by name or ID
            all_quizzes = db.get_quizzes_by_user(0, limit=20)
            for q in all_quizzes:
                if query_text.lower() in q.get("name", "").lower() or query_text.lower() in q.get("quiz_id", "").lower():
                    quizzes_to_show.append(q)
    else:
        # Show user's quizzes or latest public quizzes
        user = update.effective_user
        user_id = user.id if user else 0
        quizzes_to_show = db.get_quizzes_by_user(user_id, limit=20)

    for q in quizzes_to_show:
        q_id = q.get("quiz_id")
        q_name = q.get("name", "Quiz")
        q_count = len(q.get("questions", []))
        q_timer = q.get("timer", 15)

        # Use HTML parse mode with proper escaping to avoid Markdown entity errors
        safe_q_name = html.escape(str(q_name))
        safe_q_id = html.escape(str(q_id))
        safe_bot_username = html.escape(str(bot_username)) if bot_username else ""

        msg_content = (
            f"via @{safe_bot_username}\n"
            f"📖 <b>Quiz Name:</b> {safe_q_name}\n"
            f"#️⃣ <b>Questions:</b> {q_count}\n"
            f"⏰ <b>Timer:</b> {q_timer}s\n"
            f"🆔 <b>Quiz ID:</b> <code>{safe_q_id}</code>\n"
            f"✖️ <b>-ve:</b> 0\n"
            f"💰 <b>Type:</b> free"
        )

        start_url = f"https://t.me/{bot_username}?start=quiz_{q_id}" if bot_username else f"https://t.me/?start=quiz_{q_id}"
        group_url = f"https://t.me/{bot_username}?startgroup=quiz_{q_id}" if bot_username else f"https://t.me/?startgroup=quiz_{q_id}"

        keyboard = [
            [
                InlineKeyboardButton("🎯 Start", url=start_url)
            ],
            [
                InlineKeyboardButton("🚀 Group", url=group_url)
            ],
            [
                InlineKeyboardButton("🔗 Share", switch_inline_query=q_id)
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        results.append(
            InlineQueryResultArticle(
                id=q_id,
                title=f"Quiz: {q_name}",
                description=f"{q_count} questions | Timer: {q_timer}s",
                input_message_content=InputTextMessageContent(msg_content, parse_mode="HTML"),
                reply_markup=reply_markup
            )
        )

    await update.inline_query.answer(results, cache_time=5)


async def send_quiz_created_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict):
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    q_count = len(quiz_data["questions"])
    timer = quiz_data["timer"]
    creator = quiz_data.get("creator_name", "MAHI 💗")

    bot_obj = context.bot
    try:
        bot_username = bot_obj.username if getattr(bot_obj, "username", None) else (await bot_obj.get_me()).username
    except Exception:
        bot_username = ""

    start_url = f"https://t.me/{bot_username}?start=quiz_{quiz_id}" if bot_username else f"https://t.me/?start=quiz_{quiz_id}"
    group_url = f"https://t.me/{bot_username}?startgroup=quiz_{quiz_id}" if bot_username else f"https://t.me/?startgroup=quiz_{quiz_id}"

    safe_name = html.escape(str(name))
    safe_creator = html.escape(str(creator))
    safe_quiz_id = html.escape(str(quiz_id))

    msg_text = (
        f"Quiz Created! 💬\n\n"
        f"💳 Name: {safe_name}\n"
        f"#️⃣ Questions: {q_count}\n"
        f"⏰ Timer: {timer}s\n"
        f"🆔 ID: <code>{safe_quiz_id}</code>\n"
        f"💰 Type: free\n"
        f"☠️ -ve: 0.00\n"
        f"👧 Creator: {safe_creator}"
    )

    keyboard = [
        [
            InlineKeyboardButton("🎯 Start", url=start_url)
        ],
        [
            InlineKeyboardButton("🚀 Group", url=group_url)
        ],
        [
            InlineKeyboardButton("🔗 Share", switch_inline_query=quiz_id)
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    target_msg = update.callback_query.message if update.callback_query else update.message
    try:
        await target_msg.reply_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Failed to send quiz created screen with HTML parse_mode: {e}, falling back to plain text")
        plain_msg = (
            f"Quiz Created! 💬\n\n"
            f"💳 Name: {name}\n"
            f"#️⃣ Questions: {q_count}\n"
            f"⏰ Timer: {timer}s\n"
            f"🆔 ID: {quiz_id}\n"
            f"💰 Type: free\n"
            f"☠️ -ve: 0.00\n"
            f"👧 Creator: {creator}"
        )
        await target_msg.reply_text(plain_msg, reply_markup=reply_markup)


async def send_quiz_editor_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict, edit_existing: bool = True):
    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]
    q_count = len(quiz_data["questions"])
    timer = quiz_data["timer"]
    negative = float(quiz_data.get("negative", 0.0))
    sec_enabled = quiz_data.get("sections_enabled", 0)
    sections = quiz_data.get("sections", [])
    sec_status_str = f"🟢 Enabled ({len(sections)} Sections)" if sec_enabled == 1 else "⚪ Disabled"
    toggle_sec_text = "📚 Sections: 🟢 Enabled" if sec_enabled == 1 else "📚 Sections: ⚪ Disabled"

    safe_name = html.escape(str(name))
    safe_quiz_id = html.escape(str(quiz_id))

    msg_text = (
        f"🎯 <b>QUIZ EDITOR PANEL</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 <b>Quiz ID:</b> <code>{safe_quiz_id}</code>\n"
        f"📌 <b>Name:</b> {safe_name}\n"
        f"🔢 <b>Questions:</b> {q_count}\n"
        f"⌚ <b>Timer:</b> {timer}s\n"
        f"📚 <b>Sections:</b> {sec_status_str}\n"
        f"➖ <b>Negative Marking:</b> {negative:.2f}\n"
        f"💰 <b>Access Type:</b> Free\n"
        f"📢 <b>Promo Banner:</b> ❌ None\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )

    keyboard = [
        [
            InlineKeyboardButton("✏️ Edit Name", callback_data=f"ed_name_{quiz_id}"),
            InlineKeyboardButton("⏱️ Edit Timer", callback_data=f"ed_timer_{quiz_id}")
        ],
        [
            InlineKeyboardButton("➕ Add Questions", callback_data=f"ed_addq_{quiz_id}"),
            InlineKeyboardButton("🔀 Shuffle", callback_data=f"ed_shuf_{quiz_id}")
        ],
        [
            InlineKeyboardButton("➖ Negative Mark", callback_data=f"ed_neg_{quiz_id}"),
            InlineKeyboardButton(toggle_sec_text, callback_data=f"sec_tog_{quiz_id}")
        ],
        [
            InlineKeyboardButton("👁️ View Questions", callback_data=f"ed_view_{quiz_id}"),
            InlineKeyboardButton("📚 Manage Sections", callback_data=f"sec_mgr_{quiz_id}")
        ],
        [
            InlineKeyboardButton("📤 Export File", callback_data=f"ed_exp_{quiz_id}"),
            InlineKeyboardButton("🗑️ Delete Quiz", callback_data=f"ed_del_{quiz_id}")
        ],
        [
            InlineKeyboardButton("❌ Close Editor", callback_data="ed_close")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query and edit_existing:
        try:
            await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
            return
        except Exception:
            pass

    if update.message:
        await update.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
    elif update.callback_query:
        await update.callback_query.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")


async def send_section_manager_screen(update: Update, context: ContextTypes.DEFAULT_TYPE, quiz_data: dict):
    quiz_id = quiz_data["quiz_id"]
    q_count = len(quiz_data["questions"])
    sections = quiz_data.get("sections", [])

    lines = [
        f"📚 QUIZ SECTIONS (Total Questions: {q_count})\n"
    ]
    if not sections:
        lines.append("ℹ️ No sections configured yet.\nClick '➕ Add Section' to add a section.")
    else:
        for idx, s in enumerate(sections, start=1):
            lines.append(f"{s['name']} → Q{s['start']}–Q{s['end']}")

    msg_text = "\n".join(lines)

    keyboard = [
        [
            InlineKeyboardButton("➕ Add Section", callback_data=f"sec_add_{quiz_id}"),
            InlineKeyboardButton("🗑️ Clear Sections", callback_data=f"sec_clr_{quiz_id}")
        ],
        [
            InlineKeyboardButton("🔙 Back to Editor", callback_data=f"sec_back_{quiz_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(msg_text, reply_markup=reply_markup)
        except Exception:
            await update.callback_query.message.reply_text(msg_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(msg_text, reply_markup=reply_markup)


async def myquizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return
    quizzes = db.get_quizzes_by_user(user.id)
    if not quizzes:
        await update.message.reply_text("ℹ️ Aapne abhi tak koi Quiz nahi banaya hai. Naya quiz banane ke liye /start bhejein!")
        return

    lines = ["📚 **Aapke Banaye Hue Quizzes:**\n"]
    for q in quizzes:
        q_id = q.get("quiz_id")
        name = q.get("name", "Quiz")
        count = len(q.get("questions", []))
        lines.append(f"• **{name}** (ID: `{q_id}`) - {count} Questions\n  Edit: `/edit {q_id}` | Start: `/start quiz_{q_id}`")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🤖 **Quiz Bot Help & Commands Guide**\n\n"
        "✨ **Quiz Banao (Private Chat):**\n"
        "• `/start` - Naya Quiz create karne ke liye.\n"
        "• `.txt file` send karein - Direct questions import karne ke liye.\n"
        "• `/done` - Quiz creation finish karne ke liye.\n\n"
        "🎯 **Quiz Group Me Play Karo:**\n"
        "• `/start quiz_ID` - Group me directly quiz start karein.\n"
        "• Button **'🎯 Select Group for Quiz'** se Group choose karke launch karein.\n\n"
        "⚙️ **Group Quiz Control Commands:**\n"
        "• `/pause` - Running quiz pause karein.\n"
        "• `/resume` - Paused quiz resume karein.\n"
        "• `/stop` - Running quiz stop karein.\n"
        "• `/fast` - Question timer speed badhayein.\n"
        "• `/slow` - Question timer speed kam karein.\n\n"
        "📋 **Manage Quizzes:**\n"
        "• `/myquizzes` - Aapke banaye saare quizzes dekhein.\n"
        "• `/edit <quiz_id>` - Quiz edit karein.\n"
        "• `/schedule <quiz_id> HH:MM` - Quiz schedule karein."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")


async def edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_owner(user.id):
        return

    args = context.args
    if not args:
        await update.message.reply_text("❌ Format: /edit <quiz_id> (e.g. /edit GGNRUOE6F)")
        return

    quiz_id = args[0].strip()
    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(f"❌ Quiz ID {quiz_id} not found in database.")
        return

    await send_quiz_editor_screen(update, context, quiz_data)


async def done_edit_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    state = user_states.get(user.id)
    if not state or state.get("step") != "EDIT_ADD_QUESTIONS":
        await update.message.reply_text("❌ No active question editing session.")
        return

    quiz_id = state.get("quiz_id")
    del user_states[user.id]

    quiz_data = db.get_quiz(quiz_id)
    if quiz_data:
        await update.message.reply_text("✅ Questions update finished!")
        await send_quiz_editor_screen(update, context, quiz_data)
    else:
        await update.message.reply_text("✅ Questions editing finished!")


# ==========================================
# CALLBACK QUERY HANDLER
# ==========================================

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # Defer query.answer() for qw_ callbacks so initiator lock can send show_alert
    if not query.data.startswith("qw_"):
        try:
            await query.answer()
        except Exception as e:
            logger.warning(f"[API TIMEOUT/ERROR] query.answer() non-fatal exception: {e}")

    user = query.from_user
    logger.info(f"Callback received: {query.data} from user_id={user.id}")

    data = query.data

    if data.startswith("qw_"):
        wizard_key = f"{query.message.chat_id}_{query.message.message_id}"
        session = group_wizard_sessions.get(wizard_key)

        # Initiator Lock: only the user who launched the quiz (or owner) can configure
        if session:
            initiator_id = session.get("initiator_id")
            if initiator_id is not None and user.id != initiator_id and not is_owner(user.id):
                try:
                    await query.answer("❌ Only the quiz initiator can configure this.", show_alert=True)
                except Exception:
                    pass
                return

        # Authorized user — answer the callback
        try:
            await query.answer()
        except Exception:
            pass

        if data.startswith("qw_quick_"):
            quiz_id = data.replace("qw_quick_", "").strip()
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data:
                last_cfg = get_last_settings(user.id, query.message.chat_id, quiz_data)
                timer = last_cfg.get("timer", quiz_data.get("timer", 20))
                mark = last_cfg.get("correct_mark", 1.0)
                try:
                    await query.message.delete()
                except Exception:
                    pass
                group_wizard_sessions.pop(wizard_key, None)
                asyncio.create_task(run_quiz_session(context.bot, query.message.chat_id, quiz_data, query.message, custom_timer=timer, custom_correct_mark=mark))
            return

        if data.startswith("qw_mk_"):
            rest = data.replace("qw_mk_", "")
            subparts = rest.split("_")
            val_str = subparts[0]
            quiz_id = subparts[1] if len(subparts) > 1 else ""
            quiz_data = db.get_quiz(quiz_id)
            if not quiz_data:
                try:
                    await query.answer("❌ Quiz not found.")
                except Exception:
                    pass
                return

            mark = 1.0
            if val_str != "skip":
                try:
                    mark = float(val_str)
                except ValueError:
                    mark = 1.0

            if not session:
                session = {"quiz_id": quiz_id, "correct_mark": mark, "timer": quiz_data.get("timer", 20)}
                group_wizard_sessions[wizard_key] = session
            else:
                session["correct_mark"] = mark

            default_timer = quiz_data.get("timer", 20)
            step2_text = (
                "⏱️ **Timer per question?**\n\n"
                "Select a time — Start button will appear after."
            )
            step2_keyboard = [
                [
                    InlineKeyboardButton("10s", callback_data=f"qw_tm_10_{quiz_id}"),
                    InlineKeyboardButton("15s", callback_data=f"qw_tm_15_{quiz_id}"),
                    InlineKeyboardButton("20s", callback_data=f"qw_tm_20_{quiz_id}")
                ],
                [
                    InlineKeyboardButton("25s", callback_data=f"qw_tm_25_{quiz_id}"),
                    InlineKeyboardButton("30s", callback_data=f"qw_tm_30_{quiz_id}"),
                    InlineKeyboardButton("40s", callback_data=f"qw_tm_40_{quiz_id}")
                ],
                [
                    InlineKeyboardButton(f"⏭️ Quiz default ({default_timer}s)", callback_data=f"qw_tm_def_{quiz_id}")
                ]
            ]
            try:
                await query.message.edit_text(step2_text, reply_markup=InlineKeyboardMarkup(step2_keyboard), parse_mode="Markdown")
            except Exception:
                pass
            return

        if data.startswith("qw_tm_"):
            rest = data.replace("qw_tm_", "")
            subparts = rest.split("_")
            val_str = subparts[0]
            quiz_id = subparts[1] if len(subparts) > 1 else ""
            quiz_data = db.get_quiz(quiz_id)
            if not quiz_data:
                try:
                    await query.answer("❌ Quiz not found.")
                except Exception:
                    pass
                return

            default_timer = quiz_data.get("timer", 20)
            timer_val = default_timer
            if val_str != "def":
                try:
                    timer_val = int(val_str)
                except ValueError:
                    timer_val = default_timer

            if not session:
                session = {"quiz_id": quiz_id, "correct_mark": 1.0, "timer": timer_val}
                group_wizard_sessions[wizard_key] = session
            else:
                session["timer"] = timer_val

            step3_text = (
                f"⏱️ **Timer set: {timer_val}s**\n\n"
                "All set! Press Start when ready."
            )
            step3_keyboard = [
                [
                    InlineKeyboardButton("▶️ Start Quiz", callback_data=f"qw_start_{quiz_id}")
                ]
            ]
            try:
                await query.message.edit_text(step3_text, reply_markup=InlineKeyboardMarkup(step3_keyboard), parse_mode="Markdown")
            except Exception:
                pass
            return

        if data.startswith("qw_start_"):
            quiz_id = data.replace("qw_start_", "").strip()
            quiz_data = db.get_quiz(quiz_id)
            if quiz_data:
                timer = session.get("timer", quiz_data.get("timer", 20)) if session else quiz_data.get("timer", 20)
                mark = session.get("correct_mark", 1.0) if session else 1.0
                save_last_settings(user.id, query.message.chat_id, timer, mark)
                try:
                    await query.message.delete()
                except Exception:
                    pass
                group_wizard_sessions.pop(wizard_key, None)
                asyncio.create_task(run_quiz_session(context.bot, query.message.chat_id, quiz_data, query.message, custom_timer=timer, custom_correct_mark=mark))
            return

    if data == "create_sec_no":
        state = user_states.get(user.id)
        if not state:
            return
        name = state.get("name", "Quiz")
        questions = state.get("questions", [])
        timer_val = state.get("timer", 15)
        creator_name = user.first_name + (f" {user.last_name}" if user.last_name else "")

        quiz_id = db.save_quiz(name, timer_val, questions, creator_name=creator_name, sections_enabled=0, sections=[], creator_id=user.id)
        del user_states[user.id]

        quiz_data = db.get_quiz(quiz_id)
        await send_quiz_created_screen(update, context, quiz_data)
        return

    if data == "create_sec_yes":
        state = user_states.get(user.id)
        if not state:
            return
        state["step"] = "CREATE_SEC_COUNT"
        await query.message.reply_text("📚 How many sections?")
        return

    if data == "create_sec_confirm":
        state = user_states.get(user.id)
        if not state:
            return
        name = state.get("name", "Quiz")
        questions = state.get("questions", [])
        timer_val = state.get("timer", 15)
        temp_sections = state.get("temp_sections", [])
        creator_name = user.first_name + (f" {user.last_name}" if user.last_name else "")

        quiz_id = db.save_quiz(name, timer_val, questions, creator_name=creator_name, sections_enabled=1, sections=temp_sections, creator_id=user.id)
        del user_states[user.id]

        quiz_data = db.get_quiz(quiz_id)
        await send_quiz_created_screen(update, context, quiz_data)
        return

    if data == "create_sec_cancel":
        if user.id in user_states:
            del user_states[user.id]
        await query.message.reply_text("🚫 Quiz creation cancelled!")
        return

    if data.startswith("share_"):
        quiz_id = data.replace("share_", "")
        bot_username = (await context.bot.get_me()).username
        share_link = f"https://t.me/{bot_username}?start=quiz_{quiz_id}"
        await query.message.reply_text(
            f"🔗 Share link for Quiz ({quiz_id}):\n{share_link}\n\n"
            f"Note: Share this link in any group to launch this quiz!"
        )
        return

    if data.startswith("start_g_"):
        quiz_id = data.replace("start_g_", "").strip()
        quiz_data = db.get_quiz(quiz_id)

        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        user_states[user.id] = {
            "step": "WAITING_NATIVE_GROUP",
            "launch_quiz_id": quiz_id
        }

        request_chat_button = KeyboardButton(
            text="🎯 Select Group for Quiz",
            request_chat=KeyboardButtonRequestChat(
                request_id=1,
                chat_is_channel=False,
                bot_is_member=True
            )
        )
        reply_markup = ReplyKeyboardMarkup(
            [[request_chat_button]],
            resize_keyboard=True,
            one_time_keyboard=True
        )

        await query.message.reply_text(
            f"👇 Neeche **'🎯 Select Group for Quiz'** button par click karke Telegram se target Group choose karein jahan Quiz `{quiz_id}` launch karni hai:",
            reply_markup=reply_markup
        )
        return

    if data.startswith("start_p_"):
        quiz_id = data.replace("start_p_", "").strip()
        quiz_data = db.get_quiz(quiz_id)

        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        if config.GROUP_ID == 0:
            await query.message.reply_text("❌ GROUP_ID configuration missing in environment variable.")
            return

        target_group = config.GROUP_ID
        logger.info(f"Triggering Quiz {quiz_id} setup wizard in default GROUP_ID={target_group}")

        await send_launch_wizard_step1(context.bot, target_group, quiz_data, initiator_user_id=user.id)
        await query.message.reply_text(f"🚀 Quiz '{quiz_data['name']}' setup wizard sent to default group ({target_group})!")
        return

    if data.startswith("ed_exp_"):
        quiz_id = data.replace("ed_exp_", "")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        questions = quiz_data.get("questions", [])
        if not questions:
            await query.message.reply_text("❌ No questions found in this quiz to export.")
            return

        try:
            txt_content = format_quiz_to_txt(questions)
            file_data = io.BytesIO(txt_content.encode('utf-8'))
            file_data.name = f"quiz_{quiz_id}.txt"
            
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=file_data,
                caption=f"📤 Exported ({len(questions)} questions)"
            )
        except Exception as e:
            logger.exception(e)
            await query.message.reply_text(f"❌ Failed to export quiz: {e}")
        return

    if data.startswith("ed_name_"):
        quiz_id = data.replace("ed_name_", "")
        prompt_msg = await query.message.reply_text(f"✏️ Send new Quiz Name for `{quiz_id}`:")
        user_states[user.id] = {
            "step": "EDIT_NAME",
            "quiz_id": quiz_id,
            "editor_msg_id": query.message.message_id,
            "prompt_msg_id": prompt_msg.message_id
        }
        return

    if data.startswith("ed_timer_"):
        quiz_id = data.replace("ed_timer_", "")
        prompt_msg = await query.message.reply_text(f"⏱️ Send new Timer in seconds (>10) for `{quiz_id}`:")
        user_states[user.id] = {
            "step": "EDIT_TIMER",
            "quiz_id": quiz_id,
            "editor_msg_id": query.message.message_id,
            "prompt_msg_id": prompt_msg.message_id
        }
        return

    if data.startswith("ed_neg_"):
        quiz_id = data.replace("ed_neg_", "")
        prompt_msg = await query.message.reply_text(f"➖ Send Negative Marking per wrong answer for `{quiz_id}` (e.g. 0, 0.25, 0.50, 1.0):")
        user_states[user.id] = {
            "step": "EDIT_NEGATIVE",
            "quiz_id": quiz_id,
            "editor_msg_id": query.message.message_id,
            "prompt_msg_id": prompt_msg.message_id
        }
        return

    if data.startswith("ed_view_"):
        quiz_id = data.replace("ed_view_", "")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        questions = quiz_data.get("questions", [])
        if not questions:
            await query.message.reply_text("❌ No questions found in this quiz.")
            return

        lines = [f"👁️ <b>PREVIEW QUESTIONS ({len(questions)} Questions)</b>\n"]
        for idx, q in enumerate(questions[:25], start=1):
            q_text = html.escape(str(q.get("question_text", "")))
            lines.append(f"<b>Q{idx}. {q_text}</b>")
            for o_idx, opt in enumerate(q.get("options", [])):
                safe_opt = html.escape(str(opt))
                if o_idx == q.get("correct_option_id", 0):
                    lines.append(f"   • {safe_opt} ✅")
                else:
                    lines.append(f"   • {safe_opt}")
            lines.append("")

        if len(questions) > 25:
            lines.append(f"<i>...and {len(questions) - 25} more questions. Export file to view all.</i>")

        msg_text = "\n".join(lines)
        keyboard = [[InlineKeyboardButton("🔙 Back to Editor", callback_data=f"sec_back_{quiz_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
        return

    if data.startswith("ed_del_"):
        quiz_id = data.replace("ed_del_", "")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return

        safe_name = html.escape(str(quiz_data.get("name", "Quiz")))
        safe_quiz_id = html.escape(str(quiz_id))

        msg_text = (
            f"⚠️ <b>CONFIRM DELETE QUIZ</b>\n\n"
            f"Are you sure you want to delete this quiz?\n"
            f"📌 <b>Name:</b> {safe_name}\n"
            f"🆔 <b>Quiz ID:</b> <code>{safe_quiz_id}</code>\n\n"
            f"<i>This action cannot be undone!</i>"
        )
        keyboard = [
            [
                InlineKeyboardButton("🗑️ Yes, Delete Permanently", callback_data=f"ed_delconf_{quiz_id}"),
                InlineKeyboardButton("❌ Cancel", callback_data=f"sec_back_{quiz_id}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        try:
            await query.message.edit_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
        except Exception:
            await query.message.reply_text(msg_text, reply_markup=reply_markup, parse_mode="HTML")
        return

    if data.startswith("ed_delconf_"):
        quiz_id = data.replace("ed_delconf_", "")
        db.delete_quiz(quiz_id)
        try:
            await query.message.edit_text(f"✅ Quiz `{quiz_id}` deleted permanently!")
        except Exception:
            await query.message.reply_text(f"✅ Quiz `{quiz_id}` deleted permanently!")
        return

    if data.startswith("ed_addq_"):
        quiz_id = data.replace("ed_addq_", "")
        user_states[user.id] = {"step": "EDIT_ADD_QUESTIONS", "quiz_id": quiz_id}
        await query.message.reply_text(
            f"➕ Send additional questions for `{quiz_id}` in standard format.\nWhen done sending, type /done_edit"
        )
        return

    if data.startswith("ed_shuf_"):
        quiz_id = data.replace("ed_shuf_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            import random
            questions = quiz_data["questions"]
            random.shuffle(questions)
            db.update_quiz_questions(quiz_id, questions)
            quiz_data["questions"] = questions
            await send_quiz_editor_screen(update, context, quiz_data)
        else:
            await query.message.reply_text("❌ Quiz not found.")
        return

    if data == "ed_close":
        try:
            await query.message.delete()
        except Exception:
            pass
        return

    if data.startswith("sec_tog_"):
        quiz_id = data.replace("sec_tog_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            new_state = 1 if quiz_data.get("sections_enabled", 0) == 0 else 0
            db.update_quiz_sections_enabled(quiz_id, new_state)
            quiz_data["sections_enabled"] = new_state
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    if data.startswith("sec_mgr_"):
        quiz_id = data.replace("sec_mgr_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_section_manager_screen(update, context, quiz_data)
        return

    if data.startswith("sec_back_"):
        quiz_id = data.replace("sec_back_", "")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_quiz_editor_screen(update, context, quiz_data)
        return

    if data.startswith("sec_clr_"):
        quiz_id = data.replace("sec_clr_", "")
        db.update_quiz_sections(quiz_id, [])
        await query.message.reply_text(f"🗑️ All sections cleared for Quiz `{quiz_id}`.")
        quiz_data = db.get_quiz(quiz_id)
        if quiz_data:
            await send_section_manager_screen(update, context, quiz_data)
        return

    if data.startswith("sec_add_"):
        quiz_id = data.replace("sec_add_", "")
        quiz_data = db.get_quiz(quiz_id)
        if not quiz_data:
            await query.message.reply_text("❌ Quiz not found.")
            return
        user_states[user.id] = {"step": "ADD_SEC_NAME", "quiz_id": quiz_id}
        await query.message.reply_text("📌 Section Name send karein (e.g. 🏛️ History):")
        return


async def chat_shared_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user:
        return

    chat_shared = update.message.chat_shared
    if not chat_shared:
        return

    selected_group_id = chat_shared.chat_id
    state = user_states.get(user.id, {})
    quiz_id = state.get("launch_quiz_id")

    if not quiz_id:
        await update.message.reply_text(
            "❌ Quiz session expired or missing. Please click '🎯 Group' again on the Quiz screen.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    quiz_data = db.get_quiz(quiz_id)
    if not quiz_data:
        await update.message.reply_text(
            f"❌ Quiz ID `{quiz_id}` not found in database.",
            reply_markup=ReplyKeyboardRemove()
        )
        return

    # Clean up waiting state
    if user.id in user_states:
        del user_states[user.id]

    logger.info(f"Triggering Quiz {quiz_id} setup wizard in selected group_id={selected_group_id}")
    await update.message.reply_text(
        f"🚀 Quiz '{quiz_data['name']}' setup wizard sent to selected group (`{selected_group_id}`)!",
        reply_markup=ReplyKeyboardRemove()
    )
    await send_launch_wizard_step1(context.bot, selected_group_id, quiz_data, initiator_user_id=user.id)


# ==========================================
# QUIZ EXECUTION ENGINE
# ==========================================

def cleanup_quiz_session(quiz_id: str):
    active_quizzes.pop(quiz_id, None)
    to_delete = [p_id for p_id, info in poll_id_map.items() if info.get("quiz_id") == quiz_id]
    for p_id in to_delete:
        poll_id_map.pop(p_id, None)


# Helper to track launch wizard state per message/chat
group_wizard_sessions: Dict[str, Dict[str, Any]] = {}
user_last_launch_settings: Dict[int, Dict[str, Any]] = {}
group_last_launch_settings: Dict[int, Dict[str, Any]] = {}

def get_last_settings(user_id: int, chat_id: int, quiz_data: dict) -> dict:
    if user_id in user_last_launch_settings:
        return user_last_launch_settings[user_id]
    if chat_id in group_last_launch_settings:
        return group_last_launch_settings[chat_id]
    return {
        "correct_mark": 1.0,
        "timer": quiz_data.get("timer", 20)
    }

def save_last_settings(user_id: int, chat_id: int, timer: int, mark: float):
    setting = {"correct_mark": float(mark), "timer": int(timer)}
    user_last_launch_settings[user_id] = setting
    group_last_launch_settings[chat_id] = setting

async def send_launch_wizard_step1(bot, group_id: int, quiz_data: dict, reply_to_msg_id: int = None, initiator_user_id: int = None):
    quiz_id = quiz_data["quiz_id"]
    default_timer = quiz_data.get("timer", 20)
    
    text = (
        "🎯 **Correct mark per question?**\n\n"
        "Or tap ⚡ **Quick Start** to launch immediately with your last-used settings."
    )
    keyboard = [
        [
            InlineKeyboardButton("1", callback_data=f"qw_mk_1_{quiz_id}"),
            InlineKeyboardButton("2", callback_data=f"qw_mk_2_{quiz_id}"),
            InlineKeyboardButton("3", callback_data=f"qw_mk_3_{quiz_id}"),
            InlineKeyboardButton("4", callback_data=f"qw_mk_4_{quiz_id}"),
            InlineKeyboardButton("5", callback_data=f"qw_mk_5_{quiz_id}")
        ],
        [
            InlineKeyboardButton("⏭️ Skip (default 1)", callback_data=f"qw_mk_skip_{quiz_id}")
        ],
        [
            InlineKeyboardButton("⚡ Quick Start (your last settings)", callback_data=f"qw_quick_{quiz_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        msg = await bot.send_message(
            chat_id=group_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
            reply_to_message_id=reply_to_msg_id
        )
    except Exception:
        msg = await bot.send_message(
            chat_id=group_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    wizard_key = f"{group_id}_{msg.message_id}"
    group_wizard_sessions[wizard_key] = {
        "quiz_id": quiz_id,
        "correct_mark": 1.0,
        "timer": default_timer,
        "step": "WAITING_MARKS",
        "initiator_id": initiator_user_id
    }
    return msg


async def run_quiz_session(bot, group_id: int, quiz_data: dict, status_msg=None, custom_timer=None, custom_correct_mark=None):
    if quiz_engine_bot is not None:
        bot = quiz_engine_bot

    quiz_id = quiz_data["quiz_id"]
    name = quiz_data["name"]

    # Protection against duplicate running timers/schedulers
    if quiz_id in active_quizzes:
        existing_session = active_quizzes[quiz_id]
        if existing_session.get("active", False) and not existing_session.get("stopped", False):
            logger.warning(f"Quiz {quiz_id} is already active. Preventing duplicate launch.")
            if status_msg:
                try:
                    await status_msg.reply_text(f"⚠️ Quiz '{name}' is already running!")
                except Exception:
                    pass
            return

    timer = int(custom_timer) if custom_timer is not None else quiz_data.get("timer", 20)
    correct_mark = float(custom_correct_mark) if custom_correct_mark is not None else 1.0
    questions = quiz_data["questions"]
    total_q = len(questions)
    sec_enabled = quiz_data.get("sections_enabled", 0)
    sections = quiz_data.get("sections", [])
    if len(sections) > 0:
        sec_enabled = 1
    sections = sorted(sections, key=lambda x: x["start"])

    # Initialize active quiz tracking
    active_session = {
        "quiz_id": quiz_id,
        "group_id": group_id,
        "name": name,
        "timer": timer,
        "correct_mark": correct_mark,
        "total_questions": total_q,
        "sections_enabled": sec_enabled,
        "sections": sections,
        "participants": {},
        "active": True,
        "paused": False,
        "stopped": False,
        "leaderboard_sent": False
    }
    active_quizzes[quiz_id] = active_session

    try:
        # 1. Send announcement in Group with Retry for transient network timeouts
        announcement_sent = False
        announcement_text = (
            f"🚀 Quiz Starting!\n\n"
            f"📜 Quiz Name: {name}\n"
            f"🔢 Total Questions: {total_q}\n"
            f"⏳ Time per question: {timer}s"
        )

        for ann_attempt in range(1, 4):
            try:
                logger.info(f"Sending start announcement to group {group_id}... (attempt {ann_attempt}/3)")
                await bot.send_message(chat_id=group_id, text=announcement_text)
                announcement_sent = True
                break
            except Exception as ann_err:
                logger.warning(f"[API TIMEOUT] send_message (announcement) attempt {ann_attempt}/3 failed: {ann_err}")
                if ann_attempt < 3:
                    await asyncio.sleep(1.0)

        if not announcement_sent:
            logger.error(f"Failed to send quiz start message in group {group_id} after 3 attempts.")
            if status_msg:
                try:
                    await status_msg.reply_text(
                        f"❌ Failed to start quiz in Group ({group_id}): Connection/Timeout error after 3 attempts."
                    )
                except Exception:
                    pass
            cleanup_quiz_session(quiz_id)
            return

        # 2. Iterate through questions using a while loop to ensure index advances only after successful poll creation
        idx = 1
        last_poll_close_time = None

        while idx <= total_q:
            t_loop_start = time.monotonic()
            if last_poll_close_time is not None:
                gap_ms = (t_loop_start - last_poll_close_time) * 1000.0
                print(f"[QUIZ TIMING] Q{idx} loop started | Gap from last poll finish: {gap_ms:.2f}ms", flush=True)

            if active_session.get("stopped", False):
                break
            while active_session.get("paused", False):
                if active_session.get("stopped", False):
                    break
                await asyncio.sleep(0.5)

            if active_session.get("stopped", False):
                break

            # Track questions_asked in session for accurate unanswered calculation if quiz is stopped early
            active_session["questions_asked"] = idx

            try:
                t_prep_start = time.monotonic()
                q_item = questions[idx - 1]
                while isinstance(q_item, list) and len(q_item) > 0:
                    q_item = q_item[0]

                if not isinstance(q_item, dict):
                    logger.error(f"Invalid question item at index {idx}: {q_item}")
                    idx += 1
                    continue

                raw_question = clean_question_text(str(q_item.get("question_text", "")).strip())
                if not raw_question:
                    raw_question = f"Question {idx}"

                raw_opts = q_item.get("options", [])
                if not isinstance(raw_opts, list):
                    raw_opts = [str(raw_opts)]

                options = [str(opt).strip() for opt in raw_opts if str(opt).strip()]
                if len(options) < 2:
                    while len(options) < 2:
                        options.append(f"Option {len(options)+1}")
                if len(options) > 10:
                    options = options[:10]

                try:
                    correct_id = int(q_item.get("correct_option_id", 0))
                except Exception:
                    correct_id = 0

                if correct_id < 0 or correct_id >= len(options):
                    correct_id = 0

                print(f"[QUIZ TIMING] Q{idx} question preparation: {((time.monotonic() - t_prep_start) * 1000.0):.2f}ms", flush=True)

                # Section Transition check before question starts
                t_sec_start = time.monotonic()
                if sec_enabled == 1 and sections:
                    match_sec_idx = None
                    for s_i, s in enumerate(sections):
                        if s["start"] == idx:
                            match_sec_idx = s_i
                            break

                    if match_sec_idx is not None:
                        curr_sec = sections[match_sec_idx]
                        if match_sec_idx == 0:
                            sec_msg = (
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"{curr_sec['name']}\n"
                                f"📚 SECTION 1\n"
                                f"━━━━━━━━━━━━━━━━━━\n\n"
                                f"🔥 GET READY!\n"
                                f"━━━━━━━━━━━━━━━━━━"
                            )
                        else:
                            prev_sec = sections[match_sec_idx - 1]
                            q_count_in_prev_sec = prev_sec["end"] - prev_sec["start"] + 1
                            sec_msg = (
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"{prev_sec['name']} COMPLETED [✅] {q_count_in_prev_sec} Questions Completed\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"{curr_sec['name']} NEXT SECTION\n"
                                f"📚 Questions {curr_sec['start']}–{curr_sec['end']}\n"
                                f"━━━━━━━━━━━━━━━━━━"
                            )
                        try:
                            await bot.send_message(chat_id=group_id, text=sec_msg)
                        except Exception as e:
                            logger.error(f"Error sending section announcement: {e}")
                print(f"[QUIZ TIMING] Q{idx} section check: {((time.monotonic() - t_sec_start) * 1000.0):.2f}ms", flush=True)

                # Send question photo if available
                photo_file_id = q_item.get("photo_file_id")
                if photo_file_id:
                    for photo_attempt in range(1, 4):
                        if active_session.get("stopped", False):
                            break
                        try:
                            await bot.send_photo(chat_id=group_id, photo=photo_file_id, protect_content=True)
                            await asyncio.sleep(0.2)
                            break
                        except Exception as pe:
                            logger.error(f"Error sending photo for Q{idx} (attempt {photo_attempt}/3): {pe}")
                            if photo_attempt < 3:
                                await asyncio.sleep(0.5)

                # Handle full question and options ONLY if exceeding Poll card capacity (200 chars question / 40 chars options)
                t_long_start = time.monotonic()
                q_text = f"[{idx}/{total_q}] {raw_question}"
                has_long_opt = any(len(opt) > 40 for opt in options)
                is_long_q = len(q_text) > 200
                if is_long_q or has_long_opt:
                    if has_long_opt:
                        opt_prefixes = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]
                        formatted_opts = []
                        for o_i, opt in enumerate(options):
                            pref = opt_prefixes[o_i] if o_i < len(opt_prefixes) else f"{o_i+1}"
                            formatted_opts.append(f"  {pref}. {opt}")
                        
                        long_msg_text = (
                            f"📋 Q{idx}/{total_q}\n"
                            f"❓ {raw_question}\n\n"
                            f"🔤 Options:\n" +
                            "\n".join(formatted_opts)
                        )
                    else:
                        long_msg_text = f"📋 Q{idx}/{total_q} ❓ {raw_question}"
                    # Retry sending long message up to 5 times if rate-limited or transient network error occurs
                    for msg_attempt in range(1, 6):
                        if active_session.get("stopped", False):
                            break
                        try:
                            await bot.send_message(chat_id=group_id, text=long_msg_text, protect_content=True)
                            print(f"[QUIZ TIMING] Q{idx} full question and options sent: {((time.monotonic() - t_long_start) * 1000.0):.2f}ms", flush=True)
                            break
                        except RetryAfter as e:
                            retry_wait = float(e.retry_after)
                            logger.warning(f"Rate limited on long message Q{idx}. Waiting {retry_wait}s...")
                            await asyncio.sleep(retry_wait)
                        except Exception as e:
                            logger.error(f"Error sending long question message Q{idx} (attempt {msg_attempt}/5): {e}")
                            if msg_attempt < 5:
                                await asyncio.sleep(0.3)
                    
                    # Short breather delay to prevent hitting group rate limit between message & poll
                    await asyncio.sleep(0.1)

                # Send poll with robust retry & RetryAfter handling
                poll_msg = None
                poll_attempts = 4
                current_wait = active_session.get("timer", timer)
                open_p = min(max(5, int(current_wait)), 600)
                q_text = f"[{idx}/{total_q}] {raw_question}"
                poll_question_text = truncate_text(q_text, 200)
                display_options = [truncate_text(opt, 40) for opt in options]

                q_explanation = q_item.get("explanation", "").strip()
                if q_explanation:
                    q_explanation = truncate_text(q_explanation, 200)

                t_poll_create_start = time.monotonic()
                print(f"[QUIZ TIMING] Q{idx} poll creation started", flush=True)

                for attempt in range(1, poll_attempts + 1):
                    if active_session.get("stopped", False):
                        break
                    try:
                        poll_kwargs = {
                            "chat_id": group_id,
                            "question": poll_question_text,
                            "options": display_options,
                            "type": Poll.QUIZ,
                            "correct_option_id": correct_id,
                            "is_anonymous": False,
                            "open_period": open_p,
                            "protect_content": True
                        }
                        if q_explanation:
                            poll_kwargs["explanation"] = q_explanation

                        poll_msg = await bot.send_poll(**poll_kwargs)
                        break
                    except RetryAfter as e:
                        retry_wait = float(e.retry_after)
                        logger.warning(f"[quiz_id={quiz_id}] Telegram Rate Limit (429) for Q{idx}. Waiting {retry_wait}s (attempt {attempt}/{poll_attempts})...")
                        if retry_wait >= 5.0:
                            try:
                                await bot.send_message(chat_id=group_id, text=f"⏳ Telegram rate limit active: waiting {int(retry_wait)}s for Q{idx}...")
                            except Exception:
                                pass
                        await asyncio.sleep(retry_wait)
                    except Exception as e:
                        logger.error(f"[quiz_id={quiz_id}] Error sending poll Q{idx} (attempt {attempt}/{poll_attempts}): {e}")
                        if attempt < poll_attempts:
                            backoff = min(1.5, 0.3 * attempt)
                            await asyncio.sleep(backoff)
                        else:
                            logger.error(f"[quiz_id={quiz_id}] Failed to send poll for Q{idx} after {poll_attempts} attempts. Skipping question.")
                            try:
                                await bot.send_message(chat_id=group_id, text=f"⚠️ Question {idx} skipped due to Telegram network delay. Moving to next question...")
                            except Exception:
                                pass
                            poll_msg = None
                            break

                if active_session.get("stopped", False):
                    break

                if not poll_msg:
                    # Skip to next question smoothly if poll failed to send
                    idx += 1
                    continue

                # Capture high-resolution monotonic timer ONLY AFTER successful poll creation
                poll_created_monotonic = time.monotonic()
                poll_created_wall_time = time.time()
                rtt_sec = poll_created_monotonic - t_poll_create_start

                # Subtract half of the API RTT so local timer expires at the exact millisecond Telegram server auto-closes the poll
                effective_wait = max(0.5, current_wait - (rtt_sec / 2.0))
                target_end_monotonic = poll_created_monotonic + effective_wait

                poll_creation_ms = rtt_sec * 1000.0
                print(f"[QUIZ TIMING] Q{idx} poll created: {poll_creation_ms:.2f}ms | RTT offset: {(rtt_sec * 500.0):.2f}ms | Effective wait: {effective_wait:.3f}s", flush=True)

                # Register poll in map for user answer score tracking
                p_id = poll_msg.poll.id
                poll_id_map[p_id] = {
                    "quiz_id": quiz_id,
                    "q_idx": idx,
                    "correct_option_id": correct_id,
                    "poll_start_time": poll_created_wall_time
                }

                # Adaptive precision timer loop (zero-drift, non-busy with safety hard limit)
                loop_max_end = target_end_monotonic + 5.0
                while True:
                    if active_session.get("stopped", False):
                        break
                    if active_session.get("paused", False):
                        await asyncio.sleep(0.5)
                        continue

                    now_m = time.monotonic()
                    if now_m >= target_end_monotonic or now_m >= loop_max_end:
                        break

                    remaining = target_end_monotonic - now_m
                    if remaining > 1.0:
                        await asyncio.sleep(min(0.5, remaining - 0.2))
                    elif remaining > 0.1:
                        await asyncio.sleep(min(0.1, remaining - 0.02))
                    else:
                        await asyncio.sleep(max(0.005, remaining))

                last_poll_close_time = time.monotonic()
                print(f"[QUIZ TIMING] Q{idx} poll ended", flush=True)

                if active_session.get("stopped", False):
                    try:
                        await bot.stop_poll(chat_id=group_id, message_id=poll_msg.message_id)
                    except Exception:
                        pass
                    break

                t_score_start = time.monotonic()
                # Result processing and score updates happen asynchronously via PollAnswerHandler
                print(f"[QUIZ TIMING] Q{idx} result processing & score update: {((time.monotonic() - t_score_start) * 1000.0):.2f}ms", flush=True)

                idx += 1
            except Exception as q_err:
                logger.error(f"[quiz_id={quiz_id}] Exception while handling Q{idx}: {q_err}")
                try:
                    await bot.send_message(chat_id=group_id, text=f"⚠️ Question {idx} me error aaya: {q_err}. Agla question start ho raha hai...")
                except Exception:
                    pass
                idx += 1  # Always advance to next question so quiz NEVER hangs

        # Short grace delay so any last-second poll answers from Telegram API register
        await asyncio.sleep(1.0)

        # 3. Finalize Quiz and Send Leaderboard
        if active_session.get("stopped", False):
            await bot.send_message(chat_id=group_id, text="⏹️ Quiz Stopped! Calculating results for attempted questions...")
        
        await send_quiz_leaderboard(bot, group_id, active_session)

    except Exception as e:
        logger.error(f"[quiz_id={quiz_id}] Exception in run_quiz_session: {e}")
        # Try to send leaderboard on crash if not already sent
        try:
            await send_quiz_leaderboard(bot, group_id, active_session)
        except Exception as le:
            logger.error(f"[quiz_id={quiz_id}] Failed to send leaderboard on crash: {le}")
    finally:
        cleanup_quiz_session(quiz_id)


# ==========================================
# POLL ANSWER HANDLER
# ==========================================

async def handle_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    if not answer:
        return

    p_id = answer.poll_id
    poll_info = poll_id_map.get(p_id)
    if not poll_info:
        return

    quiz_id = poll_info["quiz_id"]
    active_session = active_quizzes.get(quiz_id)
    if not active_session:
        return

    if active_session.get("stopped", False) or active_session.get("leaderboard_sent", False):
        return

    user = answer.user
    user_id = user.id
    selected_options = answer.option_ids

    if not selected_options:
        return

    user_selected = selected_options[0]
    q_idx = poll_info["q_idx"]
    correct_option_id = poll_info["correct_option_id"]
    poll_start_time = poll_info["poll_start_time"]
    time_taken = max(0.0, time.time() - poll_start_time)

    participants = active_session["participants"]
    if user_id not in participants:
        full_name = user.first_name + (f" {user.last_name}" if user.last_name else "")
        participants[user_id] = {
            "user_id": user_id,
            "name": full_name,
            "username": user.username or "",
            "correct": 0,
            "wrong": 0,
            "attempted_set": set(),
            "correct_q_indices": set(),
            "total_time": 0.0
        }

    p_data = participants[user_id]

    # Prevent double counting time / score if same question is answered twice
    if q_idx not in p_data["attempted_set"]:
        p_data["attempted_set"].add(q_idx)
        p_data["total_time"] += time_taken
        if "correct_q_indices" not in p_data:
            p_data["correct_q_indices"] = set()

        try:
            is_correct = int(user_selected) == int(correct_option_id)
        except (ValueError, TypeError):
            is_correct = str(user_selected).strip() == str(correct_option_id).strip()

        if is_correct:
            p_data["correct"] += 1
            p_data["correct_q_indices"].add(q_idx)
        else:
            p_data["wrong"] += 1


# ==========================================
# FINAL LEADERBOARD GENERATION
# ==========================================

async def send_quiz_leaderboard(bot, group_id: int, session: dict):
    if session.get("leaderboard_sent", False):
        return
    session["leaderboard_sent"] = True

    participants = list(session["participants"].values())
    
    # For early-stopped quizzes or normal quizzes, use actual questions asked if available
    total_q = session["total_questions"]
    if session.get("stopped", False) and "questions_asked" in session:
        total_q = session["questions_asked"]
    elif "questions_asked" in session and session["questions_asked"] > 0:
        total_q = session["questions_asked"]

    quiz_name = session.get("name", "Quiz")
    quiz_id = session.get("quiz_id")
    correct_mark = float(session.get("correct_mark", 1.0))

    # Fetch quiz negative marking setting
    quiz_data = db.get_quiz(quiz_id) if quiz_id else None
    neg_rate = float(quiz_data.get("negative", 0.0)) if quiz_data else 0.0

    if not participants:
        await bot.send_message(
            chat_id=group_id,
            text=f"🏁 Quiz Completed!\n\n📝 {quiz_name}\n\nNo participants attempted the quiz."
        )
        return

    # Process score, accuracy, and format participant list
    formatted_participants = []
    max_possible_score = float(total_q) * correct_mark

    for p in participants:
        correct = int(p.get("correct", 0))
        wrong = int(p.get("wrong", 0))
        attempted = correct + wrong
        unanswered = max(0, total_q - attempted) if total_q > 0 else 0

        # Score = (Correct × Correct Mark) - (Wrong × Negative Mark)
        score = (float(correct) * correct_mark) - (float(wrong) * neg_rate)
        total_time = float(p.get("total_time", 0.0))
        
        # Score Percentage = max(0, Score) / Maximum Possible Score × 100
        score_pct = (max(0.0, score) / max_possible_score * 100.0) if max_possible_score > 0 else 0.0
        
        # Accuracy = Correct / (Correct + Wrong) × 100
        accuracy = (float(correct) / float(attempted) * 100.0) if attempted > 0 else 0.0

        p_info = {
            "name": p.get("name", "User"),
            "correct": correct,
            "wrong": wrong,
            "unanswered": unanswered,
            "score": score,
            "total_time": total_time,
            "score_pct": score_pct,
            "accuracy": accuracy,
            "attempted": attempted
        }
        formatted_participants.append(p_info)

    # Sorting criteria: 1. Score desc, 2. Accuracy desc, 3. Total Time asc
    sorted_p = sorted(formatted_participants, key=lambda x: (-x["score"], -x["accuracy"], x["total_time"]))

    msg_lines = [
        "🏁 Quiz Completed!\n",
        f"📝 {quiz_name}\n",
        "🎯 Top Performers:\n"
    ]
    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}

    for idx, p in enumerate(sorted_p, start=1):
        badge = rank_emojis.get(idx, f"{idx}.")
        time_str = format_time(p["total_time"])
        line = (
            f"{badge} {p['name']} | ✅ {p['correct']} | ❌ {p['wrong']} | ⏭️ {p['unanswered']} | "
            f"🎯 {p['score']:.2f} | ⏱️ {time_str} | 📊 {p['score_pct']:.1f}% | 🚀 {p['accuracy']:.1f}%\n"
            f"────────────────"
        )
        msg_lines.append(line)

    full_text = "\n".join(msg_lines)

    if len(full_text) <= 4000:
        await bot.send_message(chat_id=group_id, text=full_text)
    else:
        chunk = ""
        for line in msg_lines:
            if len(chunk) + len(line) + 2 > 4000:
                await bot.send_message(chat_id=group_id, text=chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await bot.send_message(chat_id=group_id, text=chunk)


from telegram import Bot

quiz_engine_bot = None

async def post_init(application):
    global quiz_engine_bot
    limits = httpx.Limits(max_keepalive_connections=30, max_connections=60)
    request = HTTPXRequest(
        connection_pool_size=60,
        connect_timeout=15.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=15.0,
        httpx_kwargs={"limits": limits}
    )
    quiz_engine_bot = Bot(token=config.BOT_TOKEN, request=request)
    await quiz_engine_bot.initialize()
    task = asyncio.create_task(scheduler_loop(application))
    application.bot_data["scheduler_task"] = task


async def post_shutdown(application):
    task = application.bot_data.get("scheduler_task")
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def clone_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    # Clear any pending interactive quiz creation state
    user_states.pop(user.id, None)


    target_msg = msg.reply_to_message if msg.reply_to_message else msg

    # Log incoming message structure for debugging
    logger.info("=== QUIZ CLONE DEBUG LOG ===")
    logger.info(f"User ID: {user.id}")
    logger.info(f"Message Text: {repr(target_msg.text)}")
    logger.info(f"Via Bot: {target_msg.via_bot.username if target_msg.via_bot else None}")

    token = None
    # Extract token from reply_markup or text
    if target_msg.reply_markup and target_msg.reply_markup.inline_keyboard:
        logger.info(f"Reply Markup: {target_msg.reply_markup.to_dict()}")
        for row in target_msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.url and ("QuizBot?start=" in btn.url or "QuizBot?startgroup=" in btn.url):
                    m = re.search(r't\.me/QuizBot\?(?:start|startgroup)=([a-zA-Z0-9_\-]+)', btn.url, re.IGNORECASE)
                    if m:
                        token = m.group(1)
                elif btn.switch_inline_query:
                    raw_siq = btn.switch_inline_query.strip()
                    token = re.sub(r'^(?:quiz:|start:)', '', raw_siq, flags=re.IGNORECASE).strip()

    if not token and target_msg.text:
        m = re.search(r'(?:QuizBot\?(?:start|startgroup)=|token:?\s*|quiz:)([a-zA-Z0-9_\-]+)', target_msg.text, re.IGNORECASE)
        if m:
            token = m.group(1)

    if token:
        token = re.sub(r'^(?:quiz:|start:)', '', token, flags=re.IGNORECASE).strip()

    logger.info(f"[CLONE DEBUG 1/4] /clone received | Target msg present: {target_msg is not None}")
    logger.info(f"[CLONE DEBUG 2/4] Extracted token: {token}")

    if not token:
        await msg.reply_text(
            "⚠️ <b>Quiz Token Not Found!</b>\n\n"
            "Please reply to a <code>@QuizBot</code> shared message with <code>/clone</code>, "
            "or send the <code>t.me/QuizBot?start=...</code> link.",
            parse_mode="HTML"
        )
        return

    # Extract Title and Questions count if present
    text = target_msg.text or ""
    title_match = re.search(r"Quiz\s*['\"](.*?)['\"]", text, re.DOTALL)
    q_count_match = re.search(r"(\d+)\s*questions", text, re.IGNORECASE)
    time_match = re.search(r"(\d+)\s*sec", text, re.IGNORECASE)

    title = title_match.group(1).strip() if title_match else "QuizBot Quiz"
    q_count = q_count_match.group(1) if q_count_match else "35"
    time_limit = time_match.group(1) if time_match else "15"

    logger.info(f"Extracted QuizBot Token: {token} | Title: {title} | Count: {q_count}")

    status_msg = await msg.reply_text(
        f"🚀 <b>Connecting...</b>\n"
        f"<b>Token:</b> <code>{token}</code>",
        parse_mode="HTML"
    )

    last_update_time = time.monotonic()

    async def on_progress(current: int, total: int, tok: str):
        nonlocal last_update_time
        now = time.monotonic()
        if now - last_update_time < 2.0 and current < total:
            return
        last_update_time = now
        bar = mtproto_worker.format_progress_bar(current, total) if mtproto_worker else "[░░░░░░░░░░] 0%"
        eta = mtproto_worker.format_eta(current, total) if mtproto_worker else "0s"
        text_update = (
            f"🔄 <b>Cloning...</b>\n"
            f"{bar}\n"
            f"📊 <b>{current}/{total}</b>\n"
            f"⏱️ <b>ETA:</b> {eta}"
        )
        try:
            await status_msg.edit_text(text_update, parse_mode="HTML")
        except Exception:
            pass

    if mtproto_worker and mtproto_worker.is_mtproto_configured():
        try:
            questions = await mtproto_worker.clone_quiz_from_token(token, progress_callback=on_progress)
            if questions:
                try:
                    time_int = int(time_limit)
                except ValueError:
                    time_int = 15
                quiz_id = db.save_quiz(title, time_int, questions, creator_name=user.first_name or "User", creator_id=user.id)
                await status_msg.edit_text(
                    f"✅ <b>Quiz Imported Successfully</b>\n\n"
                    f"📚 <b>Questions:</b> {len(questions)}\n"
                    f"⏱ <b>Time:</b> {time_limit} sec\n\n"
                    f"<i>(Use <code>/myquizzes</code> to view or start this quiz!)</i>",
                    parse_mode="HTML"
                )

                # Generate and send TXT export file automatically
                try:
                    blocks = []
                    for q in questions:
                        clean_q = clean_question_text(q.get("question_text", ""))
                        opts = [o.strip() for o in q.get("options", [])]
                        correct_id = q.get("correct_option_id", 0)
                        if 0 <= correct_id < len(opts):
                            correct_ans = opts[correct_id]
                        else:
                            correct_ans = opts[0] if opts else ""
                        block_lines = [clean_q] + opts + [f"Answer: {correct_ans}"]
                        blocks.append("\n".join(block_lines))

                    txt_content = "\n\n".join(blocks)
                    txt_bytes = io.BytesIO(txt_content.encode("utf-8"))
                    safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_') or "quiz"
                    filename = f"{safe_title}_{len(questions)}q.txt"
                    txt_bytes.name = filename

                    await msg.reply_document(
                        document=txt_bytes,
                        filename=filename,
                        caption=f"📄 <b>TXT Export:</b> <code>{html.escape(title)}</code>\n"
                                f"📚 <b>Questions:</b> {len(questions)}\n"
                                f"🆔 <b>Quiz ID:</b> <code>{quiz_id}</code>",
                        parse_mode="HTML"
                    )
                except Exception as doc_err:
                    logger.warning(f"Failed to send TXT document: {doc_err}")

            else:
                await status_msg.edit_text(
                    f"❌ <b>Quiz Import Failed</b>\n\n"
                    f"Reason: No questions could be retrieved from @QuizBot.",
                    parse_mode="HTML"
                )
        except Exception as e:
            logger.error(f"Clone error for user {user.id}: {e}")
            await status_msg.edit_text(
                f"⚠️ <b>Cloning Error:</b> {html.escape(str(e))}\n\n"
                f"<b>Quiz Token:</b> <code>{token}</code>",
                parse_mode="HTML"
            )
    else:
        status = config.get_mtproto_status()
        api_id_status = "✅ YES" if status["API_ID_SET"] else "❌ NO (Missing TELEGRAM_API_ID)"
        api_hash_status = "✅ YES" if status["API_HASH_SET"] else "❌ NO (Missing TELEGRAM_API_HASH)"
        session_status = "✅ YES" if status["SESSION_SET"] else "❌ NO (Missing MTPROTO_SESSION_STRING)"

        logger.info(f"[CLONE CONFIG DIAGNOSTIC] API_ID: {api_id_status} | API_HASH: {api_hash_status} | SESSION: {session_status}")

        await status_msg.edit_text(
            f"🚀 <b>Quiz Token Extracted:</b> <code>{token}</code>\n"
            f"<b>Title:</b> {html.escape(title)}\n"
            f"<b>Questions:</b> {q_count} | <b>Time:</b> {time_limit}s\n\n"
            f"⚠️ <b>MTProto Environment Diagnostics:</b>\n"
            f"• <code>TELEGRAM_API_ID</code>: {api_id_status}\n"
            f"• <code>TELEGRAM_API_HASH</code>: {api_hash_status}\n"
            f"• <code>MTPROTO_SESSION_STRING</code>: {session_status}\n\n"
            f"<i>Configure missing variable(s) in Railway Dashboard ➔ Variables tab.</i>",
            parse_mode="HTML"
        )

def main():
    db.init_db()

    token = config.BOT_TOKEN
    if not token:
        print("❌ ERROR: BOT_TOKEN is missing! Please set BOT_TOKEN in .env or environment variable.")
        return

    limits = httpx.Limits(max_keepalive_connections=30, max_connections=60)
    request = HTTPXRequest(
        connection_pool_size=60,
        connect_timeout=15.0,
        read_timeout=20.0,
        write_timeout=20.0,
        pool_timeout=15.0,
        httpx_kwargs={"limits": limits}
    )

    app = (
        ApplicationBuilder()
        .token(token)
        .request(request)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Log every incoming update for debugging
    app.add_handler(MessageHandler(filters.ALL, global_update_logger), group=-1)
    app.add_handler(CallbackQueryHandler(global_update_logger), group=-1)
    app.add_error_handler(error_handler)

    # Handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("myquizzes", myquizzes_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CommandHandler("schedules", schedules_command))
    app.add_handler(CommandHandler("unschedule", unschedule_command))
    app.add_handler(CommandHandler("pause", pause_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("stop", stop_command_quiz))
    app.add_handler(CommandHandler("fast", fast_command))
    app.add_handler(CommandHandler("slow", slow_command))
    app.add_handler(CommandHandler("edit", edit_command))
    app.add_handler(CommandHandler("done_edit", done_edit_command))
    app.add_handler(CommandHandler("clone", clone_command))
    app.add_handler(InlineQueryHandler(inline_query_handler))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(PollAnswerHandler(handle_poll_answer))
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, chat_shared_handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_private_photo))
    app.add_handler(MessageHandler(filters.POLL, handle_private_poll))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_private_document))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_private_message))

    print(f"🚀 Telegram Quiz Bot starting... (OWNER_ID={config.OWNER_ID}, GROUP_ID={config.GROUP_ID})", flush=True)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "poll_answer", "inline_query"]
    )


if __name__ == "__main__":
    main()
