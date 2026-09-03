import re

DECORATIVE_SYMBOLS = r'[\U0001F000-\U0001FFFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF\u2500-\u259F\u25A0-\u25FF\u2190-\u21FF\u2900-\u297F\u2E80-\u2FD5\u3000-\u303F\uFE00-\uFE0F\u200D\u200C♡♥★☆◄►▲▼•●○■□◆◇─━—~*_=\-/\\|ツ✿❀⇣⇡⇢⇠⤾⤿▶️📌❓🔹🔸💡🚀💥🌟📢🚨]'
FANCY_FONT_RANGES = r'[\U0001F100-\U0001F1FF\U0001D400-\U0001D7FF\u2460-\u24FF\u1D00-\u1D7F\u0250-\u02AF\u0370-\u03FF]'

PROMO_KEYWORDS = re.compile(
    r'(?:join|credit|by|follow|subscribe|channel|group|telegram|t\.me|http[s]?://|@\w+)',
    re.IGNORECASE
)

def is_promotional_or_decorative_line(line: str) -> bool:
    line_stripped = line.strip()
    if not line_stripped:
        return True

    # If line contains link or telegram handle
    if re.search(r'(?:https?://\S+|t\.me/\S+|telegram\.me/\S+|@\w+)', line_stripped, re.IGNORECASE):
        clean = re.sub(r'(?:https?://\S+|t\.me/\S+|telegram\.me/\S+|@\w+)', '', line_stripped).strip()
        if len(clean) < 15 or PROMO_KEYWORDS.search(line_stripped):
            return True

    # Count decorative symbols and fancy font characters
    total_chars = len(line_stripped)
    decor_count = len(re.findall(f'(?:{DECORATIVE_SYMBOLS}|{FANCY_FONT_RANGES})', line_stripped))
    valid_text_chars = len(re.findall(r'[\u0900-\u097Fa-zA-Z0-9\?\!\,\.\:\;\-\"\'\(\)\/]', line_stripped))

    # If more than 35% of the line is decorative symbols or fancy font
    if total_chars > 0 and (decor_count / total_chars) >= 0.35:
        return True

    # If valid text characters are less than 30% of total chars (for line >= 5 chars)
    if total_chars >= 5 and (valid_text_chars / total_chars) < 0.30:
        return True

    # Short lines with promo keywords
    if PROMO_KEYWORDS.search(line_stripped) and len(line_stripped) <= 60:
        return True

    return False

def clean_question_text(text: str) -> str:
    """
    Cleans question text from forwarded polls and messages by removing:
    - Multiple consecutive leading counters/brackets ([2/11] [2/55], [49/55], Q1., 1., etc.)
    - Leading emojis, symbols, and boxed/squared watermark logos (🔥, 🎯, 🅂🄺, 🅖🅚, ✱✍️, etc.)
    - Bottom promotional lines, channel names, and decorative borders (e.g. ♡◄••───○ ⇣sᴏɴηᴀᴍ⤾○───••► ♡, @channel, links)
    - Extra blank lines and whitespace
    """
    if not text:
        return ""

    lines = [l for l in text.split("\n")]
    cleaned_lines = []

    for line in lines:
        l = line.strip()
        if not l:
            continue
        if is_promotional_or_decorative_line(l):
            continue
        cleaned_lines.append(l)

    if not cleaned_lines:
        cleaned_lines = [text.strip()]

    result = "\n".join(cleaned_lines).strip()

    # Clean trailing promo handles, links, and trailing decorative symbols/emojis
    result = re.sub(r'\s*(?:(?:Join|By|Credit)?\s*@\w+|https?://\S+|t\.me/\S+)\s*$', '', result, flags=re.IGNORECASE).strip()

    suffix_pattern = re.compile(rf'(?:{DECORATIVE_SYMBOLS}|{FANCY_FONT_RANGES}|\s)+$')
    result = suffix_pattern.sub('', result).strip()

    counter_pattern = re.compile(
        rf'^\s*(?:'
        rf'\[\s*\d+\s*/\s*\d+\s*\]|'
        rf'\(\s*\d+\s*/\s*\d+\s*\)|'
        rf'\[\s*\d+\s*\]|'
        rf'Q(?:uestion)?\s*[\.\:\-]?\s*\d+\s*[\.\:\-\)]?|'
        rf'(?:प्रश्न|प्र)\s*[\.\:\-]?\s*\d+\s*[\.\:\-\)]?|'
        rf'\d+\s*[\.\:\-\)]'
        rf')\s*',
        re.IGNORECASE
    )

    symbol_prefix_pattern = re.compile(
        rf'^\s*(?:{DECORATIVE_SYMBOLS}|{FANCY_FONT_RANGES}|\s)+\s*'
    )

    prev = None
    while result != prev:
        prev = result
        # 1. Strip leading counters
        result = counter_pattern.sub('', result).strip()
        # 2. Strip leading symbols/emojis
        result = symbol_prefix_pattern.sub('', result).strip()

    # Re-clean trailing symbols in case stripping prefix revealed trailing emojis
    result = suffix_pattern.sub('', result).strip()

    # Normalize multiple spaces
    result = re.sub(r'[ \t]+', ' ', result).strip()

    return result

