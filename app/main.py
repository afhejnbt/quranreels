"""
Quran Reels API
================
أداة تولّد فيديو عمودي (9:16) جاهز للنشر على انستقرام/تيك توك، من آية واحدة
أو مقطع آيات متتالية: النص العربي + تلاوة صوتية — بضغطة زر واحدة عبر API.

يتكيّف القالب تلقائيًا حسب طول النص:
  - نص قصير/متوسط → خط كبير وثابت في منتصف الشاشة.
  - نص طويل → خط مريح للقراءة مع تمرير (Scroll) سلس متزامن مع مدة التلاوة،
    بدل تصغير الخط بشكل مبالغ فيه أو قصّ النص.

نقاط النهاية:
  GET /health       فحص سريع للتأكد إن الخدمة شغالة.
  GET /reciters     قائمة القراء المتاحين.
  GET /palettes     قائمة الخلفيات المتاحة (بالاسم والرقم).
  GET /generate     يولّد الفيديو ويرجعه مباشرة كملف mp4.

  معاملات /generate (كلها اختيارية):
    - surah      : رقم السورة (1-114)
    - ayah       : رقم آية واحدة داخل السورة
    - ayah_start : أول آية بمقطع من عدة آيات (بدل ayah)
    - ayah_end   : آخر آية بمقطع من عدة آيات (لازم تكون مع ayah_start)
    - random     : true لاختيار سورة عشوائية + مقطع عشوائي (1-4 آيات) منها
    - reciter    : alafasy (افتراضي) / sudais / husary / minshawi
    - palette    : رقم خلفية محدد (0-5). فاضي = عشوائي كل مرة.

  أمثلة:
    /generate?surah=2&ayah=255&reciter=alafasy
    /generate?surah=2&ayah_start=1&ayah_end=4&palette=3
    /generate?random=true
"""
import os
import tempfile
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from app.quran import (
    RECITERS,
    DEFAULT_RECITER,
    MAX_AYAHS_PER_SEGMENT_RANDOM,
    MIN_RANDOM_DURATION_SECONDS,
    QuranAPIError,
    get_ayah,
    get_ayah_range,
    random_ayah_range,
    download_audio,
    download_and_concat_audio,
)
from app.render import (
    build_label_overlay,
    build_ayah_overlay,
    render_video,
    choose_palette,
    ACCENT_COLORS,
    ACCENT_NAMES,
    get_audio_duration,
)

