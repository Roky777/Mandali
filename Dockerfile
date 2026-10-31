FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=config.settings
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 app
COPY config ./config
COPY core ./core
COPY billing ./billing
COPY checkout ./checkout
COPY integrations ./integrations
COPY recurring ./recurring
COPY portal ./portal
COPY reports ./reports
COPY templates ./templates
COPY static ./static
COPY manage.py gunicorn.conf.py ./
RUN python manage.py collectstatic --noinput --settings=config.build_settings
USER 10001:10001
EXPOSE 8080
CMD ["gunicorn", "--config", "gunicorn.conf.py", "config.wsgi:application"]