def parse_single_question_block(block_text: str):
    """
    Parses a single block of text containing question lines followed by options and optional Ex/Explanation lines.
    Returns dict: {"question_text": str, "options": list, "correct_option_id": int, "explanation": str} or None if invalid.
    """
    raw_lines = [line.strip() for line in block_text.strip().split("\n") if line.strip()]
    if len(raw_lines) < 3:
        return None

    # Filter out standalone promotional/decorative lines (watermarks, borders, channel handles)
    filtered_lines = []
    for line in raw_lines:
        if is_promotional_or_decorative_line(line):
            continue
        filtered_lines.append(line)
    
    raw_lines = filtered_lines
    if len(raw_lines) < 3:
        return None

    # Extract explanation if present (Ex:, EX:, ex:, Explanation:, explanation:, व्याख्या:)
    explanation = ""
    ex_idx = -1
    ex_regex = re.compile(r'^(?:Ex|EX|ex|Explanation|explanation|व्याख्या)\s*[:\-]\s*', re.IGNORECASE)

    for idx, line in enumerate(raw_lines):
        if ex_regex.match(line):
            ex_idx = idx
            break

    if ex_idx != -1:
        ex_lines = raw_lines[ex_idx:]
        ex_text = "\n".join(ex_lines).strip()
        explanation = ex_regex.sub('', ex_text).strip()
        raw_lines = raw_lines[:ex_idx]

    if len(raw_lines) < 3:
        return None

    correct_line_idx = -1
    for i, line in enumerate(raw_lines):
        if "✅" in line:
            if correct_line_idx != -1:
                return None
            correct_line_idx = i

    if correct_line_idx == -1:
        return None

    option_prefix_regex = re.compile(r'^(?:[A-Za-z0-9][\.\)\:]|[\(\[\{][A-Za-z0-9][\)\]\}]|[\u25cb\u2022\u25cf\u25b6\U0001f170-\U0001f189])\s*')

    opt_start_idx = -1
    for i in range(correct_line_idx, 0, -1):
        line_clean = raw_lines[i].replace("✅", "").strip()
        if option_prefix_regex.match(line_clean):
            opt_start_idx = i
        else:
            if opt_start_idx != -1:
                break

    if opt_start_idx == -1 or opt_start_idx > correct_line_idx:
        possible_start = max(1, correct_line_idx - 3)
        opt_start_idx = possible_start

    question_lines = raw_lines[:opt_start_idx]
    raw_options = raw_lines[opt_start_idx:]

    if not question_lines or len(raw_options) < 2 or len(raw_options) > 10:
        return None

    question_text = clean_question_text("\n".join(question_lines).strip())
    if not question_text:
        return None

    correct_index = -1
    clean_options = []

    for idx, opt in enumerate(raw_options):
        is_correct = False
        if "✅" in opt:
            is_correct = True
            opt = opt.replace("✅", "").strip()

        opt_clean = opt.strip()
        if not opt_clean:
            return None
        clean_options.append(opt_clean)
        if is_correct:
            correct_index = idx

    if correct_index == -1:
        return None

    res = {
        "question_text": question_text,
        "options": clean_options,
        "correct_option_id": correct_index
    }
    if explanation:
        res["explanation"] = explanation[:200]

    return res

def parse_questions_message(text: str):
    """
    Parses input message which may contain one or multiple question blocks.
    Returns list of question dicts.
    """
    parsed_questions = []
    
    blocks = re.split(r'\n\s*\n+', text.strip())

    for block in blocks:
        q = parse_single_question_block(block)
        if q:
            parsed_questions.append(q)

    if not parsed_questions:
        q = parse_single_question_block(text)
        if q:
            parsed_questions.append(q)

    return parsed_questions
