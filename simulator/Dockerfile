FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

RUN useradd --create-home --uid 10001 appuser

COPY --chown=appuser:appuser simulator/main.py ./main.py

USER appuser

CMD ["python", "main.py"]
