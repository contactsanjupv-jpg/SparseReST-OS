# One image runs the API, the worker(s), the inbound checker and the migrations -- the command differs.
FROM python:3.12-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --system --uid 10001 --create-home sparserest
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY --chown=sparserest:sparserest . .
USER sparserest
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live',timeout=4).status==200 else 1)"
CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
