# Web2API

Web2API wraps browser-based AI services as OpenAI / Anthropic compatible HTTP APIs. It keeps a real browser session alive, then exposes local API endpoints for OpenAI SDK, Cursor, Cherry Studio, and other clients that can talk to `/v1/chat/completions`.

The repository currently includes a built-in `tongji` plugin and a general plugin structure for adding more web services.

## Features

- OpenAI Chat Completions compatible API
- OpenAI Responses compatible API
- Anthropic Messages compatible API
- Streaming and non-streaming responses
- Image and document attachments
- Local PDF path / filename detection with `pdftotext` text injection
- Tagged tool protocol adaptation
- Browser session reuse
- Web config UI for API keys, admin password, proxy groups, accounts, and models

## Quick Start

### Run From Source

Requirements:

- Python 3.12+
- [uv](https://github.com/astral-sh/uv) is recommended
- A working Chromium / fingerprint-chromium executable
- `poppler-utils` on Linux if you need PDF text extraction

Linux / macOS:

```bash
git clone https://github.com/<your-name>/web2api.git
cd web2api
./start.sh
```

Windows:

```bat
git clone https://github.com/<your-name>/web2api.git
cd web2api
start.bat
```

The scripts create `config.local.yaml`, install dependencies, and start `main.py`.

Open:

- Config login: `http://127.0.0.1:9000/login`
- API base: `http://127.0.0.1:9000`

### Docker Compose

```bash
docker compose up -d --build
```

Runtime data is stored in `./docker-data`.

The Docker image includes Python, fingerprint-chromium, Xvfb, and `poppler-utils`.

## Basic Config

Config priority:

1. `WEB2API_CONFIG_PATH`
2. `config.local.yaml`
3. `config.yaml`

Common fields:

```yaml
server:
  host: '127.0.0.1'
  port: 9000

auth:
  api_key: ''
  config_secret: ''

browser:
  chromium_bin: ''
  headless: false
  no_sandbox: false
  disable_gpu: false
```

Notes:

- Empty `auth.api_key` uses the default `tongji-api-key`
- Empty `auth.config_secret` enables first-run admin password setup at `/login`
- Set `browser.chromium_bin` to your browser executable path
- Docker uses `/opt/fingerprint-chromium/chrome`

## API Examples

OpenAI Chat Completions:

```bash
curl -s "http://127.0.0.1:9000/openai/tongji/v1/chat/completions" \
  -H "Authorization: Bearer tongji-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "stream": false,
    "messages": [
      {"role": "user", "content": "Hello"}
    ]
  }'
```

OpenAI Responses:

```bash
curl -s "http://127.0.0.1:9000/openai/tongji/v1/responses" \
  -H "Authorization: Bearer tongji-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "input": "Explain Web2API in three sentences."
  }'
```

Anthropic Messages:

```bash
curl -s "http://127.0.0.1:9000/anthropic/tongji/v1/messages" \
  -H "x-api-key: tongji-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello"}
    ]
  }'
```

Local PDF summarization:

```bash
curl -s "http://127.0.0.1:9000/openai/tongji/v1/chat/completions" \
  -H "Authorization: Bearer tongji-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.1",
    "messages": [
      {"role": "user", "content": "Summarize this PDF\n/home/user/papers/example.pdf"}
    ]
  }'
```

## Routes

OpenAI:

- `GET /openai/{provider}/v1/models`
- `POST /openai/{provider}/v1/chat/completions`
- `POST /openai/{provider}/v1/responses`
- `POST /openai/{provider}/v1/embeddings`

Anthropic:

- `GET /anthropic/{provider}/v1/models`
- `GET /anthropic/{provider}/v1/models/{model_id}`
- `POST /anthropic/{provider}/v1/messages`

Config UI:

- `GET /login`
- `GET /config`
- `GET /api/config`
- `PUT /api/config`
- `POST /api/admin/login`
- `POST /api/admin/logout`

## Development

```bash
uv sync
uv run ruff check .
uv run pytest
```
