FROM python:3.12-slim

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

RUN uv pip install --system --no-cache \
    "aiohttp>=3.8.1" \
    "deepdiff>=6.2.1" \
    "pyjwt>=2.7.0" \
    "pycognito>=2024.2.0"

COPY pylitterbot/ pylitterbot/
COPY litter_helper.py .

ENTRYPOINT ["python", "litter_helper.py"]
