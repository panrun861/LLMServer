# AITube-PLLM

OpenAI-compatible private LLM gateway with PLLM token authentication, model tiers,
usage accounting, audit logs, multimodal messages, and function calling passthrough.

## Local Docker setup

1. Copy `.env.example` to `.env`.
2. Put local database and LiteLLM credentials in `.env`.
3. Start the service:

```bash
docker compose up -d --build
```

The API listens on `http://localhost:8080` by default. Swagger documentation is
available at `http://localhost:8080/docs`.

`.env`, local compose overrides, private keys, logs, generated API test files, and
virtual environments are intentionally excluded from Git.