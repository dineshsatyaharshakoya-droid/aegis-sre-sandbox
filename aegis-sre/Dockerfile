FROM python:3.13-slim

WORKDIR /app

# Install system dependencies (needed for compilation of some python packages and sqlite)
RUN apt-get update && apt-get install -y \
    gcc \
    sqlite3 \
    libsqlite3-dev \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source code
COPY . .

# Expose the API port
EXPOSE 8000

# Set python path to allow importing aegis_sre
ENV PYTHONPATH=/app

# Start the FastAPI server
CMD ["uvicorn", "aegis_sre.telemetry.api_receiver:app", "--host", "0.0.0.0", "--port", "8000"]
