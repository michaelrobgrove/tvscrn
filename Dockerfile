FROM python:3.11-slim

# System dependencies for scrcpy, ffmpeg, ADB, Xvfb
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    scrcpy \
    adb \
    xvfb \
    xauth \
    wget \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

# Install mediamtx (WebRTC/RTSP server)
ARG MEDIAMTX_VERSION=v1.11.3
RUN wget -q "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_amd64.tar.gz" -O /tmp/mediamtx.tar.gz \
    && tar -xzf /tmp/mediamtx.tar.gz -C /opt/ \
    && mv /opt/mediamtx /opt/mediamtx.bin \
    && chmod +x /opt/mediamtx.bin \
    && rm /tmp/mediamtx.tar.gz

WORKDIR /app

# Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code
COPY app.py .
COPY templates/ templates/

# MediaMTX config
COPY mediamtx.yml /etc/mediamtx/mediamtx.yml

# Entry point
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Persistent data volume
VOLUME ["/app/data"]

EXPOSE 5000

ENTRYPOINT ["/entrypoint.sh"]
