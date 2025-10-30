# --- Base Image ---
FROM python:3.11-slim

# --- Work Directory ---
WORKDIR /app

# --- Dependencies ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Copy Source ---
COPY . .

# --- Environment Variables ---
ENV PORT=8080
EXPOSE 8080

# --- Run Server ---
# Cloudtype이 동적으로 PORT 값을 주는 경우에도 대응됨
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080}"]
