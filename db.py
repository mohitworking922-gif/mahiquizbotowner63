import os
import json
import random
import string
from pymongo import MongoClient

# Railway se MONGODB_URI read karein
MONGO_URI = os.getenv("MONGODB_URI") or os.getenv("MONGO_URI")

client = None
db = None
quizzes_col = None
schedules_col = None

if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI)
        # Default Database connect karein ya 'quiz_bot_db' use karein
        db = client.get_database() if client.get_default_database() is not None else client["quiz_bot_db"]
        quizzes_col = db["quizzes"]
        schedules_col = db["schedules"]
        print("✅ Successfully connected to MongoDB!")
    except Exception as e:
        print(f"❌ MongoDB Connection Error: {e}")

def init_db():
    # MongoDB mein table initialize karne ki zaroorat nahi hoti
    pass

def generate_quiz_id():
    # ID format: GG + 7 alphanumeric characters
    chars = string.ascii_uppercase + string.digits
    random_str = ''.join(random.choices(chars, k=7))
    return f"GG{random_str}"

def save_quiz(name: str, timer: int, questions: list, creator_name: str = "MAHI 💗", sections_enabled: int = 0, sections: list = None) -> str:
    if sections is None:
        sections = []
    quiz_id = generate_quiz_id()
    doc = {
        "quiz_id": quiz_id,
        "name": name,
        "timer": timer,
        "questions": questions,
        "creator_name": creator_name,
        "sections_enabled": sections_enabled,
        "sections": sections
    }
    if quizzes_col is not None:
        quizzes_col.insert_one(doc)
    return quiz_id

def get_quiz(quiz_id: str):
    if quizzes_col is not None:
        doc = quizzes_col.find_one({"quiz_id": quiz_id})
        if doc:
            return {
                "quiz_id": doc.get("quiz_id"),
                "name": doc.get("name"),
                "timer": doc.get("timer"),
                "questions": doc.get("questions", []),
                "created_at": str(doc.get("_id").generation_time) if "_id" in doc else "",
                "creator_name": doc.get("creator_name", "MAHI 💗"),
                "sections_enabled": doc.get("sections_enabled", 0),
                "sections": doc.get("sections", [])
            }
    return None

def save_schedule(quiz_id: str, scheduled_timestamp: float, time_str: str):
    if schedules_col is not None:
        schedules_col.update_one(
            {"quiz_id": quiz_id},
            {"$set": {"quiz_id": quiz_id, "scheduled_timestamp": scheduled_timestamp, "time_str": time_str}},
            upsert=True
        )

def get_active_schedules():
    if schedules_col is not None:
        docs = schedules_col.find()
        return [
            {
                "quiz_id": d.get("quiz_id"),
                "scheduled_timestamp": d.get("scheduled_timestamp"),
                "time_str": d.get("time_str")
            }
            for d in docs
        ]
    return []

def delete_schedule(quiz_id: str):
    if schedules_col is not None:
        schedules_col.delete_one({"quiz_id": quiz_id})

def update_quiz_name(quiz_id: str, new_name: str):
    if quizzes_col is not None:
        quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"name": new_name}})

def update_quiz_timer(quiz_id: str, new_timer: int):
    if quizzes_col is not None:
        quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"timer": new_timer}})

def update_quiz_questions(quiz_id: str, questions: list):
    if quizzes_col is not None:
        quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"questions": questions}})

def update_quiz_sections_enabled(quiz_id: str, enabled: int):
    if quizzes_col is not None:
        quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"sections_enabled": enabled}})

def update_quiz_sections(quiz_id: str, sections: list):
    if quizzes_col is not None:
        quizzes_col.update_one({"quiz_id": quiz_id}, {"$set": {"sections": sections}})
