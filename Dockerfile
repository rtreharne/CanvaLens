FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app/

ENV DJANGO_SETTINGS_MODULE=canvaslens.settings

ENTRYPOINT ["/app/docker/entrypoint.sh"]
CMD ["gunicorn", "canvaslens.wsgi:application", "-b", "0.0.0.0:8000"]
