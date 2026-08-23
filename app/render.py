"""
وحدة توليد فيديو عمودي (9:16) لآية قرآنية:
- الآيات القصيرة/المتوسطة: تُرسم بخط كبير وتبقى ثابتة في وسط الشاشة.
- الآيات الطويلة: تُرسم بخط مريح للقراءة وتُمرَّر (Scroll) بسلاسة متزامنة مع مدة التلاوة،
  بدل تصغير الخط بشكل مبالغ فيه أو قصّ النص.

الطبقات:
  1) خلفية متدرجة (ffmpeg lavfi)
  2) طبقة ثابتة: اسم السورة ورقم الآية (أعلى الشاشة)
  3) طبقة نص الآية: ثابتة أو متحركة حسب الطول
  4) الصوت (تلاوة)
"""
import os
import random
import subprocess
import tempfile
import uuid

from PIL import Image, ImageDraw, ImageFont

RAQM_LAYOUT = ImageFont.Layout.RAQM

WIDTH, HEIGHT = 1080, 1920

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT_AYAH = os.path.join(BASE_DIR, "fonts", "AmiriQuran-Regular.ttf")
FONT_UI = os.path.join(BASE_DIR, "fonts", "NotoKufiArabic.ttf")

# مجموعة لوحات ألوان (خلفية علوية/سفلية + لون العنوان) — تُختار عشوائيًا كل مرة
# حتى ما تكون كل الفيديوهات متطابقة بصريًا. الألوان مختارة لتبقى هادئة ومناسبة
# لمحتوى قرآني (لا صور/فيديوهات خارجية، فقط تدرجات تركيبية بدون أي مشاكل حقوق).
# كل لوحة لها اسم صريح يفيد باختيارها يدويًا من Google Sheet / Make (عبر رقمها 0-5).
PALETTE_NAMES = ["أخضر داكن", "كحلي بنفسجي", "بني دافئ", "تركوازي", "عنابي", "أزرق مخضر"]

PALETTES = [
    {"top": "0x0b2e28", "bottom": "0x08151a", "accent": (212, 175, 55)},   # 0: أخضر داكن + ذهبي
    {"top": "0x1a2a4a", "bottom": "0x0a0e1a", "accent": (201, 162, 255)},  # 1: كحلي + بنفسجي فاتح
    {"top": "0x2e1a0b", "bottom": "0x140a05", "accent": (230, 190, 120)},  # 2: بني دافئ + كريمي
    {"top": "0x0d2b3e", "bottom": "0x061119", "accent": (120, 200, 210)},  # 3: تركوازي داكن + سماوي
    {"top": "0x2a0b2e", "bottom": "0x0f0514", "accent": (220, 150, 190)},  # 4: عنابي داكن + وردي فاتح
    {"top": "0x0b1f2e", "bottom": "0x05090f", "accent": (170, 210, 160)},  # 5: كحلي مزرق + أخضر فاتح
]

