LLM Wrapper (llama.cpp) — README

## What this repo contains

- `wrapper.py` — A FastAPI wrapper that proxies client requests and hides the real upstream model identity. It exposes OpenAI-compatible endpoints: `/v1/chat/completions`, `/v1/models`, `/health`, and `/stats`.
- `Dockerfile` — (expected) Dockerfile for the LLM (llama.cpp-based) server. Edit as needed for your llama.cpp setup.
- `Dockerfile.wrapper` — Dockerfile for the wrapper service (runs `wrapper.py`).
- `docker-compose.yml` — Optional compose file (you can use the included file or the example below).
- `requirements.txt` and `requirements-wrapper.txt` — Python dependencies used by the services.

Goal: Build two separate Docker images — one running your llama.cpp-based LLM server (that exposes an OpenAI-compatible `/v1/chat/completions` endpoint) and one running the wrapper (`wrapper.py`) that forwards requests to the LLM and masks the model name.

## Quick overview and contract

- Inputs: Client sends POST /v1/chat/completions with a JSON body containing `messages` (a non-empty list).
- Outputs: Wrapper returns the upstream LLM response with the real model name redacted and replaced with `PUBLIC_MODEL_NAME`.
- Error modes: 503 if backend unreachable, 4xx for invalid requests, upstream 5xx/4xx proxied as errors.

The wrapper enforces a few safety behaviors (probe detection, sensitive-pattern redaction). See "Environment variables" below to control behavior.

## Required environment variables (wrapper)

Set these for the wrapper container; reasonable defaults are used in `wrapper.py`, but you should set them explicitly in production:

- `INTERNAL_LLM_URL` — The full URL (including path) where the llama.cpp server exposes the OpenAI-compatible completions endpoint. Example: `http://llm:7000/v1/chat/completions` or `http://192.168.0.42:7000/v1/chat/completions`.
- `INTERNAL_MODEL` — The name/key of the model to pass to the upstream server. For llama.cpp-based servers this may be the identifier the server expects (for instance the filename or model id used by your server).
- `PUBLIC_MODEL_NAME` — What the wrapper returns to clients as the model name (e.g., `surya-01`).
- `BACKEND_IDENTITY` — The identity string to present if the model is probed (e.g., `LLaMA-like` or `DONT KNOW`). Used inside the `SYSTEM_GUARD` default.
- `SYSTEM_GUARD` — Optional system guard content (string). When set, it gets prepended as instructions to the model to avoid revealing internal details. If you want the wrapper to preserve complete fidelity to upstream, unset/empty this.
- `LOG_FILE` — Path for rotating logs (default: `./logs/requests.log`).
- `LOG_LEVEL` — Logging level (e.g., `INFO`, `DEBUG`).
- `WRAPPER_PORT` — Port for the wrapper server (default in `wrapper.py` is `9001`).

Important: `wrapper.py` requires that incoming requests include `messages` as a non-empty list. The wrapper will return HTTP 400 if that field is missing or invalid.

## Build the images

Below are the minimal docker build commands. The repository already contains `Dockerfile` and `Dockerfile.wrapper`; adapt them if your LLM server uses a different base image or servable.

Build the LLM (llama.cpp) image (example tag `llm-llamacpp`):

```bash
# from repo root
docker build -f Dockerfile -t llm-llamacpp:latest .
```

Build the wrapper image (uses `Dockerfile.wrapper`):

```bash
docker build -f Dockerfile.wrapper -t llm-wrapper:latest .
```

If you don't have those Dockerfiles or want a quick Python-only wrapper image, a minimal `Dockerfile.wrapper` should install `requirements-wrapper.txt` and run `uvicorn wrapper:app --host 0.0.0.0 --port $WRAPPER_PORT`.

## Run locally with `docker run`

This example assumes the LLM server listens on port 7000 inside its container and exposes a `/v1/chat/completions` endpoint.

1) Start the LLM container (replace with your own runtime for llama.cpp server):

```bash
docker run -d --name llm -p 7000:7000 llm-llamacpp:latest
```

2) Start the wrapper container and point it at the LLM by URL.

If both containers are on the same Docker network (recommended), use the container name as hostname (e.g. `http://llm:7000/...`). When running with `--network` you can omit `-p` for internal-only wiring or expose the wrapper port to your host.

Example (exposes wrapper to host port 8001):

```bash
docker run -d --name llm-wrapper \
  --link llm:llm \
  -p 8001:9001 \
  -e INTERNAL_LLM_URL="http://llm:7000/v1/chat/completions" \
  -e INTERNAL_MODEL="your-llama-model-id" \
  -e PUBLIC_MODEL_NAME="surya-01" \
  -e BACKEND_IDENTITY="LLaMA-like" \
  -e WRAPPER_PORT=9001 \
  llm-wrapper:latest
```

