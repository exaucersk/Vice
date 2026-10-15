FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY deploy/share-host/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && mkdir /data && chown 10001:10001 /data
COPY vice/__init__.py vice/share_host.py /app/vice/
USER 10001:10001
EXPOSE 8080
CMD ["python", "-m", "vice.share_host"]
