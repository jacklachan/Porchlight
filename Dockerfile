FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PORCHLIGHT_HOST=0.0.0.0 \
    PORCHLIGHT_PORT=7860 \
    PORCHLIGHT_DATA_DIR=/data

COPY pyproject.toml README.md ./
COPY porchlight ./porchlight
RUN pip install --no-cache-dir ".[aws]"

RUN useradd --create-home app && mkdir -p /data && chown app /data
USER app
EXPOSE 7860

# Add --demo to run against the bundled local stand-in instead of Ring.
CMD ["python", "-m", "porchlight"]
