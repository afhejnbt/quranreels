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


async def download_audio(audio_url: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(audio_url)
        if resp.status_code != 200:
            raise QuranAPIError(f"تعذر تحميل الصوت من {audio_url}")
        return resp.content
