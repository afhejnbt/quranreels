"""
Quran Reels API
================
أداة تولّد فيديو عمودي (9:16) جاهز للنشر على انستقرام/تيك توك، لآية قرآنية واحدة:
النص العربي + تلاوة صوتية — بضغطة زر واحدة عبر API.

يتكيّف القالب تلقائيًا حسب طول الآية:
  - آية قصيرة/متوسطة → خط كبير وثابت في منتصف الشاشة.
  - آية طويلة → خط مريح للقراءة مع تمرير (Scroll) سلس متزامن مع مدة التلاوة،
    بدل تصغير الخط بشكل مبالغ فيه أو قصّ النص.

نقاط النهاية:
  GET /health
      فحص سريع للتأكد إن الخدمة شغالة.

  GET /reciters
      قائمة القراء المتاحين.

  GET /generate
      يولّد الفيديو ويرجعه مباشرة كملف mp4.
      المعاملات (query params):
        - surah   (اختياري): رقم السورة 1-114
        - ayah    (اختياري): رقم الآية داخل السورة
        - random  (اختياري): true لاختيار آية عشوائية من كامل القرآن
        - reciter (اختياري): alafasy (افتراضي) / sudais / husary / minshawi

      أمثلة:
        /generate?surah=2&ayah=255&reciter=alafasy
        /generate?random=true
        /generate?surah=112&ayah=1
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
    QuranAPIError,
    get_ayah,
    random_ayah_reference,
    download_audio,
)
from app.render import build_label_overlay, build_ayah_overlay, render_video, choose_palette

app = FastAPI(
    title="Quran Reels API",
    description="يولّد فيديوهات قصيرة لآيات قرآنية (نص + تلاوة) بضغطة زر، جاهزة للربط مع Make.com",
    version="1.1.0",
)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/reciters")
async def list_reciters():
    """يرجع أسماء القراء المتاحين، يفيد بضبط سيناريو Make.com."""
    return {"reciters": list(RECITERS.keys()), "default": DEFAULT_RECITER}


def _cleanup(*paths: str):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


@app.get("/generate")
async def generate(
    surah: int | None = Query(None, ge=1, le=114, description="رقم السورة (1-114)"),
    ayah: int | None = Query(None, ge=1, description="رقم الآية داخل السورة"),
    random_verse: bool = Query(False, alias="random", description="اختيار آية عشوائية من كامل القرآن"),
    reciter: str = Query(DEFAULT_RECITER, description="مفتاح القارئ"),
):
    # 1) تحديد الآية المطلوبة
    if random_verse or (surah is None and ayah is None):
        reference: int | str = random_ayah_reference()
    elif surah is not None and ayah is not None:
        reference = f"{surah}:{ayah}"
    else:
        raise HTTPException(400, "لازم تحدد surah و ayah معًا، أو تستخدم random=true")

    # 2) جلب نص الآية من Al Quran Cloud API
    try:
        data = await get_ayah(reference, reciter_key=reciter)
    except QuranAPIError as e:
        raise HTTPException(502, str(e))

    # 3) تحميل ملف الصوت (التلاوة) كاملاً
    try:
        audio_bytes = await download_audio(data["audio_url"])
    except QuranAPIError as e:
        raise HTTPException(502, str(e))

    work_id = uuid.uuid4().hex
    audio_path = os.path.join(tempfile.gettempdir(), f"audio_{work_id}.mp3")
    output_path = os.path.join(tempfile.gettempdir(), f"video_{work_id}.mp4")

    with open(audio_path, "wb") as f:
        f.write(audio_bytes)

    # 4) رسم الطبقات: لوحة ألوان عشوائية + العنوان (ثابت) + نص الآية (ثابت أو متحرك حسب الطول)
    palette = choose_palette()
    label = f"{data['surah_name_ar']} - آية {data['ayah_number_in_surah']}"
    label_png = build_label_overlay(label, accent=palette["accent"])
    ayah_png, ayah_height, needs_scroll = build_ayah_overlay(data["ayah_text"])

    # 5) توليد الفيديو النهائي عبر ffmpeg (يضمن تشغيل الصوت كاملاً من أول لآخر ثانية)
    try:
        render_video(label_png, ayah_png, ayah_height, needs_scroll, audio_path, output_path, palette=palette)
    finally:
        _cleanup(audio_path, label_png, ayah_png)

    filename = f"{data['surah_name_en']}_{data['ayah_number_in_surah']}.mp4"

    return FileResponse(
        output_path,
        media_type="video/mp4",
        filename=filename,
        background=BackgroundTask(_cleanup, output_path),
    )
