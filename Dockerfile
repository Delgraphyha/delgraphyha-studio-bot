FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl ca-certificates python3 python3-pip python3-venv ffmpeg make g++ \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app


# Persian fonts used by the text-PNG tool.
# Downloaded at build time from open-source upstreams, so font binaries are
# not committed to the project repository.
RUN mkdir -p /app/fonts \
    && curl -fL "https://raw.githubusercontent.com/google/fonts/main/ofl/notonastaliqurdu/NotoNastaliqUrdu%5Bwght%5D.ttf" -o /app/fonts/nastaliq.ttf \
    && curl -fL "https://raw.githubusercontent.com/google/fonts/main/ofl/arefruqaa/ArefRuqaa-Regular.ttf" -o /app/fonts/handwriting.ttf \
    && curl -fL "https://raw.githubusercontent.com/google/fonts/main/ofl/vazirmatn/Vazirmatn%5Bwght%5D.ttf" -o /app/fonts/modern.ttf \
    && curl -fL "https://raw.githubusercontent.com/google/fonts/main/ofl/lalezar/Lalezar-Regular.ttf" -o /app/fonts/bold.ttf \
    && test -s /app/fonts/nastaliq.ttf \
    && test -s /app/fonts/handwriting.ttf \
    && test -s /app/fonts/modern.ttf \
    && test -s /app/fonts/bold.ttf

# Build the local bgutil PO-token HTTP provider.
RUN git clone --depth 1 --branch 2.0.1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /app/bgutil-provider
WORKDIR /app/bgutil-provider/server
RUN npm ci && npx tsc

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# Debian protects system Python, so use an isolated venv.
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/python -m pip install --no-cache-dir --upgrade -r /app/requirements.txt

ENV PATH="/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"
ENV PYTHONUNBUFFERED=1

# Build-time sanity checks. These do not contact YouTube.
RUN node --version \
    && which node \
    && python --version \
    && ffmpeg -version | head -n 1 \
    && python -m pip show bgutil-ytdlp-pot-provider

COPY . /app

CMD ["python", "main.py"]
