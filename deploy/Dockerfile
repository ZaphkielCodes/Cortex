FROM python:3.14-slim
WORKDIR /app
COPY hub/requirements.txt /tmp/hub-requirements.txt
RUN pip install --no-cache-dir -r /tmp/hub-requirements.txt && useradd --uid 10001 --create-home cortex
COPY hub /app/hub
COPY shared /app/shared
# UI models are pure schemas imported by the hub; do not install desktop/audio dependencies.
COPY agent/*.py /app/agent/
RUN mkdir /state /app/data && chown -R cortex:cortex /state /app/data
USER cortex
ENV CORTEX_DB_PATH=/state/cortex.db CORTEX_HUB_HOST=0.0.0.0 CORTEX_HOST_LOCALITY=cloud_worker
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["python", "-m", "uvicorn", "hub.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-proxy-headers"]
