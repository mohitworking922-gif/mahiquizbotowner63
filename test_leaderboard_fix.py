import asyncio
from unittest.mock import AsyncMock, MagicMock
import quiz_bot

async def verify_leaderboard():
    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    # 1. Test 36-question quiz with the user's specific examples
    session_36 = {
        "name": "36-Question General Quiz",
        "total_questions": 36,
        "quiz_id": None,
        "participants": {
            101: {
                "user_id": 101,
                "name": "User 25",
                "username": "user25",
                "correct": 25,
                "wrong": 0,
                "attempted_set": set(range(36)),
                "total_time": 330.0
            },
            102: {
                "user_id": 102,
                "name": "User 23",
                "username": "user23",
                "correct": 23,
                "wrong": 0,
                "attempted_set": set(range(36)),
                "total_time": 355.0
            }
        }
    }

    await quiz_bot.send_quiz_leaderboard(mock_bot, -1001, session_36)

    # Check send_message calls
    sent_text = mock_bot.send_message.call_args[1]["text"]
    print("--- GENERATED LEADERBOARD OUTPUT (36 Questions) ---")
    print(sent_text)
    print("---------------------------------------------------")

    # Assertions for User 25
    assert "✅ 25 | ❌ 11" in sent_text, "User 25: Correct/Wrong mismatch!"
    assert "📊 69.4% | 🚀 69.4%" in sent_text, "User 25: Percentage/Accuracy mismatch!"

    # Assertions for User 23
    assert "✅ 23 | ❌ 13" in sent_text, "User 23: Correct/Wrong mismatch!"
    assert "📊 63.9% | 🚀 63.9%" in sent_text, "User 23: Percentage/Accuracy mismatch!"

    print("✅ 36-Question Leaderboard Verification PASSED!")

    # 2. Test Dynamic Question Count (e.g. 50 questions)
    mock_bot.send_message.reset_mock()
    session_50 = {
        "name": "50-Question Test Quiz",
        "total_questions": 50,
        "quiz_id": None,
        "participants": {
            201: {
                "user_id": 201,
                "name": "User 40",
                "username": "user40",
                "correct": 40,
                "wrong": 0,
                "attempted_set": set(range(50)),
                "total_time": 200.0
            }
        }
    }
    await quiz_bot.send_quiz_leaderboard(mock_bot, -1002, session_50)
    sent_text_50 = mock_bot.send_message.call_args[1]["text"]
    print("\n--- GENERATED LEADERBOARD OUTPUT (50 Questions) ---")
    print(sent_text_50)
    print("---------------------------------------------------")

    assert "✅ 40 | ❌ 10" in sent_text_50, "User 40: Correct/Wrong mismatch!"
    assert "📊 80.0% | 🚀 80.0%" in sent_text_50, "User 40: Percentage/Accuracy mismatch!"

    print("✅ Dynamic Total Questions (50 Qs) Verification PASSED!")

if __name__ == "__main__":
    asyncio.run(verify_leaderboard())
