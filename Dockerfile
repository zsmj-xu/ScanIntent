FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY ck_reader.py clickhouse.py collector.py reconstruction.py scanintent.py schema.sql source.py store.py web.py ./
COPY static/ ./static/

EXPOSE 8765
ENTRYPOINT ["python", "scanintent.py"]
