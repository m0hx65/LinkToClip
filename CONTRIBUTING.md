# Contributing to LinkToClip

Thanks for your interest in improving this project!

## Development setup

```bash
git clone https://github.com/m0hx65/LinkToClip.git
cd LinkToClip
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt ruff
cp .env.example .env    # set BOT_TOKEN
python -m bot.main
```

Before pushing, make sure the linter passes — CI enforces it:

```bash
ruff check .
```

## Reporting issues

Please use the issue templates. The short version of what helps most:

- What you expected vs. what happened
- The **platform** (Instagram, TikTok, X, YouTube) and a **sample URL** if the content is public
- **Environment**: local, Docker, Render, or another host — and relevant env vars **without** secrets (never paste `BOT_TOKEN` or cookies)
- **Logs** around the failure (redact tokens and cookies)

Extraction failures are often fixed upstream — try upgrading `yt-dlp` before filing.

## Pull requests

- Keep changes focused: one concern per PR.
- Match the existing code style — formatting, type hints, and logging conventions (`ruff check .` must pass).
- If you change user-visible behavior or configuration, update **README.md** and **.env.example** in the same PR.
- Describe how you tested the change (sample URLs and platforms tried).

## Security

If you find a security vulnerability, please report it privately to the repository maintainer rather than filing a public issue with exploit details.