app = FastAPI(
    title="Quran Reels API",
    description="يولّد فيديوهات قصيرة لآيات قرآنية (نص + تلاوة) بضغطة زر، جاهزة للربط مع Make.com",
    version="1.2.0",
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/reciters")
async def list_reciters():
    """يرجع أسماء القراء المتاحين، يفيد بضبط سيناريو Make.com."""
    return {"reciters": list(RECITERS.keys()), "default": DEFAULT_RECITER}


@app.get("/palettes")
async def list_palettes():
    """يرجع قائمة الخلفيات المتاحة بالاسم ورقمها، يفيد باختيار خلفية محددة من Google Sheet."""
    return {"palettes": [{"index": i, "name": name} for i, name in enumerate(ACCENT_NAMES)]}


def _cleanup(*paths: str):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _opt_int(value: str | None, field_name: str, min_value: int | None = None,
             max_value: int | None = None) -> int | None:
    """
    يحوّل قيمة query param نصية إلى رقم صحيح اختياري، ويتعامل مع القيمة الفاضية
    ("") كأنها ما أُرسلت أصلاً — هذا يحصل كثير لما Make.com يعوّض متغير من خلية
    فاضية بجوجل شيتس (يرسل &palette= فاضي بدل ما يحذف المعامل بالكامل).
    """
    if value is None or value.strip() == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise HTTPException(400, f"قيمة غير صحيحة لـ {field_name}: '{value}' (لازم تكون رقم صحيح)")
    if min_value is not None and parsed < min_value:
        raise HTTPException(400, f"{field_name} لازم يكون {min_value} أو أكبر")
    if max_value is not None and parsed > max_value:
        raise HTTPException(400, f"{field_name} لازم يكون {max_value} أو أقل")
    return parsed


@app.get("/generate")
async def generate(
    surah: str | None = Query(None, description="رقم السورة (1-114)"),
    ayah: str | None = Query(None, description="رقم آية واحدة داخل السورة"),
    ayah_start: str | None = Query(None, description="أول آية بمقطع من عدة آيات"),
    ayah_end: str | None = Query(None, description="آخر آية بمقطع من عدة آيات"),
    random_verse: bool = Query(False, alias="random", description="اختيار آية عشوائية من كامل القرآن"),
    reciter: str = Query(DEFAULT_RECITER, description="مفتاح القارئ"),
    palette: str | None = Query(None, description="رقم خلفية محدد (0-5)، فاضي = عشوائي"),
):
    # كل المعاملات الرقمية تمر عبر _opt_int عشان تتقبل القيم الفاضية بأمان
    # (حالة شائعة عند التعويض من خلايا Google Sheets فاضية عبر Make.com).
    surah = _opt_int(surah, "surah", 1, 114)
    ayah = _opt_int(ayah, "ayah", 1)
    ayah_start = _opt_int(ayah_start, "ayah_start", 1)
    ayah_end = _opt_int(ayah_end, "ayah_end", 1)
    palette_index = _opt_int(palette, "palette", 0, len(ACCENT_COLORS) - 1)

    is_range = ayah_start is not None or ayah_end is not None
    audio_path: str | None = None  # يتحدد بوضع العشوائي أثناء حلقة إعادة المحاولة أدناه

    # 1) تحديد الآية/المقطع المطلوب وجلب النص + الصوت
    if random_verse or (surah is None and ayah is None and not is_range):
        # عشوائي: نبدأ بمقطع صغير (1-4 آيات)، ولو طلعت مدة الصوت الفعلية قصيرة
        # (أقل من 20 ثانية — غير مناسبة للنشر)، نوسّع المقطع تدريجيًا ونعيد
        # المحاولة تلقائيًا، لين نوصل لمدة كافية أو نضرب حد أقصى أمان.
        for attempt_max_len in (4, 6, 8, MAX_AYAHS_PER_SEGMENT_RANDOM):
            r_surah, r_start, r_end = random_ayah_range(max_len=attempt_max_len)
            try:
                data = await get_ayah_range(
                    r_surah, r_start, r_end, reciter_key=reciter,
                    max_segment=MAX_AYAHS_PER_SEGMENT_RANDOM,
                )
            except QuranAPIError as e:
                raise HTTPException(502, str(e))
            try:
                audio_bytes = await download_and_concat_audio(data["audio_urls"])
            except QuranAPIError as e:
                raise HTTPException(502, str(e))

            # نفحص المدة الفعلية بسرعة (بدون رسم أو ترميز فيديو، بس فحص الصوت)
            check_path = os.path.join(tempfile.gettempdir(), f"check_{uuid.uuid4().hex}.mp3")
            with open(check_path, "wb") as f:
                f.write(audio_bytes)
            duration = get_audio_duration(check_path)

            if duration >= MIN_RANDOM_DURATION_SECONDS or attempt_max_len == MAX_AYAHS_PER_SEGMENT_RANDOM:
                audio_path = check_path
                break
            os.remove(check_path)

        ayah_label_number = data["ayah_range_label"]

    elif is_range:
        if surah is None or ayah_start is None or ayah_end is None:
            raise HTTPException(400, "وضع المقطع يحتاج surah و ayah_start و ayah_end الثلاثة مع بعض")
        try:
            data = await get_ayah_range(surah, ayah_start, ayah_end, reciter_key=reciter)
        except QuranAPIError as e:
            raise HTTPException(502, str(e))
        try:
            audio_bytes = await download_and_concat_audio(data["audio_urls"])
        except QuranAPIError as e:
            raise HTTPException(502, str(e))
        ayah_label_number = data["ayah_range_label"]

    elif surah is not None and ayah is not None:
        reference = f"{surah}:{ayah}"
        try:
            data = await get_ayah(reference, reciter_key=reciter)
        except QuranAPIError as e:
            raise HTTPException(502, str(e))
        try:
            audio_bytes = await download_audio(data["audio_url"])
        except QuranAPIError as e:
            raise HTTPException(502, str(e))
        ayah_label_number = str(data["ayah_number_in_surah"])

    else:
        raise HTTPException(
            400,
            "حدد إما (surah + ayah) لآية واحدة، أو (surah + ayah_start + ayah_end) لمقطع، أو random=true",
        )

    work_id = uuid.uuid4().hex
    output_path = os.path.join(tempfile.gettempdir(), f"video_{work_id}.mp4")

    if audio_path is None:
        # وضع الآية الوحدة أو المقطع المحدد يدويًا: لسا ما كتبنا ملف الصوت
        audio_path = os.path.join(tempfile.gettempdir(), f"audio_{work_id}.mp3")
        with open(audio_path, "wb") as f:
            f.write(audio_bytes)
    # (وضع العشوائي: audio_path مكتوب أصلاً من حلقة إعادة المحاولة فوق)

    # 2) رسم الطبقات: لوحة ألوان (محددة أو عشوائية) + العنوان + نص الآية/المقطع
    palette = choose_palette(index=palette_index)
    label = f"{data['surah_name_ar']} - آية {ayah_label_number}"
    label_png = build_label_overlay(label, accent=palette["accent"])
    ayah_png, ayah_height, needs_scroll = build_ayah_overlay(data["ayah_text"])

    # 3) توليد الفيديو النهائي عبر ffmpeg (يضمن تشغيل الصوت كاملاً من أول لآخر ثانية)
    try:
        render_video(label_png, ayah_png, ayah_height, needs_scroll, audio_path, output_path, palette=palette)
    finally:
        _cleanup(audio_path, label_png, ayah_png)

    filename = f"{data['surah_name_en']}_{ayah_label_number}.mp4"

    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(_cleanup, output_path),
    )
