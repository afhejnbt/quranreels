FROM python:3.12-slim

# ffmpeg مطلوب لدمج الصوت والصورة في فيديو نهائي.
# ملاحظة: دعم تشكيل النص العربي (RAQM) مدمج بالفعل داخل عجلة Pillow نفسها،
# فلا حاجة لتثبيت مكتبات نظام إضافية له.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PORT=8000
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
