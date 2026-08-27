"""
وحدة الاتصال بـ Al Quran Cloud API (مجاني، بدون مفتاح API)
التوثيق: https://alquran.cloud/api
"""
import random
import httpx

QURAN_API_BASE = "https://api.alquran.cloud/v1"
CDN_AUDIO_BASE = "https://cdn.islamic.network/quran/audio"

# عدد آيات كل سورة (يفيد باختيار آية عشوائية صحيحة)
# رقم السورة -> عدد الآيات
TOTAL_AYAHS_IN_QURAN = 6236

RECITERS = {
    "alafasy": "ar.alafasy",     # مشاري العفاسي (الافتراضي)
    "sudais": "ar.sudais",       # عبدالرحمن السديس
    "husary": "ar.husary",       # محمود خليل الحصري
    "minshawi": "ar.minshawi",   # محمد صديق المنشاوي
}

DEFAULT_RECITER = "alafasy"
DEFAULT_TRANSLATION_EDITION = "ar.muyassar"  # تفسير ميسر مبسط بالعربية (اختياري)


class QuranAPIError(Exception):
    pass


async def get_ayah(
    reference: int | str,
    reciter_key: str = DEFAULT_RECITER,
    include_translation: bool = False,
    translation_edition: str = DEFAULT_TRANSLATION_EDITION,
    audio_bitrate: int = 128,
):
    """
    يجلب نص آية واحدة (بالرسم العثماني) + رابط الصوت + اسم السورة.
    reference: رقم الآية الإجمالي (1-6236) أو بصيغة "سورة:آية" مثل "2:255"
    """
    if reciter_key not in RECITERS:
        raise QuranAPIError(f"قارئ غير معروف: {reciter_key}. الخيارات: {list(RECITERS)}")

    reciter_edition = RECITERS[reciter_key]

    async with httpx.AsyncClient(timeout=15) as client:
        # نص الآية بالرسم العثماني
        text_resp = await client.get(f"{QURAN_API_BASE}/ayah/{reference}/quran-uthmani")
        if text_resp.status_code != 200:
            raise QuranAPIError(f"تعذر جلب نص الآية {reference}")
        text_data = text_resp.json()["data"]

        translation_text = None
        if include_translation:
            tr_resp = await client.get(f"{QURAN_API_BASE}/ayah/{reference}/{translation_edition}")
            if tr_resp.status_code == 200:
                translation_text = tr_resp.json()["data"]["text"]

    ayah_number_global = text_data["number"]  # 1-6236
    audio_url = f"{CDN_AUDIO_BASE}/{audio_bitrate}/{reciter_edition}/{ayah_number_global}.mp3"

    return {
        "ayah_text": text_data["text"],
        "translation_text": translation_text,
        "surah_name_ar": text_data["surah"]["name"],
        "surah_name_en": text_data["surah"]["englishName"],
        "surah_number": text_data["surah"]["number"],
        "ayah_number_in_surah": text_data["numberInSurah"],
        "ayah_number_global": ayah_number_global,
        "audio_url": audio_url,
        "reciter_key": reciter_key,
    }


def random_ayah_reference() -> int:
    """يرجع رقم آية عشوائي بين 1 و 6236 (يشمل كل القرآن)."""
    return random.randint(1, TOTAL_AYAHS_IN_QURAN)


async def get_ayah_range(
    surah: int,
    ayah_start: int,
    ayah_end: int,
    reciter_key: str = DEFAULT_RECITER,
    audio_bitrate: int = 128,
):
    """
    يجلب نطاق آيات متتالية من نفس السورة (مثلاً 2:1 إلى 2:5) ويرجعها كوحدة واحدة:
    نص مدمج (يُعرض ككتلة واحدة، والمقطع الطويل يتحرك تلقائيًا بفضل build_ayah_overlay)
    + قائمة روابط صوت كل آية على حدة (تُدمج لاحقًا بملف صوت واحد قبل توليد الفيديو).
    """
    if ayah_end < ayah_start:
        raise QuranAPIError("رقم آخر آية لازم يكون أكبر من أو يساوي أول آية")
    if (ayah_end - ayah_start) > 10:
        raise QuranAPIError(
            "الحد الأقصى 10 آيات بالمقطع الواحد (حتى ما يصير الفيديو طويل جدًا "
            "ويستهلك ذاكرة أكثر من طاقة الاستضافة المجانية)"
        )

    ayahs = []
    for n in range(ayah_start, ayah_end + 1):
        data = await get_ayah(f"{surah}:{n}", reciter_key=reciter_key, audio_bitrate=audio_bitrate)
        ayahs.append(data)

    combined_text = " ".join(a["ayah_text"] for a in ayahs)

    return {
        "ayah_text": combined_text,
        "surah_name_ar": ayahs[0]["surah_name_ar"],
        "surah_name_en": ayahs[0]["surah_name_en"],
        "surah_number": ayahs[0]["surah_number"],
        "ayah_number_in_surah": ayah_start,      # لأغراض التسمية
        "ayah_range_label": f"{ayah_start}-{ayah_end}" if ayah_end > ayah_start else str(ayah_start),
        "audio_urls": [a["audio_url"] for a in ayahs],
        "reciter_key": reciter_key,
    }


async def download_and_concat_audio(audio_urls: list[str]) -> bytes:
    """
    يحمّل عدة ملفات صوت (لآيات متتالية) ويدمجها بملف mp3 واحد متصل
    عبر ffmpeg concat demuxer (بدون إعادة ترميز، سريع وبلا فقدان جودة).
    """
    import subprocess
    import tempfile
    import os
    import uuid

    if len(audio_urls) == 1:
        return await download_audio(audio_urls[0])

    work_dir = tempfile.mkdtemp(prefix=f"concat_{uuid.uuid4().hex}_")
    file_paths = []
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            for i, url in enumerate(audio_urls):
                resp = await client.get(url)
                if resp.status_code != 200:
                    raise QuranAPIError(f"تعذر تحميل الصوت من {url}")
                path = os.path.join(work_dir, f"part_{i:03d}.mp3")
                with open(path, "wb") as f:
                    f.write(resp.content)
                file_paths.append(path)

        list_path = os.path.join(work_dir, "list.txt")
        with open(list_path, "w") as f:
            for p in file_paths:
                f.write(f"file '{p}'\n")

        out_path = os.path.join(work_dir, "combined.mp3")
        result = subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out_path],
            capture_output=True,
        )
        if result.returncode != 0:
            raise QuranAPIError(f"فشل دمج الصوت: {result.stderr.decode(errors='ignore')[-500:]}")

        with open(out_path, "rb") as f:
            return f.read()
    finally:
        for p in file_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        for extra in ("list.txt", "combined.mp3"):
            try:
                os.remove(os.path.join(work_dir, extra))
            except OSError:
                pass
        try:
            os.rmdir(work_dir)
        except OSError:
            pass


async def download_audio(audio_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(audio_url)
        if resp.status_code != 200:
            raise QuranAPIError(f"تعذر تحميل الصوت من {audio_url}")
        return resp.content
