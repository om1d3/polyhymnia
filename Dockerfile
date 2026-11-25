FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt
COPY jellyfin_music_audit.py /app/jellyfin_music_audit.py
WORKDIR /work
ENTRYPOINT ["python", "/app/jellyfin_music_audit.py"]
