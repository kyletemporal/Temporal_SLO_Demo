FROM python:3.12-slim

# One pinned dependency: the SDK under test.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt && useradd -u 10001 app

COPY *.py /app/
WORKDIR /app
USER app
ENV PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "worker.py"]