Notes:
- The wrapper's default port inside the container is controlled by `WRAPPER_PORT`. `wrapper.py` uses `uvicorn.run(..., port=int(os.environ.get("WRAPPER_PORT", "9001")))` when run directly.
- Use `--link` or better, a user-defined Docker network to resolve `llm` by name.

## Example docker-compose (recommended)

You can create a `docker-compose.override.yml` or use the provided `docker-compose.yml`. Here is a minimal example you can adapt:

```yaml
version: "3.8"
services:
  llm:
    image: llm-llamacpp:latest
    container_name: llm
    # map host port if you want to reach it from outside
    ports:
      - "7000:7000"
    # environment, volumes and command depend on how you expose the llama.cpp server

  wrapper:
    image: llm-wrapper:latest
    container_name: llm-wrapper
    ports:
      - "8001:9001"  # expose wrapper to host: clients connect to 8001
    environment:
      INTERNAL_LLM_URL: "http://llm:7000/v1/chat/completions"
      INTERNAL_MODEL: "your-llama-model-id"
      PUBLIC_MODEL_NAME: "surya-01"
      BACKEND_IDENTITY: "LLaMA-like"
      WRAPPER_PORT: "9001"
    depends_on:
      - llm
    networks:
      - llm-net

networks:
  llm-net:
    driver: bridge
```

Run both services:

```bash
docker-compose up --build -d
```

## API examples (try it)

List models (quick check wrapper identity):

```bash
curl -s http://localhost:8001/v1/models | jq
```

Chat completion (non-streaming):

```bash
curl -s -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Hello"}], "stream": false}' | jq
```

Streaming example (server must support streaming):

```bash
curl -N -X POST http://localhost:8001/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Stream me"}], "stream": true}'
```

## What `wrapper.py` expects from the upstream LLM

- An OpenAI-compatible JSON API under the URL you set in `INTERNAL_LLM_URL` (the wrapper POSTs the same `messages` and additional fields such as `temperature`, `max_tokens`, etc.).
- For streaming, a server that returns Server-Sent Events (SSE) with lines that start with `data: ` and contain JSON.

If your llama.cpp toolchain doesn't expose an OpenAI-compatible REST interface natively, you will need a small adaptor service (or use a project that already exposes such an interface) that accepts the same request shape and translates to llama.cpp calls.

## Logs and diagnostics

- Default log file: `./logs/requests.log` (rotates daily). Check `logs/errors.log` for warnings and probe events.
- Health: `GET /health` should return `{ "status": "ok" }`.
- Stats: `GET /stats` will show daily counts and totals.

## Troubleshooting

- Backend unreachable (503): Verify `INTERNAL_LLM_URL` is correct and the llama.cpp server is up. If using Docker, ensure both containers share a network and use service name resolution (no host firewall blocking).
- Probing blocked: The wrapper detects several phrases (see `PROBE_PHRASES` in `wrapper.py`) and returns a canned response. You can tune `PROBE_PHRASES` or `SYSTEM_GUARD` in the environment if desired.
- Model-name leakage: `SENSITIVE_PATTERNS` in `wrapper.py` is used to redact model names from upstream output. You can edit that regex if your upstream reports other identifiers.

## Security and production notes

- Keep `SYSTEM_GUARD` conservative if you must prevent identity leakage. However, adding guard text may slightly change outputs — balance safety vs fidelity.
- Run the wrapper behind TLS/HTTPS or a reverse proxy in production.
- Logs may contain chat content. Rotate and secure logs appropriately.

## Example minimal `Dockerfile.wrapper`

If you need a minimal wrapper Dockerfile for reference (do NOT overwrite your existing file unless you intend to):

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements-wrapper.txt ./
RUN pip install --no-cache-dir -r requirements-wrapper.txt
COPY wrapper.py ./
EXPOSE 9001
CMD ["/usr/local/bin/uvicorn", "wrapper:app", "--host", "0.0.0.0", "--port", "9001", "--proxy-headers"]
```

## Final checklist

- [ ] Ensure your llama.cpp-based server exposes a compatible `/v1/chat/completions` endpoint.
- [ ] Set `INTERNAL_LLM_URL` and `INTERNAL_MODEL` in the wrapper environment.
- [ ] Build and run the two images as separate containers (or via docker-compose).
- [ ] Test with `GET /health`, `GET /v1/models` and a `POST /v1/chat/completions`.

If you'd like, I can:
- Provide a complete, tested `Dockerfile` for an example llama.cpp server adapter (I will need details about which wrapper or adapter you intend to use), or
- Generate a `docker-compose.yml` tailored to your environment and desired ports.

Completion summary: Added this `README.md` with build/run instructions, environment variables, examples, and troubleshooting tips. Follow the steps above to build two separate Docker images (LLM + wrapper) and run them together.
