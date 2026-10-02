FROM node:22-bookworm-slim AS provider-build
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates python3 make g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
RUN git clone --depth 1 --branch 2.0.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git bgutil-provider
WORKDIR /build/bgutil-provider/server
RUN npm ci && npx tsc

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=provider-build /build/bgutil-provider /app/bgutil-provider
COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir --upgrade -r /app/requirements.txt \
    && python -m pip show yt-dlp \
    && python -m pip show bgutil-ytdlp-pot-provider
COPY . /app
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
