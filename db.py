import os
import json
import re
import random
import string
import sqlite3
import urllib.parse
from datetime import datetime
from dotenv import load_dotenv

def _safe_print(text: str):
    try:
        print(text)
    except Exception:
        try:
            print(text.encode('ascii', 'replace').decode('ascii'))
        except Exception:
            pass

# Memory cache so bot operates with zero latency
_memory_quizzes = {}
_memory_schedules = {}

# SQLite Database Setup (Offline / No MongoDB Required)
DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "quiz_bot.db")

def _get_sqlite_conn():
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception as e:
        print(f"❌ SQLite Connection Error: {e}")
        return None

def _init_sqlite():
    conn = _get_sqlite_conn()
    if conn:
        try:
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS quizzes (
                        quiz_id TEXT PRIMARY KEY,
                        name TEXT,
                        timer INTEGER,
                        creator_name TEXT,
                        creator_id INTEGER,
                        sections_enabled INTEGER,
                        sections TEXT,
                        negative REAL,
                        card_mode TEXT,
                        questions TEXT,
                        created_at TEXT
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS schedules (
                        quiz_id TEXT PRIMARY KEY,
                        scheduled_timestamp REAL,
                        time_str TEXT,
                        group_id INTEGER,
                        shuffle_mode TEXT,
                        opt_count TEXT
                    )
                """)
            _safe_print("✅ SQLite Local Database (quiz_bot.db) Initialized Successfully!")
        except Exception as e:
            _safe_print(f"❌ SQLite Table Init Error: {e}")
        finally:
            conn.close()

def _load_sqlite_to_memory():
    conn = _get_sqlite_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM quizzes")
            rows = cur.fetchall()
            for r in rows:
                q_id = r["quiz_id"]
                _memory_quizzes[q_id] = {
                    "quiz_id": q_id,
                    "name": r["name"],
                    "timer": r["timer"],
                    "creator_name": r["creator_name"] or "MAHI 💗",
                    "creator_id": r["creator_id"] or 0,
                    "sections_enabled": r["sections_enabled"] or 0,
                    "sections": json.loads(r["sections"] or "[]"),
                    "negative": float(r["negative"] or 0.0),
                    "card_mode": r["card_mode"] or "1_card",
                    "questions": json.loads(r["questions"] or "[]"),
                    "created_at": r["created_at"] or ""
                }
            
            cur.execute("SELECT * FROM schedules")
            s_rows = cur.fetchall()
            for sr in s_rows:
                q_id = sr["quiz_id"]
                _memory_schedules[q_id] = {
                    "quiz_id": q_id,
                    "scheduled_timestamp": sr["scheduled_timestamp"],
                    "time_str": sr["time_str"],
                    "group_id": sr["group_id"] or 0,
                    "shuffle_mode": sr["shuffle_mode"] or "none",
                    "opt_count": sr["opt_count"] or "all"
                }
            if rows:
                print(f"✅ Loaded {len(rows)} quizzes from SQLite local database!")
        except Exception as e:
            print(f"⚠️ SQLite Load Error: {e}")
        finally:
            conn.close()

# Initialize SQLite tables and load existing quizzes
_init_sqlite()
_load_sqlite_to_memory()

# MongoDB Setup (Optional)
RAW_MONGO_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or os.getenv("DATABASE_URL")

def fix_mongo_uri(uri):
    if not uri:
        return uri
    try:
        prefix_match = re.match(r'^(mongodb(?:\+srv)?://)([^:]+):([^@]+)@(.+)$', uri)
        if prefix_match:
            scheme = prefix_match.group(1)
            username = prefix_match.group(2)
            password = prefix_match.group(3)
            rest = prefix_match.group(4)
            quoted_user = urllib.parse.quote_plus(urllib.parse.unquote(username))
            quoted_pass = urllib.parse.quote_plus(urllib.parse.unquote(password))
            return f"{scheme}{quoted_user}:{quoted_pass}@{rest}"
    except Exception as e:
        print(f"⚠️ URI parse error: {e}")
    return uri

MONGO_URI = fix_mongo_uri(RAW_MONGO_URI)

client = None
db = None
quizzes_col = None
schedules_col = None

if MONGO_URI:
    try:
        from pymongo import MongoClient
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        try:
            db = client.get_default_database()
            if db is None:
                db = client["quiz_bot_db"]
        except Exception:
            db = client["quiz_bot_db"]
            
        quizzes_col = db["quizzes"]
        schedules_col = db["schedules"]
        _safe_print(f"✅ Successfully connected to MongoDB Database: {db.name}")
    except Exception as e:
        _safe_print(f"❌ MongoDB Connection Error: {e}")
else:
    _safe_print("ℹ️ MongoDB URI not set. Operating in Offline Local Mode (SQLite database quiz_bot.db).")

def init_db():
    if quizzes_col is not None:
        try:
            quizzes_col.create_index("quiz_id", unique=True)
            quizzes_col.create_index("creator_id")
            _safe_print("✅ MongoDB Indexes initialized!")
        except Exception as e:
            _safe_print(f"⚠️ Notice on MongoDB index creation: {e}")
    if schedules_col is not None:
        try:
            schedules_col.create_index("quiz_id", unique=True)
            schedules_col.create_index("scheduled_timestamp")
            _safe_print("✅ MongoDB Schedules indexes initialized!")
        except Exception as e:
            _safe_print(f"⚠️ Notice on MongoDB schedules index creation: {e}")

def generate_quiz_id():
    chars = string.ascii_uppercase + string.digits
    random_str = ''.join(random.choices(chars, k=7))
    return f"GG{random_str}"

def save_quiz(name: str, timer: int, questions: list, creator_name: str = "MAHI 💗", sections_enabled: int = 0, sections: list = None, creator_id: int = 0, card_mode: str = "1_card") -> str:
    if sections is None:
        sections = []
    quiz_id = generate_quiz_id()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    doc = {
        "quiz_id": quiz_id,
        "name": name,
        "timer": timer,
        "questions": questions,
        "created_at": created_at,
        "creator_name": creator_name,
        "creator_id": creator_id,
        "sections_enabled": sections_enabled,
        "sections": sections,
        "card_mode": card_mode,
        "negative": 0.0
    }
    
    # 1. Memory Cache
    _memory_quizzes[quiz_id] = doc

    # 2. SQLite Local Database
    conn = _get_sqlite_conn()
    if conn:
        try:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO quizzes (quiz_id, name, timer, creator_name, creator_id, sections_enabled, sections, negative, card_mode, questions, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    quiz_id, name, timer, creator_name, creator_id, sections_enabled,
                    json.dumps(sections), 0.0, card_mode, json.dumps(questions), created_at
                ))
            _safe_print(f"✅ Quiz {quiz_id} saved to SQLite (quiz_bot.db) successfully!")
        except Exception as e:
            _safe_print(f"❌ SQLite save_quiz error: {e}")
        finally:
            conn.close()

    # 3. MongoDB (if configured)
    if quizzes_col is not None:
        try:
            quizzes_col.insert_one(doc)
            _safe_print(f"✅ Quiz {quiz_id} saved to MongoDB successfully!")
        except Exception as e:
            _safe_print(f"❌ MongoDB insert error: {e}")
            
    return quiz_id

def get_quiz(quiz_id: str):
    if quiz_id in _memory_quizzes:
        return _memory_quizzes[quiz_id]

    # Try SQLite
    conn = _get_sqlite_conn()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("SELECT * FROM quizzes WHERE quiz_id = ?", (quiz_id,))
            r = cur.fetchone()
            if r:
                q_data = {
                    "quiz_id": r["quiz_id"],
                    "name": r["name"],
                    "timer": r["timer"],
                    "negative": float(r["negative"] or 0.0),
                    "questions": json.loads(r["questions"] or "[]"),
                    "created_at": r["created_at"] or "",
                    "creator_name": r["creator_name"] or "MAHI 💗",
                    "creator_id": r["creator_id"] or 0,
                    "sections_enabled": r["sections_enabled"] or 0,
                    "sections": json.loads(r["sections"] or "[]"),
                    "card_mode": r["card_mode"] or "1_card"
                }
                _memory_quizzes[quiz_id] = q_data
                return q_data
        except Exception as e:
            _safe_print(f"❌ SQLite get_quiz error: {e}")
        finally:
            conn.close()

    # Try MongoDB
    if quizzes_col is not None:
        try:
            doc = quizzes_col.find_one({"quiz_id": quiz_id})
            if doc:
                quiz_data = {
                    "quiz_id": doc.get("quiz_id"),
                    "name": doc.get("name"),
                    "timer": doc.get("timer"),
                    "negative": float(doc.get("negative", 0.0)),
                    "questions": doc.get("questions", []),
                    "created_at": doc.get("created_at", ""),
                    "creator_name": doc.get("creator_name", "MAHI 💗"),
                    "creator_id": doc.get("creator_id", 0),
                    "sections_enabled": doc.get("sections_enabled", 0),
                    "sections": doc.get("sections", []),
                    "card_mode": doc.get("card_mode", "1_card")
                }
                _memory_quizzes[quiz_id] = quiz_data
                return quiz_data
        except Exception as e:
            _safe_print(f"❌ MongoDB get_quiz error: {e}")
            
    return _memory_quizzes.get(quiz_id)

def get_quizzes_by_user(user_id: int = 0, limit: int = 20):
    results = []
    
    # Try memory first if populated
    for q_id, doc in reversed(list(_memory_quizzes.items())):
        if user_id == 0 or doc.get("creator_id") == user_id or "creator_id" not in doc:
            results.append(doc)
            if len(results) >= limit:
                return results

    # Try SQLite
    conn = _get_sqlite_conn()
    if conn:
        try:
            cur = conn.cursor()
            if user_id > 0:
                cur.execute("SELECT * FROM quizzes WHERE creator_id = ? OR creator_id = 0 ORDER BY rowid DESC LIMIT ?", (user_id, limit))
            else:
                cur.execute("SELECT * FROM quizzes ORDER BY rowid DESC LIMIT ?", (limit,))
            rows = cur.fetchall()
            for r in rows:
                item = {
                    "quiz_id": r["quiz_id"],
                    "name": r["name"],
                    "timer": r["timer"],
                    "questions": json.loads(r["questions"] or "[]"),
                    "created_at": r["created_at"] or "",
                    "creator_name": r["creator_name"] or "",
                    "creator_id": r["creator_id"] or 0,
                    "sections_enabled": r["sections_enabled"] or 0,
                    "sections": json.loads(r["sections"] or "[]"),
                    "card_mode": r["card_mode"] or "1_card"
                }
                _memory_quizzes[r["quiz_id"]] = item
                results.append(item)
            if results:
                return results
        except Exception as e:
            _safe_print(f"❌ SQLite get_quizzes_by_user error: {e}")
        finally:
            conn.close()

    return results

def save_schedule(quiz_id: str, scheduled_timestamp: float, time_str: str, group_id: int = 0, shuffle_mode: str = "none", opt_count: str = "all"):
    _memory_schedules[quiz_id] = {
        "quiz_id": quiz_id,
        "scheduled_timestamp": scheduled_timestamp,
        "time_str": time_str,
        "group_id": group_id,
        "shuffle_mode": shuffle_mode,
        "opt_count": opt_count
    }
    
    conn = _get_sqlite_conn()
    if conn:
        try:
            with conn:
                conn.execute("""
                    INSERT OR REPLACE INTO schedules (quiz_id, scheduled_timestamp, time_str, group_id, shuffle_mode, opt_count)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (quiz_id, scheduled_timestamp, time_str, group_id, shuffle_mode, opt_count))
        except Exception as e:
            _safe_print(f"❌ SQLite save_schedule error: {e}")
        finally:
            conn.close()

    if schedules_col is not None:
        try:
            schedules_col.update_one(
                {"quiz_id": quiz_id},
                {"$set": {
                    "quiz_id": quiz_id,
                    "scheduled_timestamp": scheduled_timestamp,
                    "time_str": time_str,
                    "group_id": group_id,
                    "shuffle_mode": shuffle_mode,
                    "opt_count": opt_count
                }},
                upsert=True
            )
        except Exception as e:
            _safe_print(f"❌ MongoDB save_schedule error: {e}")

