FROM debian:13-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-venv python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
    gir1.2-gstreamer-1.0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
    gstreamer1.0-plugins-bad gstreamer1.0-libav librsvg2-common ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY app.py artwork.py diagnostics.py mpris.py soundcloud.py oauth_soundcloud.py wave_queue.py ./
ENV PATH="/opt/venv/bin:${PATH}"
CMD ["python3", "-u", "app.py"]
