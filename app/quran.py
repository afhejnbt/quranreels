"""
وحدة الاتصال بـ Al Quran Cloud API (مجاني، بدون مفتاح API)
التوثيق: https://alquran.cloud/api
"""
import asyncio
import os
import random
import subprocess
import tempfile
import uuid

import httpx

QURAN_API_BASE = "https://api.alquran.cloud/v1"
CDN_AUDIO_BASE = "https://cdn.islamic.network/quran/audio"

TOTAL_AYAHS_IN_QURAN = 6236
MAX_AYAHS_PER_SEGMENT = 4  # الحد الافتراضي لمقطع محدد يدويًا (surah+ayah_start+ayah_end)
MAX_AYAHS_PER_SEGMENT_RANDOM = 12  # أقصى حد نسمح نوسّع له تلقائيًا بوضع العشوائي فقط
MIN_RANDOM_DURATION_SECONDS = 20  # أقل مدة مقبولة للفيديو العشوائي (بالثواني)

# عدد آيات كل سورة (1-114) — بيانات هيكلية ثابتة، يستخدمها الاختيار العشوائي
# عشان يعرف حدود كل سورة (ما يطلب آية رقم أكبر من عدد آيات السورة).
SURAH_AYAH_COUNTS = {
    1: 7, 2: 286, 3: 200, 4: 176, 5: 120, 6: 165, 7: 206, 8: 75, 9: 129, 10: 109,
    11: 123, 12: 111, 13: 43, 14: 52, 15: 99, 16: 128, 17: 111, 18: 110, 19: 98, 20: 135,
    21: 112, 22: 78, 23: 118, 24: 64, 25: 77, 26: 227, 27: 93, 28: 88, 29: 69, 30: 60,
    31: 34, 32: 30, 33: 73, 34: 54, 35: 45, 36: 83, 37: 182, 38: 88, 39: 75, 40: 85,
    41: 54, 42: 53, 43: 89, 44: 59, 45: 37, 46: 35, 47: 38, 48: 29, 49: 18, 50: 45,
    51: 60, 52: 49, 53: 62, 54: 55, 55: 78, 56: 96, 57: 29, 58: 22, 59: 24, 60: 13,
    61: 14, 62: 11, 63: 11, 64: 18, 65: 12, 66: 12, 67: 30, 68: 52, 69: 52, 70: 44,
    71: 28, 72: 28, 73: 20, 74: 56, 75: 40, 76: 31, 77: 50, 78: 40, 79: 46, 80: 42,
    81: 29, 82: 19, 83: 36, 84: 25, 85: 22, 86: 17, 87: 19, 88: 26, 89: 30, 90: 20,
    91: 15, 92: 21, 93: 11, 94: 8, 95: 8, 96: 19, 97: 5, 98: 8, 99: 8, 100: 11,
    101: 11, 102: 8, 103: 3, 104: 9, 105: 5, 106: 4, 107: 7, 108: 3, 109: 6, 110: 3,
    111: 5, 112: 4, 113: 5, 114: 6,
}

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


def random_ayah_range(max_len: int = MAX_AYAHS_PER_SEGMENT) -> tuple[int, int, int]:
    """
    يختار سورة عشوائية بالكامل، ثم مقطع عشوائي من 1 إلى max_len آيات متتالية
    داخلها (يحترم حدود عدد آيات السورة نفسها دائمًا). يرجع (surah, start, end).
    """
    surah = random.randint(1, 114)
    total = SURAH_AYAH_COUNTS[surah]
    length = random.randint(1, min(max_len, total))
    start = random.randint(1, total - length + 1)
    end = start + length - 1
    return surah, start, end


async def get_ayah_range(
    surah: int,
    ayah_start: int,
    ayah_end: int,
    reciter_key: str = DEFAULT_RECITER,
    audio_bitrate: int = 128,
    max_segment: int = MAX_AYAHS_PER_SEGMENT,
):
    """
    يجلب نطاق آيات متتالية من نفس السورة (مثلاً 2:1 إلى 2:5) ويرجعها كوحدة واحدة:
    نص مدمج (يُعرض ككتلة واحدة، والمقطع الطويل يتحرك تلقائيًا بفضل build_ayah_overlay)
    + قائمة روابط صوت كل آية على حدة (تُدمج لاحقًا بملف صوت واحد قبل توليد الفيديو).
    max_segment: الحد الأقصى المسموح لهذا الاستدعاء تحديدًا (يفيد بوضع العشوائي
    اللي يوسّع الحد تدريجيًا لو المقطع القصير طلع أقصر من المدة المطلوبة).
    """
    if ayah_end < ayah_start:
        raise QuranAPIError("رقم آخر آية لازم يكون أكبر من أو يساوي أول آية")
    if (ayah_end - ayah_start + 1) > max_segment:
        raise QuranAPIError(
            f"الحد الأقصى {max_segment} آيات بالمقطع الواحد "
            "(حتى يضل الفيديو قصير وسريع المعالجة على أي خطة استضافة)"
        )

    ayahs = await asyncio.gather(
        *[get_ayah(f"{surah}:{n}", reciter_key=reciter_key, audio_bitrate=audio_bitrate)
          for n in range(ayah_start, ayah_end + 1)]
    )
    # asyncio.gather يرجع النتائج بنفس ترتيب الطلبات دايمًا، فترتيب الآيات مضمون
    # حتى لو وصلت الردود من الشبكة بترتيب مختلف.

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
    يحمّل عدة ملفات صوت (لآيات متتالية) بالتوازي (مو وحدة وحدة، أسرع بكثير)
    ويدمجها بملف mp3 واحد متصل عبر ffmpeg concat demuxer (بدون إعادة ترميز).
    """
    if len(audio_urls) == 1:
        return await download_audio(audio_urls[0])

    work_dir = tempfile.mkdtemp(prefix=f"concat_{uuid.uuid4().hex}_")
    file_paths = [os.path.join(work_dir, f"part_{i:03d}.mp3") for i in range(len(audio_urls))]
    try:
        async def _fetch_one(url: str, path: str):
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    raise QuranAPIError(f"تعذر تحميل الصوت من {url}")
                with open(path, "wb") as f:
                    f.write(resp.content)

        # كل ملفات الصوت تُحمّل بالتوازي بدل التسلسل — يوفر وقت كبير خصوصًا
        # على استضافة فيها زمن استجابة أعلى (Cold Start) أو مقاطع بعدة آيات.
        await asyncio.gather(*[_fetch_one(u, p) for u, p in zip(audio_urls, file_paths)])

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


def get_audio_duration(audio_path: str) -> float:
    """يقيس مدة ملف صوتي بالثواني باستخدام ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", audio_path,
        ],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return float(result.stdout.strip())


