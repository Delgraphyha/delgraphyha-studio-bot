# Delgraphyha Studio Bot

Telegram content-production bot for Delgraphyha.

Features:
- Music search and MP3 delivery
- Persian transparent PNG text
- Text style selection: Nastaliq, handwriting, modern, bold
- Text color selection
- Persian text-to-speech via ElevenLabs
- Delgraphyha channel membership check

## Local setup

1. Install Python and FFmpeg.
2. Run: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env`.
4. Put your real secrets only in `.env`.
5. Run: `python main.py`

## Security

Never commit `.env` or `cookies.txt`.
They are intentionally excluded by `.gitignore`.

## Fonts

Optional custom fonts belong in `fonts/`.
Use only fonts you have permission to redistribute.