# اتجاهات تدرج متنوعة (من نقطة إلى نقطة داخل الإطار) — عشان التدرج نفسه يختلف شكله
GRADIENT_DIRECTIONS = [
    lambda w, h: (w // 2, 0, w // 2, h),        # من فوق لتحت
    lambda w, h: (0, 0, w, h),                  # قطري: يسار-فوق إلى يمين-تحت
    lambda w, h: (w, 0, 0, h),                  # قطري: يمين-فوق إلى يسار-تحت
    lambda w, h: (0, h // 2, w, h // 2),        # من يسار ليمين
]

# منطقة عرض نص الآية (بين شريط العنوان وهامش الأسفل)
REGION_TOP = 300
REGION_BOTTOM = HEIGHT - 140
REGION_HEIGHT = REGION_BOTTOM - REGION_TOP
LINE_MARGIN_X = 90
MAX_TEXT_WIDTH = WIDTH - 2 * LINE_MARGIN_X


def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size, layout_engine=RAQM_LAYOUT)


def _text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> int:
    bbox = draw.textbbox((0, 0), text, font=font, direction="rtl")
    return bbox[2] - bbox[0]


def _wrap_text(draw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join(current + [word])
        if _text_width(draw, candidate, font) <= max_width or not current:
            current.append(word)
        else:
            lines.append(" ".join(current))
            current = [word]
    if current:
        lines.append(" ".join(current))
    return lines


def _font_size_for_length(char_count: int) -> int:
    """يختار حجم خط مناسب حسب طول الآية، حتى تبقى القراءة مريحة دائمًا
    (بدل تصغير الخط بشكل عشوائي لين "يعصر" النص بالشاشة)."""
    if char_count <= 60:
        return 118
    elif char_count <= 130:
        return 92
    elif char_count <= 260:
        return 74
    elif char_count <= 450:
        return 62
    else:
        return 54


def choose_palette(index: int | None = None) -> dict:
    """
    يختار لوحة ألوان: رقم محدد (0-5) إذا انطلب، وإلا عشوائيًا.
    اتجاه التدرج دايمًا عشوائي (حتى لو نفس اللوحة، يختلف شكلها شوي كل مرة).
    """
    if index is not None:
        palette = dict(PALETTES[index % len(PALETTES)])
    else:
        palette = dict(random.choice(PALETTES))
    palette["direction"] = random.choice(GRADIENT_DIRECTIONS)
    return palette


def build_label_overlay(surah_label: str, accent: tuple[int, int, int] = (212, 175, 55)) -> str:
    """طبقة ثابتة فيها اسم السورة ورقم الآية فقط (أعلى الشاشة)، لا تتحرك أبدًا."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(FONT_UI, 46)
    w = _text_width(draw, surah_label, font)
    fill = (accent[0], accent[1], accent[2], 255)
    draw.text(((WIDTH - w) // 2, 140), surah_label, font=font, fill=fill, direction="rtl")

    out_path = os.path.join(tempfile.gettempdir(), f"label_{uuid.uuid4().hex}.png")
    img.save(out_path)
    return out_path


def build_ayah_overlay(ayah_text: str) -> tuple[str, int, bool]:
    """
    يرسم نص الآية كاملاً بدون قصّ. يرجع:
      (مسار الصورة، ارتفاعها الفعلي، هل تحتاج تمرير Scroll)
    - آية قصيرة/متوسطة: ترسم بخط كبير وترجع needs_scroll=False (تعرض ثابتة بمنتصف الشاشة).
    - آية طويلة جدًا: ترسم بخط مريح على كانفس أطول من الشاشة، وترجع needs_scroll=True.
    """
    font_size = _font_size_for_length(len(ayah_text))
    dummy = Image.new("RGBA", (10, 10))
    draw_probe = ImageDraw.Draw(dummy)
    font = _load_font(FONT_AYAH, font_size)
    lines = _wrap_text(draw_probe, ayah_text, font, MAX_TEXT_WIDTH)

    line_height = int(font_size * 1.7)
    top_pad, bottom_pad = 20, 20
    content_height = line_height * len(lines) + top_pad + bottom_pad

    needs_scroll = content_height > REGION_HEIGHT
    canvas_height = max(content_height, REGION_HEIGHT)

    img = Image.new("RGBA", (WIDTH, canvas_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # إذا ما احتاج تمرير: نتوسط النص عموديًا داخل الكانفس (المساوي لارتفاع المنطقة المتاحة)
    start_y = top_pad if needs_scroll else (canvas_height - content_height) // 2 + top_pad

    for i, line in enumerate(lines):
        w = _text_width(draw, line, font)
        x = (WIDTH - w) // 2
        y = start_y + i * line_height
        draw.text((x + 3, y + 3), line, font=font, fill=(0, 0, 0, 130), direction="rtl")
        draw.text((x, y), line, font=font, fill=(255, 250, 235, 255), direction="rtl")

    out_path = os.path.join(tempfile.gettempdir(), f"ayah_{uuid.uuid4().hex}.png")
    img.save(out_path)
    return out_path, canvas_height, needs_scroll


def get_audio_duration(audio_path: str) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def render_video(label_png: str, ayah_png: str, ayah_height: int, needs_scroll: bool,
                  audio_path: str, output_path: str, palette: dict | None = None) -> None:
    """يدمج: خلفية متدرجة (تختلف ألوانها واتجاهها كل مرة) + طبقة العنوان + طبقة الآية + الصوت كاملاً."""
    if palette is None:
        palette = choose_palette()

    duration = get_audio_duration(audio_path)
    total_duration = duration + 0.8  # هامش بسيط بالنهاية حتى ما ينقطع الصوت فجأة

    x0, y0, x1, y1 = palette["direction"](WIDTH, HEIGHT)
    gradient_filter = (
        f"gradients=s={WIDTH}x{HEIGHT}:c0={palette['top']}:c1={palette['bottom']}:"
        f"x0={x0}:y0={y0}:x1={x1}:y1={y1}:duration={total_duration}:rate=30"
    )

    if needs_scroll:
        crop_y_max = ayah_height - REGION_HEIGHT
        scroll_span = f"max({duration}-1.0,0.1)"
        # نافذة القصّ (Crop) تتحرك عبر الصورة الطويلة بمرور الوقت، فتُظهر فقط
        # الجزء الحالي من النص. هذا يضمن إن النص المتحرك لا يتجاوز أبدًا حدود
        # منطقته المخصصة (لا يتراكب مع شريط العنوان فوق ولا مع الهامش تحت).
        crop_y_expr = f"{crop_y_max}*min(max((t-0.5)/{scroll_span},0),1)"
    else:
        crop_y_expr = "0"

    filter_complex = (
        f"[0:v][1:v]overlay=x=0:y=0[bg1];"
        f"[2:v]crop=w={WIDTH}:h={REGION_HEIGHT}:x=0:y='{crop_y_expr}'[ayahc];"
        f"[bg1][ayahc]overlay=x=0:y={REGION_TOP}:format=auto[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", gradient_filter,
        "-loop", "1", "-i", label_png,
        "-loop", "1", "-i", ayah_png,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "3:a",
        "-t", str(total_duration),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"فشل ffmpeg:\n{result.stderr.decode(errors='ignore')[-2000:]}")
