import io
from PIL import Image, ImageDraw, ImageFont

def get_font(size: int, bold: bool = False):
    font_names = [
        "arialbd.ttf" if bold else "arial.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "Segoe UI.ttf",
        "FreeSans.ttf"
    ]
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default()
    except Exception:
        return None

def truncate_str(text: str, max_chars: int = 20) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 2] + ".."

def format_time_short(seconds: float) -> str:
    total_sec = int(round(seconds))
    mins = total_sec // 60
    secs = total_sec % 60
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"

def create_medal_icon(rank: int, size: int = 28) -> Image.Image:
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    
    if rank == 1:
        bg_color = (245, 166, 35)   # Gold
        text_color = (255, 255, 255)
    elif rank == 2:
        bg_color = (155, 155, 155) # Silver
        text_color = (255, 255, 255)
    elif rank == 3:
        bg_color = (205, 127, 50)  # Bronze
        text_color = (255, 255, 255)
    else:
        bg_color = (220, 224, 230)
        text_color = (70, 70, 70)

    draw.ellipse([0, 0, size - 1, size - 1], fill=bg_color)
    
    font = get_font(14, bold=True)
    rank_str = str(rank)
    if font:
        try:
            bbox = font.getbbox(rank_str)
            w = bbox[2] - bbox[0]
            h = bbox[3] - bbox[1]
        except Exception:
            w, h = 8, 10
        draw.text(((size - w) / 2, (size - h) / 2 - 2), rank_str, fill=text_color, font=font)
    
    return img

