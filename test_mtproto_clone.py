import sys
import io
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

import config
import db
import mtproto_worker

print("=== RUNNING MTPROTO CLONE UNIT TESTS ===")

# Test 1: MTProto Config Check
print("Test 1: MTProto Config Check...")
is_cfg = mtproto_worker.is_mtproto_configured()
print(f"Is MTProto Configured: {is_cfg}")

# Test 2: Progress Bar Formatting
print("\nTest 2: Progress Bar Formatting...")
p_bar = mtproto_worker.format_progress_bar(14, 35)
print(f"Progress (14/35): '{p_bar}'")
assert "[████░░░░░░] 40%" in p_bar or "[███" in p_bar

# Test 3: ETA Formatting
print("\nTest 3: ETA Formatting...")
eta_str = mtproto_worker.format_eta(2, 35)
print(f"ETA (2/35 remaining): '{eta_str}'")
assert "m" in eta_str or "s" in eta_str

# Test 4: Database Save Test
print("\nTest 4: Database Save Test...")
db.init_db()
test_questions = [
    {
        "question_text": "हाल ही में चर्चा में रही \"Regime Change\" पुस्तक का संबंध किससे है?",
        "options": ["शी जिनपिंग", "डोनाल्ड ट्रम्प", "ब्लादिमीर पुतिन"],
        "correct_option_id": 1
    }
]
quiz_id = db.save_quiz(name="Test Cloned Quiz", timer=15, questions=test_questions, creator_id=99999)
print(f"Saved Quiz ID: {quiz_id}")
fetched = db.get_quiz(quiz_id)
assert fetched is not None
assert fetched["name"] == "Test Cloned Quiz"
assert len(fetched["questions"]) == 1
assert fetched["questions"][0]["question_text"] == "हाल ही में चर्चा में रही \"Regime Change\" पुस्तक का संबंध किससे है?"

print("\n🎉 ALL MTPROTO CLONE TESTS PASSED PERFECTLY!")
