FROM python:3.11-slim

# libgl/libglib needed for trimesh's optional rendering backends; gosu drops
# from root (needed at startup to chown /config and create the PUID/PGID user)
# down to an unprivileged user before the app itself ever runs.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 curl gosu \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV LIBRARY_PATH=/data \
    CONFIG_PATH=/config \
    MPLCONFIGDIR=/config/matplotlib \
    PYTHONUNBUFFERED=1 \
    PUID=99 \
    PGID=100

VOLUME ["/data", "/config"]
EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8420/api/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8420"]
