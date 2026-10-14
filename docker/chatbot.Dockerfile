# The analytics chatbot and the sqlgen gRPC service for the local Compose stack
# (docker-compose.yml, profile `app`): one image, two commands.
FROM python:3.11-slim
# Run as an unprivileged user (milestone 4); no .pyc writes, so the root filesystem can be read-only
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY requirements.txt .
# Packaging tools the base image ships are patched too (Trivy flagged old wheel and setuptools'
# vendored jaraco.context); the app never uses them at runtime, but they're in the image.
RUN pip install --no-cache-dir --upgrade pip "setuptools>=80" "wheel>=0.46.2" \
 && pip install --no-cache-dir -r requirements.txt
COPY chatbot/ chatbot/
COPY sqlgen/ sqlgen/
COPY etl/ etl/
COPY config/ config/
USER 10001
EXPOSE 8000
CMD ["uvicorn", "chatbot.app:app", "--host", "0.0.0.0", "--port", "8000"]
