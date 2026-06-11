# MCP-сервер STDIO: запуск через `docker run -i -e YANDEX_SEARCH_API_KEY -e YANDEX_FOLDER_ID <image>`
# HTTP-HEALTHCHECK намеренно отсутствует: сервер не слушает порт (урок официального демо).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir .

RUN useradd --uid 1000 --create-home mcp
USER mcp

CMD ["python", "-m", "yandex_search_mcp"]
