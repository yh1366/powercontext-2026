# Container image for StudyMind (Qoder track).
# Minimal dependency set only: the app boots without seekdb/PowerContext and
# reports the degraded mode honestly in /api/health. Mount or point it at a
# seekdb server via SEEKDB_HOST if you want the full retrieval path.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=5000 \
    HOST=0.0.0.0

WORKDIR /app

COPY requirements.txt ./
# The root file includes the project's list via "-r qoder/requirements.txt",
# so that file has to exist before pip resolves it.
COPY qoder/requirements.txt ./qoder/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5000/api/health',timeout=3)"

CMD ["python", "app.py"]
