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

WIDTH, HEIGHT = 720, 1280  # 9:16 — أنزلناها من 1080×1920 لتقليل استهلاك الذاكرة
# (كل الأبعاد والخطوط بالأسفل محسوبة بنفس النسب على الحجم الجديد)

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

# اتجاهات تدرج متنوعة — أسماء بسيطة تُستخدم لرسم الخلفية كصورة PIL ثابتة
# (بدل حسابها رياضيًا لكل فريم بالفيديو عبر ffmpeg، وهذا يوفر ذاكرة كبيرة
# على استضافات محدودة الموارد زي الخطة المجانية من Render).
GRADIENT_DIRECTION_NAMES = ["vertical", "diag_tlbr", "diag_trbl", "horizontal"]

# منطقة عرض نص الآية (بين شريط العنوان وهامش الأسفل)
REGION_TOP = 200
REGION_BOTTOM = HEIGHT - 93
REGION_HEIGHT = REGION_BOTTOM - REGION_TOP
LINE_MARGIN_X = 60
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
        return 79
    elif char_count <= 130:
        return 61
    elif char_count <= 260:
        return 49
    elif char_count <= 450:
        return 41
    else:
        return 36


def choose_palette(index: int | None = None) -> dict:
    """
    يختار لوحة ألوان: رقم محدد (0-5) إذا انطلب، وإلا عشوائيًا.
    اتجاه التدرج دايمًا عشوائي (حتى لو نفس اللوحة، يختلف شكلها شوي كل مرة).
    """
    if index is not None:
        palette = dict(PALETTES[index % len(PALETTES)])
    else:
        palette = dict(random.choice(PALETTES))
    palette["direction_name"] = random.choice(GRADIENT_DIRECTION_NAMES)
    return palette


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.replace("0x", "")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def build_gradient_background(palette: dict) -> str:
    """
    يرسم خلفية متدرجة كصورة PNG ثابتة (مرة وحدة بس، رخيصة على الذاكرة):
    نرسم رقعة صغيرة (64×64) فيها التدرج بالاتجاه المطلوب، ثم نكبّرها بسلاسة
    لحجم الفيديو الكامل. هذا أخف بكثير من حساب التدرج رياضيًا لكل فريم بالفيديو
    (اللي كان يستهلك ذاكرة زايدة وتسبب بتوقف الخدمة على استضافة محدودة الموارد).
    """
    top = _hex_to_rgb(palette["top"])
    bottom = _hex_to_rgb(palette["bottom"])
    direction = palette.get("direction_name", "vertical")

    patch_size = 64
    patch = Image.new("RGB", (patch_size, patch_size))
    pixels = patch.load()
    denom = 2 * (patch_size - 1)
    for y in range(patch_size):
        for x in range(patch_size):
            if direction == "vertical":
                t = y / (patch_size - 1)
            elif direction == "horizontal":
                t = x / (patch_size - 1)
            elif direction == "diag_tlbr":
                t = (x + y) / denom
            else:  # diag_trbl
                t = ((patch_size - 1 - x) + y) / denom
            pixels[x, y] = (
                int(top[0] + (bottom[0] - top[0]) * t),
                int(top[1] + (bottom[1] - top[1]) * t),
                int(top[2] + (bottom[2] - top[2]) * t),
            )

    bg = patch.resize((WIDTH, HEIGHT), Image.BILINEAR)
    out_path = os.path.join(tempfile.gettempdir(), f"bg_{uuid.uuid4().hex}.png")
    bg.save(out_path)
    return out_path


def build_label_overlay(surah_label: str, accent: tuple[int, int, int] = (212, 175, 55)) -> str:
    """طبقة ثابتة فيها اسم السورة ورقم الآية فقط (أعلى الشاشة)، لا تتحرك أبدًا."""
    img = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(FONT_UI, 31)
    w = _text_width(draw, surah_label, font)
    fill = (accent[0], accent[1], accent[2], 255)
    draw.text(((WIDTH - w) // 2, 93), surah_label, font=font, fill=fill, direction="rtl")

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
    """يدمج: خلفية متدرجة ثابتة (صورة، تختلف ألوانها واتجاهها كل مرة) + طبقة العنوان + طبقة الآية + الصوت كاملاً."""
    if palette is None:
        palette = choose_palette()

    duration = get_audio_duration(audio_path)
    total_duration = duration + 0.8  # هامش بسيط بالنهاية حتى ما ينقطع الصوت فجأة

    background_png = build_gradient_background(palette)

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
        "-loop", "1", "-i", background_png,
        "-loop", "1", "-i", label_png,
        "-loop", "1", "-i", ayah_png,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "3:a",
        "-t", str(total_duration),
        # preset أخف (ultrafast) + خيط واحد (threads=1) عشان يضل استهلاك
        # الذاكرة تحت سقف الاستضافات المجانية المحدودة (زي Render 512MB)
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "1",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(f"فشل ffmpeg:\n{result.stderr.decode(errors='ignore')[-2000:]}")
    finally:
        try:
            os.remove(background_png)
        except OSError:
            pass
