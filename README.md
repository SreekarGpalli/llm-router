# LLM Router

A self-hosted, zero-cost Anthropic-compatible LLM proxy that routes requests to any OpenAI-compatible upstream provider (Groq, Together, OpenRouter, Ollama, etc.) based on user-defined model aliases. Claude Code and the Anthropic SDK point at it without any client-side changes.

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  Claude Code / Anthropic SDK                                                  │
│  ANTHROPIC_BASE_URL = https://router.yourdomain.com/v1                        │
│  ANTHROPIC_API_KEY  = sk-router-<key>                                         │
│         │                                                                     │
│         ▼  POST /v1/messages   model: "claude-opus-4-5"                        │
│  ┌─────────────────────────────────────────────────────────────────────────┐  │
│  │  LLM Router (GCP e2-micro)                                              │  │
│  │                                                                         │  │
│  │  1. Validate sk-router key (SHA-256 compare)                            │  │
│  │  2. Look up "claude-opus-4-5" in alias table → Groq / llama-3.1-70b    │  │
│  │  3. Translate Anthropic → OpenAI format                                 │  │
│  │  4. Stream from https://api.groq.com/openai/v1/chat/completions         │  │
│  │  5. Translate OpenAI SSE → Anthropic SSE, echo alias name               │  │
│  └─────────────────────────────────────────────────────────────────────────┘  │
│         ▼  Cloudflare Tunnel (outbound-only, no open ports)                   │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

- **GCP account** with Always Free tier (us-central1 e2-micro, 30 GB standard disk)
- **Cloudflare account** with a domain whose DNS is managed by Cloudflare

---

## 1. Create the GCP VM

1. Go to **Compute Engine → VM Instances → Create**.
2. Region: **us-central1**, zone: us-central1-a (Always Free eligible).
3. Machine type: **e2-micro** (2 vCPU, 1 GB RAM).
4. Boot disk: **Ubuntu 22.04 LTS**, standard persistent disk, **20 GB**.
5. Under *Identity and API access*: choose "No service account".
6. Under *Networking*: **uncheck** "Allow HTTP traffic" and "Allow HTTPS traffic" — we use Cloudflare Tunnel, not GCP load balancers.
7. Click **Create**.

**Firewall rules (critical):**

Delete the default `default-allow-http` and `default-allow-https` rules if they exist, then create:

```
Name: allow-iap-ssh
Direction: Ingress
Target: All instances in network
Source IP ranges: 35.235.240.0/20   ← Google IAP range only
Protocols/ports: tcp:22
```

No other inbound rules. Port 8000 (Uvicorn) is never exposed.

**Connect via IAP:**
```bash
gcloud compute ssh <instance-name> --zone us-central1-a --tunnel-through-iap
```

---

## 2. One-line install

SSH into the VM, clone the repo, and run:

```bash
git clone https://github.com/your-org/llm-router.git
cd llm-router
sudo bash install.sh
```

The script will:
1. Install Python 3.11, configure a 512 MB swapfile, harden SSH
2. Enable Fail2Ban and unattended security upgrades
3. Disable snapd and ModemManager
4. Prompt for 4 values (secret key, UI password, Cloudflare domain, port)
5. Generate your virtual API key and display it **once**
6. Download cloudflared, authenticate interactively, create the tunnel
7. Start both `llm-router` and `cloudflared` systemd services

**Total interactive steps**: 4 prompts + 1 browser tab for Cloudflare auth.

---

## 3. Post-install: add your first provider

Open the UI via an SSH tunnel (more secure than exposing port 80):

```bash
# On your local machine:
gcloud compute ssh <instance-name> --zone us-central1-a --tunnel-through-iap -- -L 8080:localhost:8000
# Then open: http://localhost:8080
```

Or if the Cloudflare Tunnel is up, just go to `https://router.yourdomain.com` from any browser.

1. Log in with the `UI_PASSWORD` you set during install.
2. Click **+ Add Provider**.
3. Fill in:
   - **Nickname**: `Groq`
   - **Base URL**: `https://api.groq.com/openai/v1`
   - **API Key**: your Groq API key
4. Add alias rows, for example:

   | Anthropic Model Name   | Upstream Model Name           | Default |
   |------------------------|-------------------------------|---------|
   | claude-opus-4-5        | llama-3.1-70b-versatile       |         |
   | claude-sonnet-4-5      | llama-3.1-8b-instant          | ✓       |
   | claude-haiku-4-5       | gemma2-9b-it                  |         |

5. Click **Save Provider**.

Changes take effect immediately — no restart needed.

---

## 4. Configure Claude Code

