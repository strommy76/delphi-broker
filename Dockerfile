FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8420

CMD ["python", "-m", "uvicorn", "delphi_broker.main:app", "--host", "0.0.0.0", "--port", "8420", "--app-dir", "src"]