def get_active_schedules():
    return list(_memory_schedules.values())

def delete_schedule(quiz_id: str):
    _memory_schedules.pop(quiz_id, None)
    
    conn = _get_sqlite_conn()
    if conn:
        try:
            with conn:
                conn.execute("DELETE FROM schedules WHERE quiz_id = ?", (quiz_id,))
        except Exception as e:
            _safe_print(f"❌ SQLite delete_schedule error: {e}")
        finally:
            conn.close()

    if schedules_col is not None:
        try:
            schedules_col.delete_one({"quiz_id": quiz_id})
        except Exception as e:
            _safe_print(f"❌ MongoDB delete_schedule error: {e}")

def _update_sqlite_field(quiz_id: str, field_name: str, value):
    conn = _get_sqlite_conn()
    if conn:
        try:
            with conn:
                conn.execute(f"UPDATE quizzes SET {field_name} = ? WHERE quiz_id = ?", (value, quiz_id))
        except Exception as e:
            _safe_print(f"❌ SQLite update {field_name} error: {e}")
        finally:
            conn.close()

def update_quiz_name(quiz_id: str, new_name: str):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["name"] = new_name
    _update_sqlite_field(quiz_id, "name", new_name)
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"name": new_name}})
        except Exception as e:
            _safe_print(f"❌ MongoDB update_quiz_name error: {e}")

