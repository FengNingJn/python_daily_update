FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    NGA_OUTPUT_DIR=/data \
    NGA_CONFIG_FILE=/data/config.json \
    NGA_STATE_FILE=/state/push_state.json \
    NGA_LOG_FILE=/logs/nas_runner.log \
    NGA_LOCK_FILE=/state/update.lock

WORKDIR /app

COPY requirements-nas.txt /app/requirements-nas.txt
COPY wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r /app/requirements-nas.txt

COPY update_nga.py nas_runner.py /app/

CMD ["python", "/app/nas_runner.py", "--loop", "--interval", "300"]