def _global_ayah_to_surah_ayah(global_ayah: int) -> tuple[int, int]:
    """يحول رقم آية إجمالي (1-6236) إلى (سورة, آية داخل السورة)."""
    remaining = global_ayah
    for surah in range(1, 115):
        count = SURAH_AYAH_COUNTS[surah]
        if remaining <= count:
            return surah, remaining
        remaining -= count
    return 114, 6  # fallback (نادر جدًا)


async def smart_random_segment(
    reciter_key: str = DEFAULT_RECITER,
    min_duration: float = MIN_RANDOM_DURATION_SECONDS,
    max_ayahs: int = MAX_AYAHS_PER_SEGMENT_RANDOM,
    audio_bitrate: int = 128,
):
    """
    يختار مقطع عشوائي ذكي ببناء تدريجي:
    - يختار آية بداية عشوائية من القرآن كاملاً (1-6236)
    - يحمل كل الآيات المحتملة (حتى max_ayahs) بالتوازي من نفس السورة
    - يقيس مدة كل صوت على حدة بـ ffprobe (سريع)
    - يجمع المدد تدريجيًا حتى الوصول للمدة المطلوبة
    - يدمج فقط الآيات المطلوبة
    - يعيد المحاولة بالكامل فقط إذا استنفذ كل الآيات المتاحة وما زالت المدة غير كافية

    هذا يحل مشكلة إعادة المحاولة العشوائية العشرات من المرات، لأننا نبني على نفس
    نقطة البداية ونضيف آيات تدريجيًا بدل رمي كل شيء وبدء من جديد.
    """
    for attempt in range(20):  # حد أمان نهائي
        # اختيار آية بداية عشوائية من القرآن كاملاً
        global_ayah = random.randint(1, TOTAL_AYAHS_IN_QURAN)
        surah, start = _global_ayah_to_surah_ayah(global_ayah)
        total_ayahs = SURAH_AYAH_COUNTS[surah]

        # عدد الآيات المتاحة من نقطة البداية (لا يتجاوز max_ayahs)
        available = min(max_ayahs, total_ayahs - start + 1)

        # جلب بيانات الآيات بالتوازي
        ayah_tasks = [
            get_ayah(f"{surah}:{n}", reciter_key=reciter_key, audio_bitrate=audio_bitrate)
            for n in range(start, start + available)
        ]
        ayahs = await asyncio.gather(*ayah_tasks)

        # تحميل كل الصوتيات بالتوازي
        audio_urls = [a["audio_url"] for a in ayahs]
        audio_bytes_list = await asyncio.gather(*[download_audio(url) for url in audio_urls])

        # قياس مدة كل صوت على حدة (سريع جدًا - ffprobe على ملف صغير)
        temp_paths = []
        durations = []
        for audio_bytes in audio_bytes_list:
            path = os.path.join(tempfile.gettempdir(), f"dur_{uuid.uuid4().hex}.mp3")
            with open(path, "wb") as f:
                f.write(audio_bytes)
            temp_paths.append(path)
            durations.append(get_audio_duration(path))

        # نجمع المدد تدريجيًا ونحدد عدد الآيات المطلوبة
        accumulated = 0.0
        needed_count = 0
        for i, dur in enumerate(durations):
            accumulated += dur
            needed_count = i + 1
            if accumulated >= min_duration:
                break

        # تنظيف الملفات المؤقتة
        for p in temp_paths:
            try:
                os.remove(p)
            except OSError:
                pass

        if accumulated >= min_duration:
            # ندمج فقط الآيات المطلوبة
            needed_urls = audio_urls[:needed_count]
            final_audio = await download_and_concat_audio(needed_urls)

            end = start + needed_count - 1
            return {
                "ayah_text": " ".join(a["ayah_text"] for a in ayahs[:needed_count]),
                "surah_name_ar": ayahs[0]["surah_name_ar"],
                "surah_name_en": ayahs[0]["surah_name_en"],
                "surah_number": ayahs[0]["surah_number"],
                "ayah_number_in_surah": start,
                "ayah_range_label": f"{start}-{end}" if end > start else str(start),
                "audio_bytes": final_audio,
                "reciter_key": reciter_key,
            }

        # إذا وصلنا هنا: المدة غير كافية حتى مع كل الآيات المتاحة
        # نعيد المحاولة بآية بداية جديدة (سورة جديدة)

    raise QuranAPIError(
        f"تعذر إيجاد مقطع عشوائي بالمدة المطلوبة ({min_duration}ث) بعد 20 محاولة. "
        "جرّب تقليل المدة المطلوبة أو زيادة max_ayahs."
    )