def update_quiz_timer(quiz_id: str, new_timer: int):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["timer"] = new_timer
    _update_sqlite_field(quiz_id, "timer", new_timer)
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"timer": new_timer}})
        except Exception as e:
            _safe_print(f"❌ MongoDB update_quiz_timer error: {e}")

def update_quiz_questions(quiz_id: str, questions: list):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["questions"] = questions
    _update_sqlite_field(quiz_id, "questions", json.dumps(questions))
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"questions": questions}})
        except Exception as e:
            _safe_print(f"❌ MongoDB update_quiz_questions error: {e}")

def update_quiz_sections_enabled(quiz_id: str, enabled: int):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["sections_enabled"] = enabled
    _update_sqlite_field(quiz_id, "sections_enabled", enabled)
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"sections_enabled": enabled}})
        except Exception as e:
            _safe_print(f"❌ MongoDB update_quiz_sections_enabled error: {e}")

def update_quiz_sections(quiz_id: str, sections: list):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["sections"] = sections
    _update_sqlite_field(quiz_id, "sections", json.dumps(sections))
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"sections": sections}})
        except Exception as e:
            _safe_print(f"❌ MongoDB update_quiz_sections error: {e}")

def update_quiz_negative(quiz_id: str, negative: float):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["negative"] = negative
    _update_sqlite_field(quiz_id, "negative", negative)
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"negative": negative}})
        except Exception as e:
            _safe_print(f"❌ MongoDB update_quiz_negative error: {e}")

def update_quiz_card_mode(quiz_id: str, card_mode: str):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["card_mode"] = card_mode
    _update_sqlite_field(quiz_id, "card_mode", card_mode)
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"card_mode": card_mode}})
        except Exception as e:
            _safe_print(f"❌ MongoDB update_quiz_card_mode error: {e}")

def delete_quiz(quiz_id: str):
    _memory_quizzes.pop(quiz_id, None)
    conn = _get_sqlite_conn()
    if conn:
        try:
            with conn:
                conn.execute("DELETE FROM quizzes WHERE quiz_id = ?", (quiz_id,))
        except Exception as e:
            _safe_print(f"❌ SQLite delete_quiz error: {e}")
        finally:
            conn.close()

    if quizzes_col is not None:
        try:
            quizzes_col.delete_one({"quiz_id": quiz_id})
        except Exception as e:
            _safe_print(f"❌ MongoDB delete_quiz error: {e}")
