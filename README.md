# emailConcierge

Self-hosted IMAP-to-CalDAV event extractor. Watches an IMAP inbox read-only and extracts
events through a tiered pipeline: `.ics` parsing, vendor plugins, ML (Phase 5+), and an
LLM fallback. See `CLAUDE.md` for the full spec.

## Safety

IMAP access is **strictly read-only**, enforced at four layers (see `CLAUDE.md` section 4).
No flag, move, copy, delete, append, expunge, or store operation is ever issued.

## Quickstart (Phase 1)

```bash
cp .env.example .env
# edit .env with your IMAP + CalDAV credentials
docker compose build
docker compose up
```

## Commands

```
python -m email_concierge run              # live listener
python -m email_concierge backfill ...     # (Phase 4)
python -m email_concierge train ...        # (Phase 5)
python -m email_concierge evaluate ...     # (Phase 5)
python -m email_concierge metrics          # (Phase 4)
python -m email_concierge export-fixtures  # (Phase 2)
```

## Development

```bash
pip install -e '.[dev]'
ruff check .
pytest
```
