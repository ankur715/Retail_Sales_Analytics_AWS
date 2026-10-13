# The analytics chatbot for the local Compose stack (docker-compose.yml, profile `app`).
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY chatbot/ chatbot/
COPY etl/ etl/
COPY config/ config/
EXPOSE 8000
CMD ["uvicorn", "chatbot.app:app", "--host", "0.0.0.0", "--port", "8000"]
