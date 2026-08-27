import os
import json
import re
import random
import string
import urllib.parse
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

# Memory backup so bot NEVER crashes if DB has temporary delay
_memory_quizzes = {}
_memory_schedules = {}

RAW_MONGO_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI") or os.getenv("DATABASE_URL")

def fix_mongo_uri(uri):
    if not uri:
        return uri
    try:
        # Match mongodb+srv://username:password@host...
        prefix_match = re.match(r'^(mongodb(?:\+srv)?://)([^:]+):([^@]+)@(.+)$', uri)
        if prefix_match:
            scheme = prefix_match.group(1)
            username = prefix_match.group(2)
            password = prefix_match.group(3)
            rest = prefix_match.group(4)
            
            # URL encode username and password safely for RFC 3986
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
        print(f"✅ Successfully connected to MongoDB Database: {db.name}")
    except Exception as e:
        print(f"❌ MongoDB Connection Error in database.py: {e}")

def init_db():
    pass

def generate_quiz_id():
    chars = string.ascii_uppercase + string.digits
    random_str = ''.join(random.choices(chars, k=7))
    return f"GG{random_str}"

def save_quiz(name: str, timer: int, questions: list, creator_name: str = "MAHI 💗", sections_enabled: int = 0, sections: list = None, creator_id: int = 0) -> str:
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
        "sections": sections
    }
    
    # Save in memory cache immediately
    _memory_quizzes[quiz_id] = doc
    
    # Save in MongoDB
    if quizzes_col is not None:
        try:
            quizzes_col.insert_one(doc)
            print(f"✅ Quiz {quiz_id} saved to MongoDB successfully!")
        except Exception as e:
            print(f"❌ Failed to insert quiz into MongoDB: {e}")
            
    return quiz_id

def get_quiz(quiz_id: str):
    if quizzes_col is not None:
        try:
            doc = quizzes_col.find_one({"quiz_id": quiz_id})
            if doc:
                return {
                    "quiz_id": doc.get("quiz_id"),
                    "name": doc.get("name"),
                    "timer": doc.get("timer"),
                    "questions": doc.get("questions", []),
                    "created_at": doc.get("created_at", ""),
                    "creator_name": doc.get("creator_name", "MAHI 💗"),
                    "creator_id": doc.get("creator_id", 0),
                    "sections_enabled": doc.get("sections_enabled", 0),
                    "sections": doc.get("sections", [])
                }
        except Exception as e:
            print(f"❌ Error fetching quiz from MongoDB: {e}")
            
    # Fallback to memory so get_quiz NEVER returns None
    return _memory_quizzes.get(quiz_id)

def get_quizzes_by_user(user_id: int = 0, limit: int = 20):
    results = []
    if quizzes_col is not None:
        try:
            query = {}
            if user_id > 0:
                query = {"$or": [{"creator_id": user_id}, {"creator_id": {"$exists": False}}]}
            docs = list(quizzes_col.find(query).sort("_id", -1).limit(limit))
            for doc in docs:
                results.append({
                    "quiz_id": doc.get("quiz_id"),
                    "name": doc.get("name"),
                    "timer": doc.get("timer"),
                    "questions": doc.get("questions", []),
                    "created_at": doc.get("created_at", ""),
                    "creator_name": doc.get("creator_name", ""),
                    "creator_id": doc.get("creator_id", 0),
                    "sections_enabled": doc.get("sections_enabled", 0),
                    "sections": doc.get("sections", [])
                })
            if results:
                return results
        except Exception as e:
            print(f"❌ Error fetching quizzes from MongoDB: {e}")

    # Fallback to memory
    for q_id, doc in reversed(list(_memory_quizzes.items())):
        if user_id == 0 or doc.get("creator_id") == user_id or "creator_id" not in doc:
            results.append(doc)
            if len(results) >= limit:
                break
    return results

def save_schedule(quiz_id: str, scheduled_timestamp: float, time_str: str, group_id: int = 0):
    _memory_schedules[quiz_id] = {
        "quiz_id": quiz_id,
        "scheduled_timestamp": scheduled_timestamp,
        "time_str": time_str,
        "group_id": group_id
    }
    if schedules_col is not None:
        try:
            schedules_col.update_one(
                {"quiz_id": quiz_id},
                {"$set": {"quiz_id": quiz_id, "scheduled_timestamp": scheduled_timestamp, "time_str": time_str, "group_id": group_id}},
                upsert=True
            )
        except Exception as e:
            print(f"❌ Error saving schedule to MongoDB: {e}")

def get_active_schedules():
    if schedules_col is not None:
        try:
            docs = list(schedules_col.find())
            if docs:
                return [
                    {
                        "quiz_id": d.get("quiz_id"),
                        "scheduled_timestamp": d.get("scheduled_timestamp"),
                        "time_str": d.get("time_str"),
                        "group_id": d.get("group_id", 0)
                    }
                    for d in docs
                ]
        except Exception as e:
            print(f"❌ Error getting schedules from MongoDB: {e}")
            
    return list(_memory_schedules.values())

def delete_schedule(quiz_id: str):
    _memory_schedules.pop(quiz_id, None)
    if schedules_col is not None:
        try:
            schedules_col.delete_one({"quiz_id": quiz_id})
        except Exception as e:
            print(f"❌ Error deleting schedule from MongoDB: {e}")

def update_quiz_name(quiz_id: str, new_name: str):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["name"] = new_name
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"name": new_name}})
        except Exception as e:
            print(f"❌ Error updating quiz name in MongoDB: {e}")

def update_quiz_timer(quiz_id: str, new_timer: int):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["timer"] = new_timer
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"timer": new_timer}})
        except Exception as e:
            print(f"❌ Error updating quiz timer in MongoDB: {e}")

def update_quiz_questions(quiz_id: str, questions: list):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["questions"] = questions
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"questions": questions}})
        except Exception as e:
            print(f"❌ Error updating quiz questions in MongoDB: {e}")

def update_quiz_sections_enabled(quiz_id: str, enabled: int):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["sections_enabled"] = enabled
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"sections_enabled": enabled}})
        except Exception as e:
            print(f"❌ Error updating sections_enabled in MongoDB: {e}")

def update_quiz_sections(quiz_id: str, sections: list):
    if quiz_id in _memory_quizzes:
        _memory_quizzes[quiz_id]["sections"] = sections
    if quizzes_col is not None:
        try:
            quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"sections": sections}})
        except Exception as e:
            print(f"❌ Error updating sections in MongoDB: {e}")
