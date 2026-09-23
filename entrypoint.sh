#!/bin/bash
set -e

# Create data directory
mkdir -p /app/data

# Start Xvfb (virtual display for scrcpy/ffmpeg)
Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
export DISPLAY=:99
sleep 1

# Start mediamtx (WebRTC/RTSP server)
/opt/mediamtx.bin /etc/mediamtx/mediamtx.yml &
sleep 1

# Run the application
exec python3 app.py
