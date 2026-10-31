FROM python:3.11-slim

# Prevent Python from writing pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Kolkata

# Install OS packages (tzdata, curl for healthcheck, build tools for Twisted/cffi)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    gcc \
    libffi-dev \
    libssl-dev \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# Create non-root execution user
RUN useradd -m -u 1000 -s /bin/bash apex3

WORKDIR /app

# Install Python dependencies first for layer caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# Copy application source
COPY --chown=apex3:apex3 . /app

# Ensure persistent runtime files exist and are owned by apex3
RUN mkdir -p /app/logs && \
    touch /app/apex3_journal.db /app/info.log && \
    chown -R apex3:apex3 /app

USER apex3

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:5000/health || exit 1

CMD ["python", "-m", "apex3.main", "--mode", "paper", "--host", "0.0.0.0", "--port", "5000"]
