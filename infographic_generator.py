"""
信息图自动生成器
根据中文脚本内容，自动生成多张信息图（Infographic）
每张图对应脚本中的一个段落，用于白板动画输入
"""

import os
import re
import math
import textwrap
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None


# ============ 配色方案 ============
THEMES = {
    "default": {
        "bg_top": (19, 27, 46),       # #131b2e
        "bg_bottom": (10, 18, 35),     # #0a1223
        "accent": (102, 126, 234),     # #667eea
        "accent2": (118, 75, 162),     # #764ba2
        "text_main": (255, 255, 255),
        "text_sub": (180, 190, 210),
        "text_muted": (130, 140, 165),
        "card_bg": (30, 42, 70),
        "card_border": (50, 65, 100),
        "icon_colors": [
            (102, 126, 234),   # 蓝
            (72, 199, 142),    # 绿
            (245, 158, 11),    # 橙
            (239, 68, 68),     # 红
            (168, 85, 247),    # 紫
        ],
    },
    "nutrition": {
        "bg_top": (13, 43, 43),
        "bg_bottom": (8, 30, 30),
        "accent": (72, 199, 142),
        "accent2": (52, 160, 120),
        "text_main": (255, 255, 255),
        "text_sub": (170, 210, 190),
        "text_muted": (120, 160, 140),
        "card_bg": (20, 55, 50),
        "card_border": (40, 80, 70),
        "icon_colors": [
            (72, 199, 142),
            (250, 204, 21),
            (239, 68, 68),
            (59, 130, 246),
        ],
    },
    "exercise": {
        "bg_top": (26, 15, 46),
        "bg_bottom": (16, 8, 32),
        "accent": (139, 92, 246),
        "accent2": (99, 102, 241),
        "text_main": (255, 255, 255),
        "text_sub": (200, 190, 220),
        "text_muted": (150, 140, 175),
        "card_bg": (35, 22, 58),
        "card_border": (60, 40, 90),
        "icon_colors": [
            (139, 92, 246),
            (59, 130, 246),
            (16, 185, 129),
            (245, 158, 11),
        ],
    },
    "mental": {
        "bg_top": (30, 35, 48),
        "bg_bottom": (20, 24, 36),
        "accent": (99, 179, 237),
        "accent2": (130, 170, 220),
        "text_main": (255, 255, 255),
        "text_sub": (190, 200, 220),
        "text_muted": (140, 150, 175),
        "card_bg": (38, 44, 60),
        "card_border": (55, 62, 82),
        "icon_colors": [
            (99, 179, 237),
            (129, 199, 132),
            (255, 183, 77),
            (186, 104, 200),
        ],
    },
}

CANVAS_W = 1920
CANVAS_H = 1080
FONT_DIR = "/usr/share/fonts"

# 已知支持中文的字体路径
CJK_FONTS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/opentype/smiley-sans/SmileySans-Oblique.otf",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]
CJK_FONTS_BOLD = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
]


def _get_font(size, bold=False):
    """获取支持中文的字体"""
    candidates = CJK_FONTS_BOLD if bold else CJK_FONTS
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    # fallback: 任意 ttf
    for root, dirs, files in os.walk(FONT_DIR):
        for f in files:
            if f.endswith(".ttf") or f.endswith(".ttc") or f.endswith(".otf"):
                path = os.path.join(root, f)
                try:
                    return ImageFont.truetype(path, size)
                except:
                    continue
    return ImageFont.load_default()


