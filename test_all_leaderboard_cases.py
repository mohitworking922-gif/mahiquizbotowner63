import asyncio
from unittest.mock import AsyncMock, MagicMock
import quiz_bot

async def run_all_tests():
    print("=" * 65)
    print("🚀 RUNNING ALL 5 LEADERBOARD TEST CASES")
    print("=" * 65)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # TEST CASE 1: Partial Attempt with 100% Accuracy (User Example)
    print("\n--- [TEST CASE 1] Partial Attempt with 100% Accuracy (36 Qs) ---")
    session_1 = {
        "name": "36-Q Quiz (Test 1)",
        "total_questions": 36,
        "quiz_id": None,
        "participants": {
            1: {
                "user_id": 1,
                "name": "Rohan",
                "correct": 26,
                "wrong": 0,
                "attempted_set": set(range(26)),
                "total_time": 180.0
            }
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1001, session_1)
    text1 = mock_bot.send_message.call_args[1]["text"]
    print(text1)
    assert "✅ 26 | ❌ 0 | ⏭️ 10" in text1, "Test 1 Failed: Correct/Wrong/Unanswered mismatch!"
    assert "📊 72.2% | 🚀 100.0%" in text1, "Test 1 Failed: Score%/Accuracy mismatch!"
    print("✅ TEST CASE 1 PASSED: 26 Correct, 0 Wrong, 10 Unanswered (Sum = 36), 72.2% Score, 100.0% Accuracy")

    # TEST CASE 2: Mixed Answers with Timeouts
    print("\n--- [TEST CASE 2] Mixed Answers with Timeouts (36 Qs) ---")
    mock_bot.send_message.reset_mock()
    session_2 = {
        "name": "36-Q Quiz (Test 2)",
        "total_questions": 36,
        "quiz_id": None,
        "participants": {
            2: {
                "user_id": 2,
                "name": "Priya",
                "correct": 20,
                "wrong": 5,
                "attempted_set": set(range(25)),
                "total_time": 210.0
            }
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1002, session_2)
    text2 = mock_bot.send_message.call_args[1]["text"]
    print(text2)
    assert "✅ 20 | ❌ 5 | ⏭️ 11" in text2, "Test 2 Failed: Correct/Wrong/Unanswered mismatch!"
    assert "📊 55.6% | 🚀 80.0%" in text2, "Test 2 Failed: Score%/Accuracy mismatch!"
    print("✅ TEST CASE 2 PASSED: 20 Correct, 5 Wrong, 11 Unanswered (Sum = 36), 55.6% Score, 80.0% Accuracy")

    # TEST CASE 3: All Questions Attempted (Zero Unanswered)
    print("\n--- [TEST CASE 3] All Questions Attempted (36 Qs) ---")
    mock_bot.send_message.reset_mock()
    session_3 = {
        "name": "36-Q Quiz (Test 3)",
        "total_questions": 36,
        "quiz_id": None,
        "participants": {
            3: {
                "user_id": 3,
                "name": "Amit",
                "correct": 25,
                "wrong": 11,
                "attempted_set": set(range(36)),
                "total_time": 330.0
            }
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1003, session_3)
    text3 = mock_bot.send_message.call_args[1]["text"]
    print(text3)
    assert "✅ 25 | ❌ 11 | ⏭️ 0" in text3, "Test 3 Failed: Correct/Wrong/Unanswered mismatch!"
    assert "📊 69.4% | 🚀 69.4%" in text3, "Test 3 Failed: Score%/Accuracy mismatch!"
    print("✅ TEST CASE 3 PASSED: 25 Correct, 11 Wrong, 0 Unanswered (Sum = 36), 69.4% Score, 69.4% Accuracy")

    # TEST CASE 4: Zero Attempts (Participant joined but answered nothing)
    print("\n--- [TEST CASE 4] Zero Attempts (36 Qs) ---")
    mock_bot.send_message.reset_mock()
    session_4 = {
        "name": "36-Q Quiz (Test 4)",
        "total_questions": 36,
        "quiz_id": None,
        "participants": {
            4: {
                "user_id": 4,
                "name": "Inquirer",
                "correct": 0,
                "wrong": 0,
                "attempted_set": set(),
                "total_time": 0.0
            }
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1004, session_4)
    text4 = mock_bot.send_message.call_args[1]["text"]
    print(text4)
    assert "✅ 0 | ❌ 0 | ⏭️ 36" in text4, "Test 4 Failed: Correct/Wrong/Unanswered mismatch!"
    assert "📊 0.0% | 🚀 0.0%" in text4, "Test 4 Failed: Score%/Accuracy mismatch!"
    print("✅ TEST CASE 4 PASSED: 0 Correct, 0 Wrong, 36 Unanswered (Sum = 36), 0.0% Score, 0.0% Accuracy")

    # TEST CASE 5: Dynamic Quiz Sizes (10, 50, 100 Questions)
    print("\n--- [TEST CASE 5] Dynamic Quiz Sizes ---")
    
    # 5a. 10 Questions
    mock_bot.send_message.reset_mock()
    session_10 = {
        "name": "10-Q Quiz",
        "total_questions": 10,
        "quiz_id": None,
        "participants": {
            51: {"user_id": 51, "name": "User 10Q", "correct": 7, "wrong": 2, "attempted_set": set(range(9)), "total_time": 45.0}
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1005, session_10)
    text_10 = mock_bot.send_message.call_args[1]["text"]
    assert "✅ 7 | ❌ 2 | ⏭️ 1" in text_10
    assert "📊 70.0% | 🚀 77.8%" in text_10
    print("  ✅ 10-Question dynamic quiz passed: 7 Correct, 2 Wrong, 1 Unanswered -> 70.0% Score, 77.8% Accuracy")

    # 5b. 50 Questions
    mock_bot.send_message.reset_mock()
    session_50 = {
        "name": "50-Q Quiz",
        "total_questions": 50,
        "quiz_id": None,
        "participants": {
            52: {"user_id": 52, "name": "User 50Q", "correct": 40, "wrong": 5, "attempted_set": set(range(45)), "total_time": 250.0}
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1006, session_50)
    text_50 = mock_bot.send_message.call_args[1]["text"]
    assert "✅ 40 | ❌ 5 | ⏭️ 5" in text_50
    assert "📊 80.0% | 🚀 88.9%" in text_50
    print("  ✅ 50-Question dynamic quiz passed: 40 Correct, 5 Wrong, 5 Unanswered -> 80.0% Score, 88.9% Accuracy")

    # 5c. 100 Questions
    mock_bot.send_message.reset_mock()
    session_100 = {
        "name": "100-Q Quiz",
        "total_questions": 100,
        "quiz_id": None,
        "participants": {
            53: {"user_id": 53, "name": "User 100Q", "correct": 85, "wrong": 10, "attempted_set": set(range(95)), "total_time": 600.0}
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1007, session_100)
    text_100 = mock_bot.send_message.call_args[1]["text"]
    assert "✅ 85 | ❌ 10 | ⏭️ 5" in text_100
    assert "📊 85.0% | 🚀 89.5%" in text_100
    print("  ✅ 100-Question dynamic quiz passed: 85 Correct, 10 Wrong, 5 Unanswered -> 85.0% Score, 89.5% Accuracy")

    print("\n" + "=" * 65)
    print("🎉 ALL 5 TEST CASES COMPLETED & PASSED WITH 100% SUCCESS!")
    print("=" * 65)

if __name__ == "__main__":
    asyncio.run(run_all_tests())
