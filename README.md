# LLM Router

A self-hosted proxy that sits between any Anthropic SDK client (Claude Code, Python SDK, any AI agent) and any OpenAI-compatible upstream provider. From the client'\''s perspective it looks exactly like Anthropic'\''s API. From the upstream'\''s perspective it looks like a normal OpenAI client. Everything in between — authentication, routing, format translation, streaming — is handled invisibly.

## What It Does

- **Zero client-side changes** — Point Claude Code or any Anthropic SDK at the router and it just works
- **Multi-provider routing** — Route different model names to different upstream providers
- **Format translation** — Automatically converts Anthropic API calls to OpenAI format and back
- **Streaming support** — Full SSE streaming with exact Anthropic event sequence
- **Security** — Two independent auth layers, encrypted API keys, timing-safe comparisons
- **Tiny footprint** — Runs on GCP'\''s free e2-micro (1 GB RAM) with headroom to spare

## Quick Start

### 1. Create a GCP VM

```bash
gcloud compute instances create llm-router --zone=us-central1-a --machine-type=e2-micro --image-family=ubuntu-2204-lts --boot-disk-size=20GB --no-address --no-service-account
```

### 2. Install

```bash
gcloud compute ssh llm-router --zone=us-central1-a --tunnel-through-iap
git clone https://github.com/SreekarGpalli/llm-router.git
cd llm-router
sudo bash install.sh
```

### 3. Configure providers in the UI

Open your domain and add upstream providers (Groq, Together, OpenRouter, Ollama).

### 4. Point your client

```bash
export ANTHROPIC_BASE_URL=https://router.yourdomain.com/v1
export ANTHROPIC_API_KEY=sk-router-...
```

## Architecture

### Authentication

- **Virtual API Key** — Protects /v1/* endpoints. SHA-256 hash stored, Fernet for UI display.
- **UI Session Cookie** — HttpOnly, SameSite=Strict, 24-hour expiry using itsdangerous.TimestampSigner.
- **Upstream Keys** — Encrypted with Fernet, never stored in plaintext.

### Routing

Model alias table in SQLite resolves incoming Anthropic model names to upstream OpenAI model names. Supports exact match and default fallback.

### Translation

Translates between Anthropic and OpenAI formats:
- System prompts, tool definitions, tool use in history
- Image blocks (base64/URL)
- Response streaming with exact Anthropic SSE sequence

### Memory

Runs on 1 GB RAM e2-micro:
- Total peak ~340 MB
- Free headroom ~680 MB + 512 MB swap

### Network

Zero exposed ports — VM has no public IP. All traffic through Cloudflare Tunnel.

## Tech Stack

- FastAPI + Uvicorn
- httpx (async HTTP)
- SQLite (WAL mode)
- Fernet encryption
- Cloudflare Tunnel

## Why This Matters

Demonstrates protocol translation, security engineering, performance optimization, streaming handling, and zero-trust infrastructure.

## License

MIT
