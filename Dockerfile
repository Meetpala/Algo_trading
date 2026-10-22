# APEX-3 Production Cloud Dockerfile
FROM python:3.11-slim

# Prevent Python bytecode and buffer stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Kolkata

# Install system dependencies (curl for healthchecks, tzdata for IST timezone)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    curl \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy dependency manifest first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy source code and config
COPY apex3/ ./apex3/
COPY scripts/ ./scripts/

# Ensure directory for logs exists
RUN mkdir -p /app/logs

# Expose port 5000 for operator dashboard
EXPOSE 5000

# Default entrypoint boots autonomous engine in live mode with dashboard on 0.0.0.0
CMD ["python", "-m", "apex3.main", "--mode", "live", "--host", "0.0.0.0", "--port", "5000"]
