import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from parser import clean_question_text, parse_questions_message

# Test 1: User's exact screenshot sample
sample_1 = "[49/55] 🔥 🅂🄺हाल ही में मरुस्थलीकरण से निपटने के लिए संयुक्त राष्ट्र कन्वेंशन की 17वीं बैठक 'COP17' कहाँ हुई है?\n\n♡◄••───○ ⇣sᴏɴηᴀᴍ⤾○───••► ♡"
res_1 = clean_question_text(sample_1)
print(f"Sample 1 Cleaned Result:\n'{res_1}'\n")
expected_1 = "हाल ही में मरुस्थलीकरण से निपटने के लिए संयुक्त राष्ट्र कन्वेंशन की 17वीं बैठक 'COP17' कहाँ हुई है?"
assert res_1 == expected_1, f"Test 1 Failed! Expected: '{expected_1}', got: '{res_1}'"

# Test 2: Channel handle and index
sample_2 = "🎯 Q.12) भारत की राजधानी क्या है? @mychannel"
res_2 = clean_question_text(sample_2)
print(f"Sample 2 Cleaned Result:\n'{res_2}'\n")
expected_2 = "भारत की राजधानी क्या है?"
assert res_2 == expected_2, f"Test 2 Failed! Expected: '{expected_2}', got: '{res_2}'"

# Test 3: Boxed font & promotional line
sample_3 = "✨ [1/20] 🅖🅚 विश्व पर्यावरण दिवस कब मनाया जाता है?\nJoin: @gkquiz daily"
res_3 = clean_question_text(sample_3)
print(f"Sample 3 Cleaned Result:\n'{res_3}'\n")
expected_3 = "विश्व पर्यावरण दिवस कब मनाया जाता है?"
assert res_3 == expected_3, f"Test 3 Failed! Expected: '{expected_3}', got: '{res_3}'"

# Test 4: Emojis and number prefix
sample_4 = "⚡ 15. उत्तर प्रदेश का राजकीय पशु क्या है? 🌟"
res_4 = clean_question_text(sample_4)
print(f"Sample 4 Cleaned Result:\n'{res_4}'\n")
expected_4 = "उत्तर प्रदेश का राजकीय पशु क्या है?"
assert res_4 == expected_4, f"Test 4 Failed! Expected: '{expected_4}', got: '{res_4}'"

# Test 5: Full question block parse test
block_text = """
[10/50] 🔥 🅂🄺भारत का पहला राष्ट्रीय उद्यान कौन सा है?
♡◄••───○ ⇣sᴏɴηᴀᴍ⤾○───••► ♡
जिम कॉर्बेट राष्ट्रीय उद्यान ✅
काजीरंगा राष्ट्रीय उद्यान
गिर राष्ट्रीय उद्यान
कान्हा राष्ट्रीय उद्यान
Ex: जिम कॉर्बेट नेशनल पार्क उत्तराखंड में स्थित है।
"""
parsed = parse_questions_message(block_text)
assert len(parsed) == 1
assert parsed[0]["question_text"] == "भारत का पहला राष्ट्रीय उद्यान कौन सा है?"
assert parsed[0]["correct_option_id"] == 0
assert parsed[0]["explanation"] == "जिम कॉर्बेट नेशनल पार्क उत्तराखंड में स्थित है।"

# Test 6: Real Telegram Poll sample with multiple consecutive counters & watermark symbols/footer
sample_6 = '[2/11] [2/55] ✱✍️ हाल ही में चर्चा में रही मैगी हेरमेन और जोनाथन स्वान की "Regime Change" पुस्तक का संबंध किससे है?\n\n♡◄••───○ ⇣sᴏɴηᴀᴍ⤾○───••► ♡'
res_6 = clean_question_text(sample_6)
print(f"Sample 6 Cleaned Result:\n'{res_6}'\n")
expected_6 = 'हाल ही में चर्चा में रही मैगी हेरमेन और जोनाथन स्वान की "Regime Change" पुस्तक का संबंध किससे है?'
assert res_6 == expected_6, f"Test 6 Failed!\nExpected: '{expected_6}'\nGot:      '{res_6}'"

# Test 7: Second Real Telegram Poll sample (Ramon Magsaysay 2026 sample)
sample_7 = '[1/11] [1/55] ✱✍️ हाल ही में रेमन मैगसेसे पुरस्कार 2026 के लिए किसे चुना गया है?\n\n♡◄••───○ ⇣sᴏɴηᴀᴍ⤾○───••► ♡'
res_7 = clean_question_text(sample_7)
print(f"Sample 7 Cleaned Result:\n'{res_7}'\n")
expected_7 = 'हाल ही में रेमन मैगसेसे पुरस्कार 2026 के लिए किसे चुना गया है?'
assert res_7 == expected_7, f"Test 7 Failed!\nExpected: '{expected_7}'\nGot:      '{res_7}'"

print("🎉 ALL TESTS PASSED PERFECTLY!")
