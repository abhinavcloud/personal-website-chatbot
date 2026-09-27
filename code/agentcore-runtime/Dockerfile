FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install Python dependencies
COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

# Copy the runtime and skills; .dockerignore excludes local secrets and artifacts.
COPY . .

# AgentCore's HTTP server provides /ping and /invocations on this port.
EXPOSE 8080

# Start the AgentCore application
CMD ["python", "main-v2.py"]
