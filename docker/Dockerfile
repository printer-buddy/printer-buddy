FROM python:3.12-slim
WORKDIR /srv
COPY requirements/core.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY ui ./ui
ENV DB_PATH=/data/monitor.db
VOLUME /data
ENV PORT=8080
EXPOSE 8080
# PORT can be changed at run time (e.g. PORT=8081 in docker-compose.yml)
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