def _draw_gradient_bg(draw, theme):
    """绘制渐变背景"""
    bg_top = theme["bg_top"]
    bg_bottom = theme["bg_bottom"]

    for y in range(CANVAS_H):
        ratio = y / CANVAS_H
        r = int(bg_top[0] * (1 - ratio) + bg_bottom[0] * ratio)
        g = int(bg_top[1] * (1 - ratio) + bg_bottom[1] * ratio)
        b = int(bg_top[2] * (1 - ratio) + bg_bottom[2] * ratio)
        draw.line([(0, y), (CANVAS_W, y)], fill=(r, g, b))

    # 装饰性左上光晕
    for r in range(300, 0, -3):
        alpha = max(0, 30 - (300 - r) // 10)
        draw.ellipse(
            [-r, -r, r, r],
            fill=(theme["accent"][0], theme["accent"][1], theme["accent"][2], alpha),
        )


def _draw_card(draw, x, y, w, h, theme, number=None, title="", body="", icon_color=None):
    """绘制一个卡片"""
    # 卡片背景
    draw.rounded_rectangle(
        [x, y, x + w, y + h],
        radius=16,
        fill=theme["card_bg"] + (240,),
        outline=theme["card_border"],
        width=1,
    )

    if number is not None:
        # 编号圆
        color = icon_color or theme["accent"]
        cx = x + 35
        cy = y + 35
        r = 18
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        font_num = _get_font(20, bold=True)
        num_text = str(number)
        bbox = draw.textbbox((0, 0), num_text, font=font_num)
        tx = cx - (bbox[2] - bbox[0]) // 2
        ty = cy - (bbox[3] - bbox[1]) // 2 - 2
        draw.text((tx, ty), num_text, fill=(255, 255, 255), font=font_num)

    # 标题
    if title:
        font_title = _get_font(22, bold=True)
        title_x = x + 35
        title_y = y + 55 if number is not None else y + 20
        # 自动换行
        for line in textwrap.wrap(title, width=20):
            draw.text((title_x, title_y), line, fill=theme["text_main"], font=font_title)
            title_y += 30

    # 正文
    if body:
        font_body = _get_font(18)
        body_x = x + 35
        body_y = y + h - 60
        # 最多两行
        lines = textwrap.wrap(body, width=28)[:2]
        for line in lines:
            draw.text((body_x, body_y), line, fill=theme["text_sub"], font=font_body)
            body_y += 25


def _draw_simple_icon(draw, cx, cy, icon_type, color, size=30):
    """绘制简单图标"""
    half = size // 2
    if icon_type == "bulb":  # 灯泡
        draw.ellipse([cx - half, cy - half + 5, cx + half, cy + half], fill=color)
        draw.rectangle([cx - 3, cy + half, cx + 3, cy + half + 10], fill=color)
    elif icon_type == "heart":  # 心形
        points = [(cx, cy + 5), (cx - half, cy - half), (cx, cy), (cx + half, cy - half)]
        draw.polygon(points, fill=color)
        draw.ellipse([cx - half, cy - half, cx, cy], fill=color)
        draw.ellipse([cx, cy - half, cx + half, cy], fill=color)
    elif icon_type == "check":  # 对勾
        draw.line([(cx - half, cy), (cx - 5, cy + half), (cx + half, cy - half)],
                   fill=color, width=4)
    elif icon_type == "circle":
        draw.ellipse([cx - half, cy - half, cx + half, cy + half], fill=color)
    elif icon_type == "dumbbell":
        draw.rectangle([cx - half, cy - 5, cx + half, cy + 5], fill=color)
        draw.ellipse([cx - half - 8, cy - 12, cx - half + 8, cy + 12], fill=color)
        draw.ellipse([cx + half - 8, cy - 12, cx + half + 8, cy + 12], fill=color)
    elif icon_type == "moon":
        draw.ellipse([cx - half, cy - half, cx + half, cy + half], fill=color)
        draw.ellipse([cx - half + 8, cy - half - 5, cx + half + 8, cy + half + 5],
                      fill=theme["bg_top"])
    elif icon_type == "clock":
        draw.ellipse([cx - half, cy - half, cx + half, cy + half], fill=None, outline=color, width=3)
        draw.line([(cx, cy), (cx, cy - half + 5)], fill=color, width=3)
        draw.line([(cx, cy), (cx + half - 8, cy)], fill=color, width=3)
    elif icon_type == "food":
        draw.ellipse([cx - half, cy - half, cx + half, cy + half], fill=color)
        draw.ellipse([cx - 5, cy - 10, cx + 5, cy + 5], fill=(255, 255, 255))
    elif icon_type == "brain":
        draw.ellipse([cx - half, cy - half + 5, cx + half, cy + half], fill=color)
        # 脑纹路
        draw.arc([cx - half + 5, cy - half + 5, cx + half - 5, cy + half - 5],
                  0, 180, fill=(255, 255, 255), width=2)
    else:  # default: circle with number
        draw.ellipse([cx - half, cy - half, cx + half, cy + half], fill=color)


def paragraph_duration_sec(text):
    """估算段落配音时长（秒）- 基于真实语速"""
    # 去掉标记行
    clean = re.sub(r'[【\[]\s*.*?\s*[】\]]', '', text)
    clean = re.sub(r'免责声明.*', '', clean, flags=re.DOTALL)
    clean = clean.strip()
    # 中文语速: 约4字/秒(正常) ~ 5字/秒(较快)
    # 加上段落间停顿
    char_count = len(clean)
    duration = max(char_count / 4.5, 15)  # 最少15秒
    return min(duration, 150)  # 最长150秒


def parse_script_sections(script_text):
    """
    解析脚本文本，提取段落和对应时长
    返回: [(title, body, duration_ms), ...]
    """
    lines = script_text.strip().split("\n")
    sections = []
    current_title = "开篇"
    current_body = []
    disclaimer_encountered = False  # 是否遇到免责声明

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 忽略纯标记行
        if line.startswith("---") or line.startswith("【注意】"):
            continue

        # 忽略免责声明段 - 遇到即停止解析
        if "免责声明" in line or "本视频内容仅供" in line:
            # 先保存当前段落（免责声明之前的段落）
            if current_body:
                body_text = "\n".join(current_body)
                dur = paragraph_duration_sec(body_text)
                sections.append((current_title, body_text, int(dur * 1000)))
                current_body = []
            disclaimer_encountered = True
            break

        # 匹配段落标题: [xxx] 或 【xxx】
        match = re.match(r'^[\[\【]\s*(.*?)(?:[-–—]\s*\d+秒)?\s*[\]】]', line)
        if match:
            if current_body:
                body_text = "\n".join(current_body)
                dur = paragraph_duration_sec(body_text)
                sections.append((current_title, body_text, int(dur * 1000)))
            current_title = match.group(1).strip()
            current_body = []
            # 忽略纯标记行
            continue

        # 忽略免责声明段
        if "免责声明" in line:
            break

        # 忽略纯标记行
        if line.startswith("---"):
            continue
        if "注意" in line and "以上为模板" in line:
            break

        current_body.append(line)

    # 最后一段（for 循环正常结束/被免责声明中断，都需要保存当前段落）
    if current_body and not disclaimer_encountered:
        body_text = "\n".join(current_body)
        dur = paragraph_duration_sec(body_text)
        sections.append((current_title, body_text, int(dur * 1000)))

    # 如果段落太少，补一个结尾段
    if len(sections) < 2:
        sections.append(("总结", "以上就是今天分享的内容，希望对你有帮助。", 8000))

    return sections


def generate_infographic(title, points, theme_name="default", output_path="output.png"):
    """
    生成一张信息图
    title: 大标题 (str)
    points: [(number, short_title, description, icon_type), ...]
    output_path: 保存路径
    """
    if Image is None:
        raise ImportError("Pillow (PIL) 未安装: pip install Pillow")

    theme = THEMES.get(theme_name, THEMES["default"])

    # 创建画布
    img = Image.new("RGB", (CANVAS_W, CANVAS_H), theme["bg_top"])
    draw = ImageDraw.Draw(img, "RGBA")

    # 渐变背景
    _draw_gradient_bg(draw, theme)

    # 大标题
    font_title = _get_font(52, bold=True)
    title_lines = textwrap.wrap(title, width=22)
    y_title = 80
    for line in title_lines:
        bbox = draw.textbbox((0, 0), line, font=font_title)
        tx = (CANVAS_W - (bbox[2] - bbox[0])) // 2
        draw.text((tx, y_title), line, fill=theme["text_main"], font=font_title)
        y_title += 60

    # 副标题装饰线
    line_y = y_title + 10
    draw.line([(CANVAS_W // 2 - 60, line_y), (CANVAS_W // 2 + 60, line_y)],
               fill=theme["accent"], width=3)

    # 如果点数为空，显示居中提示
    if not points:
        font_body = _get_font(24)
        msg = "根据脚本自动生成..."
        bbox = draw.textbbox((0, 0), msg, font=font_body)
        draw.text(((CANVAS_W - (bbox[2] - bbox[0])) // 2, CANVAS_H // 2),
                   msg, fill=theme["text_sub"], font=font_body)
        img.save(output_path, quality=95)
        return output_path

    # 根据点数选择布局
    n = len(points)
    if n <= 3:
        # 横向3列
        card_w = 500
        card_h = 480
        spacing = (CANVAS_W - n * card_w) // (n + 1)
        start_x = spacing
        y_start = max(y_title + 40, 200)
    elif n <= 4:
        # 2x2 网格
        card_w = 550
        card_h = 350
        cols = 2
        rows = math.ceil(n / cols)
        spacing_x = (CANVAS_W - cols * card_w) // (cols + 1)
        spacing_y = 40
        total_h = rows * card_h + (rows - 1) * spacing_y
        y_start = max((CANVAS_H - total_h) // 2, 200)
        start_x = spacing_x
    else:
        # 3行
        card_w = 540
        card_h = 300
        spacing_x = (CANVAS_W - 2 * card_w) // 3
        spacing_y = 30
        cols = 2
        rows = math.ceil(n / cols)
        total_h = rows * card_h + (rows - 1) * spacing_y
        y_start = max((CANVAS_H - total_h) // 2, 200)
        start_x = spacing_x

    for i, (num, short_title, desc, icon) in enumerate(points):
        if n <= 3:
            col = i
            row = 0
        else:
            col = i % cols
            row = i // cols

        if n <= 3:
            x = start_x + col * (card_w + spacing)
            y = y_start
        else:
            x = start_x + col * (card_w + spacing_x)
            y = y_start + row * (card_h + spacing_y)

        icon_color = theme["icon_colors"][i % len(theme["icon_colors"])]
        _draw_card(draw, x, y, card_w, card_h, theme,
                   number=num, title=short_title, body=desc,
                   icon_color=icon_color)

    # 底部免责声明
    font_disclaimer = _get_font(16)
    disclaimer = "本内容仅供健康科普参考，不构成医疗建议"
    bbox = draw.textbbox((0, 0), disclaimer, font=font_disclaimer)
    draw.text(
        ((CANVAS_W - (bbox[2] - bbox[0])) // 2, CANVAS_H - 40),
        disclaimer,
        fill=theme["text_muted"],
        font=font_disclaimer,
    )

    # 保存
    img.save(output_path, quality=95)
    return output_path


def generate_infographics_from_script(script_text, output_dir, theme_name="default", video_title=""):
    """
    根据脚本生成多张信息图
    返回: [(image_path, duration_ms), ...]
    """
    import os
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    sections = parse_script_sections(script_text)
    if not sections:
        print("[WARN] 脚本段落解析为空，生成默认信息图")
        sections = [("内容", script_text[:200], 10000)]

    print(f"[INFO] 解析到 {len(sections)} 个段落:")
    for title, body, dur in sections:
        print(f"   - {title}: {len(body)}字 / {dur/1000:.0f}秒")

    # 为每个段落生成信息图
    results = []
    icon_types = ["bulb", "heart", "check", "circle", "dumbbell", "moon", "clock", "brain", "food"]

    for idx, (title, body, duration_ms) in enumerate(sections):
        # 从正文提取要点
        body_sentences = [s.strip() for s in re.split(r'[。！？!?]', body) if len(s.strip()) > 5]

        # 构建 points
        points = []
        for i, sent in enumerate(body_sentences[:5]):  # 最多5个点
            short = sent[:25] + "..." if len(sent) > 25 else sent
            icon = icon_types[i % len(icon_types)]
            points.append((i + 1, f"要点{i+1}", short, icon))

        if not points:
            points = [(1, title, body[:30], "circle")]

        # 段落标题做信息图大标题
        section_title = title
        if "开头" in title or "开篇" in title:
            section_title = video_title[:40] if video_title else "健康科普"
        elif "要点" in title:
            section_title = f"{video_title[:20]} - {title}" if video_title else title
        elif "结尾" in title or "总结" in title:
            section_title = "总结与建议"

        output_path = f"{output_dir}/section_{idx+1:02d}.png"
        generate_infographic(section_title, points, theme_name, output_path)
        results.append((output_path, duration_ms))

        print(f"[OK]  信息图 {idx+1}: {output_path} ({duration_ms/1000:.0f}秒)")

    return results


if __name__ == "__main__":
    # 测试
    test_script = """【开头 - 45秒】
大家好，今天我们来聊一个和睡眠有关的话题。你知道吗，全球有超过30%的人正在经历睡眠问题。

【要点1 - 90秒】
科学研究表明，睡眠不足会直接影响我们的免疫系统。哈佛大学的一项研究发现，每天睡足7-8小时的人，感冒概率降低了4倍。

【要点2 - 90秒】
那么如何改善睡眠质量呢？第一，保持固定的作息时间。第二，睡前1小时远离电子屏幕。第三，避免睡前摄入咖啡因。

【结尾 - 30秒】
总结一下，好睡眠其实不需要复杂的技巧，关键在于坚持好的习惯。从今晚开始，试试放下手机，固定作息吧。

【免责声明】
本视频内容仅供健康科普参考"""

    out = generate_infographics_from_script(test_script, "./test_infographics", "default", "改善睡眠质量的三个方法")
    print(f"\n生成 {len(out)} 张信息图:")
    for path, dur in out:
        print(f"  {path} ({dur/1000:.0f}秒)")