```bash
export ANTHROPIC_BASE_URL=https://router.yourdomain.com/v1
export ANTHROPIC_API_KEY=sk-router-<your-key>
```

Or add to `~/.bashrc` / `~/.zshrc`. Then:

```bash
claude  # Claude Code CLI
```

To verify:
```bash
curl https://router.yourdomain.com/health
# {"status":"ok","providers":1,"aliases":3}

curl -X POST https://router.yourdomain.com/v1/messages \
  -H "x-api-key: sk-router-<your-key>" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5","max_tokens":50,"messages":[{"role":"user","content":"Hello"}]}'
```

---

## 5. Adding a local Ollama upstream

If you run Ollama on the same VM:

- **Nickname**: `Ollama Local`
- **Base URL**: `http://localhost:11434/v1`
- **API Key**: *(leave blank)*
- Aliases: `claude-haiku-4-5` → `llama3.2:3b`

Pass-through mode: leave the upstream model name blank to forward the exact name the client sent.

---

## 6. Rotating the virtual API key

In the UI, click **Regenerate** in the API Key card. The new key is shown once — copy it. The old key is immediately invalid.

Or directly:
```bash
curl -X POST https://router.yourdomain.com/api/key/regenerate \
  -b "llm_router_session=<your-session-cookie>"
```

---

## 7. Changing the UI password

```bash
sudo nano /opt/llm-router/.env
# Edit: UI_PASSWORD=new-password
sudo systemctl restart llm-router
```

---

## 8. Monitoring

```bash
# Logs (live)
journalctl -u llm-router -f

# Memory usage
ps aux | grep uvicorn

# Swap usage
free -h

# Service status
systemctl status llm-router cloudflared
```

---

## 9. Troubleshooting

**Alias not found error**
```json
{"type":"error","error":{"type":"invalid_request_error","message":"No alias found for model '...' and no default alias is configured."}}
```
→ Add the alias in the UI, or mark an existing alias as **Default** to catch any unrecognised model.

**Upstream 401 error**
→ The API key for that provider is wrong. Edit the provider in the UI and re-enter the key (the old encrypted key is replaced only when you provide a new one).

**Streaming hangs / timeout**
→ The upstream provider may be slow or overloaded. Check `journalctl -u llm-router -f` for connection errors. The router has a 30 s connection timeout for streaming requests but no read timeout (long agentic streams can run for minutes).

**OOM / service keeps restarting**
→ Check `journalctl -u llm-router -n 50` for `MemoryMax exceeded`. The systemd unit kills at 400 MB RSS and restarts automatically. Ensure swap is active (`free -h`). If problems persist, reduce concurrent request load.

**Cloudflare tunnel not connecting**
→ Check `journalctl -u cloudflared -f`. Ensure `$CF_DIR/config.yml` has the correct tunnel ID. Re-authenticate if needed: `cloudflared tunnel login`.

**"Provider not found" but alias exists**
→ Check if the provider is enabled (toggle in the UI). Disabled providers return an explicit error.

---

## Architecture notes

| Component          | RAM footprint | Notes                                      |
|--------------------|---------------|--------------------------------------------|
| Python 3.11        | ~25 MB        | Interpreter baseline                       |
| FastAPI + Uvicorn  | ~35 MB        | Single worker, h11 HTTP                    |
| httpx + crypto     | ~20 MB        | async HTTP client + Fernet                 |
| SQLite (WAL)       | ~5 MB         | Local disk, no external DB                 |
| cloudflared        | ~25 MB        | Separate process, outbound-only tunnel     |
| **Total idle**     | **~110 MB**   | Ubuntu OS adds ~220 MB                     |
| **Peak streaming** | **~150 MB**   | Well within 400 MB systemd limit           |
| **VM headroom**    | **~630 MB**   | + 512 MB swap = effectively OOM-proof      |

---

## File structure

```
llm-router/
├── main.py              FastAPI app — all proxy + UI + API endpoints
├── db.py                SQLite schema, CRUD, alias resolution
├── translator.py        Anthropic ↔ OpenAI translation (streaming + sync)
├── router.py            Alias lookup and upstream resolution
├── crypto.py            Fernet encrypt/decrypt for upstream API keys
├── auth.py              Session cookie + rate limiter
├── static/
│   └── index.html       Complete UI SPA (HTML + CSS + JS, offline-capable)
├── requirements.txt     Pinned Python dependencies (7 packages)
├── .env.example         All configuration variables with comments
├── llm-router.service   systemd unit with MemoryMax=400M, Restart=always
├── cloudflared.service  systemd unit for Cloudflare Tunnel
├── install.sh           Bootstrap script (4 prompts, under 5 minutes)
└── README.md            This file
```
