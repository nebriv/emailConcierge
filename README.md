# emailConcierge

Self-hosted IMAP-to-CalDAV event extractor. Watches an IMAP inbox read-only and extracts
events through a tiered pipeline: `.ics` parsing, vendor plugins, ML (Phase 5+), and an
LLM fallback. See `CLAUDE.md` for the full spec.

## Safety

IMAP access is **strictly read-only**, enforced at four layers (see `CLAUDE.md` section 4):

1. Folders opened via `EXAMINE` (not `SELECT`) — server-side guarantee.
2. Every `FETCH` uses `BODY.PEEK` (`mark_seen=False`).
3. `ReadOnlyMailbox` is the only class in the codebase permitted to import
   `imap_tools`; it deliberately omits `delete`, `move`, `copy`, `append`,
   `expunge`, `seen`, `flag`, `store`, etc.
4. `tests/test_imap_readonly.py` records every IMAP command issued during a
   full listener cycle and fails CI if any mutating command appears.

A dedicated app password is strongly recommended. Scope it read-only where
your provider supports it (Fastmail, some Microsoft accounts).

## Quickstart

```bash
cp .env.example .env
# edit .env with your IMAP + CalDAV credentials
docker compose build
docker compose up
```

## Commands

```
python -m email_concierge run                              # live IMAP listener
python -m email_concierge backfill --folder=Archive \      # bulk ingest a folder
    [--since=YYYY-MM-DD] [--max=N] [--write-to-caldav]
python -m email_concierge import-training --from-google \  # harvest labeled pairs
    [--since=YYYY-MM-DD] [--limit=N] [--resolve-plids]     #   from Google Calendar
python -m email_concierge train ...                        # (Phase 5)
python -m email_concierge evaluate ...                     # (Phase 5)
python -m email_concierge metrics                          # (Phase 4)
python -m email_concierge export-fixtures                  # (Phase 2)
```

### `backfill`

Runs the live pipeline over a historical IMAP folder, strictly read-only.
Each message produces a `training_examples` row (what Phase 5 trains on).

- `--folder` is required (e.g. `INBOX`, `Archive`).
- `--since` defaults to 2 years ago.
- `--max` caps processing — recommended on first runs against large archives.
- `--write-to-caldav` is **off by default**. We usually only care about
  labeled training rows, not about backfilling the calendar with years of
  old events.

### `import-training --from-google`

One-off read-only import that pairs Google Calendar's auto-extracted events
with their source Gmail messages to produce pre-labeled `(email, event)`
rows. See [`docs/google-training-import.md`](docs/google-training-import.md)
for OAuth setup.

## LLM backends

Stage 4 uses any OpenAI-compatible chat-completions endpoint — only
`base_url`, `api_key`, and `model` change between providers.

| Backend | `LLM_BASE_URL` | Notes |
|---|---|---|
| Ollama (local) | `http://ollama:11434/v1` | Default. No cost, needs a GPU/CPU big enough for the model you pick. |
| RunPod serverless vLLM | `https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1` | Pay-per-second, scales to zero. See below. |
| OpenRouter | `https://openrouter.ai/api/v1` | Frontier models. |
| Anthropic via proxy | via any OpenAI-compat gateway | No Anthropic-native SDK dependency. |

### RunPod serverless vLLM

Useful when you don't want a GPU in the box that runs this service. Deploy
a vLLM worker from the RunPod console; in the endpoint's env-var panel set:

- `MODEL_NAME=qwen/qwen3-8b` (or any instruct-tuned chat model)
- `RAW_OPENAI_OUTPUT=true` (**required** — surfaces the `/openai/v1/*`
  proxy paths so existing OpenAI SDK clients work unchanged)
- `OPENAI_RESPONSE_ROLE=assistant`
- `ENABLE_AUTO_TOOL_CHOICE=false`

Then set in `.env`:

```bash
EMAIL_CONCIERGE_LLM_BASE_URL=https://api.runpod.ai/v2/<ENDPOINT_ID>/openai/v1
EMAIL_CONCIERGE_LLM_API_KEY=<runpod-api-key>
EMAIL_CONCIERGE_LLM_MODEL=qwen/qwen3-8b
EMAIL_CONCIERGE_LLM_TIMEOUT_SECONDS=180  # first-request cold start
```

vLLM's guided-decoding backend honors `response_format={"type":"json_object"}`,
so `LlmExtractor` works unchanged; no RunPod-specific code path.

FlashBoot keeps warm checkpoints around — after the first request, cold
starts are typically ~2s.

## Development

```bash
pip install -e '.[dev]'
ruff check .
pytest
```
