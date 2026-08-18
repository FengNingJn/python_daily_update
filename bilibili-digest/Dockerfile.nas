FROM local/nga-updater:1.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY bilibili-digest/wheels /wheels-bilibili
RUN pip install --no-cache-dir --no-index --find-links=/wheels-bilibili PyYAML

COPY message_hub.py /app/message_hub.py
COPY bilibili-digest/bilibili_digest.py /app/bilibili_digest.py
COPY bilibili-digest/nas_service.py /app/nas_service.py
COPY bilibili-digest/bili_digest /app/bili_digest
COPY bilibili-digest/config /app/config

CMD ["python", "/app/nas_service.py", "--loop"]