def generate_leaderboard_image(participants: list, quiz_name: str = "Quiz Result", max_rows: int = 15) -> io.BytesIO:
    display_rows = participants[:max_rows]
    row_count = max(len(display_rows), 1)

    card_width = 750
    row_height = 46
    header_height = 48
    top_title_height = 45 if quiz_name else 0
    padding = 15

    card_height = top_title_height + header_height + (row_count * row_height) + (padding * 2) + 15

    canvas_bg = (240, 242, 245)
    image = Image.new("RGB", (card_width, card_height), canvas_bg)
    draw = ImageDraw.Draw(image)

    card_margin = 12
    card_box = [
        card_margin,
        card_margin,
        card_width - card_margin,
        card_height - card_margin
    ]
    card_bg = (255, 255, 255)
    border_color = (218, 224, 233)
    
    try:
        draw.rounded_rectangle(card_box, radius=12, fill=card_bg, outline=border_color, width=2)
    except AttributeError:
        draw.rectangle(card_box, fill=card_bg, outline=border_color, width=2)

    font_title = get_font(20, bold=True)
    font_header = get_font(16, bold=True)
    font_body = get_font(15, bold=False)
    font_bold = get_font(15, bold=True)

    y_offset = card_margin + 8

    # Optional Top Title Row inside Card
    if quiz_name:
        title_text = f"🏆 {truncate_str(quiz_name, 35)}"
        draw.text((card_margin + 20, y_offset + 5), title_text, fill=(30, 40, 55), font=font_title)
        y_offset += top_title_height

    # Header Row Box
    header_box = [
        card_margin + 2,
        y_offset,
        card_width - card_margin - 2,
        y_offset + header_height
    ]
    header_bg = (242, 244, 248)
    try:
        draw.rounded_rectangle(header_box, radius=8, fill=header_bg)
    except AttributeError:
        draw.rectangle(header_box, fill=header_bg)

    # Column Boundaries
    col_x = {
        "rank": card_margin + 15,
        "name": card_margin + 65,
        "correct": card_width - 345,
        "wrong": card_width - 280,
        "score": card_width - 200,
        "time": card_width - 110,
        "pct": card_width - 45
    }

    # Vertical Gridline X positions
    v_lines = [
        card_margin + 55,
        card_width - 355,
        card_width - 290,
        card_width - 215,
        card_width - 120,
        card_width - 55
    ]

    # Draw Header Text
    draw.text((col_x["rank"], y_offset + 13), "#", fill=(70, 80, 95), font=font_header)
    draw.text((col_x["name"], y_offset + 13), "Name", fill=(70, 80, 95), font=font_header)
    draw.text((col_x["correct"], y_offset + 13), "✅", fill=(34, 139, 34), font=font_header)
    draw.text((col_x["wrong"], y_offset + 13), "❌", fill=(220, 20, 60), font=font_header)
    draw.text((col_x["score"], y_offset + 13), "Score", fill=(70, 80, 95), font=font_header)
    draw.text((col_x["time"], y_offset + 13), "Time", fill=(70, 80, 95), font=font_header)
    draw.text((col_x["pct"], y_offset + 13), "%", fill=(70, 80, 95), font=font_header)

    y_start = y_offset + header_height

    # Draw Header Divider
    draw.line([(card_margin + 2, y_start), (card_width - card_margin - 2, y_start)], fill=(220, 226, 235), width=2)

    # Draw Table Rows
    for i, p in enumerate(display_rows, start=1):
        row_y = y_start + ((i - 1) * row_height)
        
        # Zebra Striping
        if i % 2 == 0:
            row_bg = (248, 250, 254)
            draw.rectangle([card_margin + 2, row_y, card_width - card_margin - 2, row_y + row_height], fill=row_bg)

        # Horizontal Gridline
        draw.line(
            [(card_margin + 2, row_y + row_height), (card_width - card_margin - 2, row_y + row_height)],
            fill=(230, 235, 242),
            width=1
        )

        # Vertical Gridlines for row
        for vx in v_lines:
            draw.line([(vx, row_y), (vx, row_y + row_height)], fill=(232, 237, 244), width=1)

        # Rank / Medal
        if i <= 3:
            medal_img = create_medal_icon(i, size=26)
            image.paste(medal_img, (col_x["rank"] - 4, row_y + 10), medal_img)
        else:
            draw.text((col_x["rank"] + 2, row_y + 12), str(i), fill=(100, 110, 125), font=font_bold)

        # Name
        name_str = truncate_str(p.get("name", "User"), max_chars=20)
        draw.text((col_x["name"], row_y + 12), name_str, fill=(30, 35, 45), font=font_body)

        # Correct
        correct_cnt = str(p.get("correct", 0))
        draw.text((col_x["correct"] + 4, row_y + 12), correct_cnt, fill=(46, 125, 50), font=font_bold)

        # Wrong
        wrong_cnt = str(p.get("wrong", 0))
        draw.text((col_x["wrong"] + 4, row_y + 12), wrong_cnt, fill=(198, 40, 40), font=font_bold)

        # Score (formatted float e.g. 33.00)
        score_val = p.get("score", float(p.get("correct", 0)))
        score_str = f"{score_val:.2f}"
        draw.text((col_x["score"], row_y + 12), score_str, fill=(30, 35, 45), font=font_bold)

        # Time (formatted short string e.g. 5m 30s)
        time_str = format_time_short(p.get("total_time", 0.0))
        draw.text((col_x["time"], row_y + 12), time_str, fill=(70, 80, 95), font=font_body)

        # % Accuracy
        accuracy_val = p.get("accuracy", 0.0)
        pct_str = f"{int(round(accuracy_val))}%"
        draw.text((col_x["pct"], row_y + 12), pct_str, fill=(70, 80, 95), font=font_body)

    # Vertical Gridlines in Header
    for vx in v_lines:
        draw.line([(vx, y_offset), (vx, y_offset + header_height)], fill=(225, 230, 238), width=1)

    # Bottom Pill Grab Bar (Aesthetic match)
    pill_w, pill_h = 60, 4
    pill_x = (card_width - pill_w) // 2
    pill_y = card_height - card_margin - 8
    try:
        draw.rounded_rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + pill_h], radius=2, fill=(200, 205, 215))
    except AttributeError:
        draw.rectangle([pill_x, pill_y, pill_x + pill_w, pill_y + pill_h], fill=(200, 205, 215))

    output = io.BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    return output
