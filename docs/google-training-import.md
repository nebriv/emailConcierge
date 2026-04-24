# Importing training data from Google Calendar + Gmail

`email-concierge` can harvest labeled `(email, event)` pairs from your
own Google account to bootstrap the Phase 5 classifier without manual
annotation. Google Calendar has, for years, been auto-extracting events
from booking emails (flights, hotels, restaurants, concerts) — the
events labeled *"Automatically added from Gmail"*. Each such event
links back to the source Gmail message. Pairing them gives us a few
hundred high-quality positive training examples in one shot.

**This is strictly read-only.** The only scopes requested are
`calendar.readonly` and `gmail.readonly`. Nothing is written to Google
Calendar, nothing is modified in Gmail. The code enforces this at two
layers:

1. A scope allowlist in `integrations/google/auth.py` raises
   `ScopeNotAllowed` if any other scope is requested.
2. A ruff TID rule forbids importing `googleapiclient`,
   `google.oauth2`, or `google_auth_oauthlib` anywhere outside
   `integrations/google/*` — the same pattern that locks down
   `imap_tools` to the `ReadOnlyMailbox` wrapper.

---

## One-time setup: create a Google Cloud OAuth client

You need to create your own OAuth client — we deliberately don't ship a
shared one so every self-hoster uses their own quota and nobody's
inbox is behind our creds.

1. Open the [Google Cloud Console](https://console.cloud.google.com/)
   and create a new project (or reuse one you have).
2. In **APIs & Services → Library**, enable both:
   - *Google Calendar API*
   - *Gmail API*
3. In **APIs & Services → OAuth consent screen**:
   - User type: **External** (required for personal Google accounts).
   - Add your own email as a test user under *Test users*. While the
     app is in Testing mode only test users can authorize — that's
     fine, we only ever authorize one user.
   - Scopes: you can leave these blank on the consent screen page;
     the scopes are requested at consent time by the library.
4. In **APIs & Services → Credentials**:
   - Click **Create Credentials → OAuth client ID**.
   - Application type: **Desktop app**.
   - Name it anything (e.g. `email-concierge`).
   - Click **Create**, then **Download JSON**. You now have a
     `client_secret_*.json` file.

## Supplying the secrets

You have two options. Pick one.

### Option A — inline environment variable (no file on disk)

Paste the full JSON content as a single-line value in your `.env`:

```
GOOGLE_CALENDAR_OAUTH_JSON={"installed":{"client_id":"...","client_secret":"...",...}}
```

Either that name or the fully-prefixed
`EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_JSON` is accepted.

### Option B — file path

```
EMAIL_CONCIERGE_GOOGLE_CLIENT_SECRETS_PATH=/data/google_client_secrets.json
```

and place the downloaded JSON at that path.

If both are set, the inline env var wins.

## Running the import

```bash
python -m email_concierge import-training --from-google
```

On first invocation:
- A browser tab opens pointing at Google's consent screen.
- You pick your Google account and click through.
- The library captures the redirect on a local loopback port and
  persists the resulting token to `EMAIL_CONCIERGE_GOOGLE_TOKEN_PATH`
  (default `/data/google_token.json`, stored with `0o600` on POSIX).

Subsequent invocations reuse the cached token and refresh it as needed
— no browser prompt after the first time.

Options:

| Flag | Default | Purpose |
|---|---|---|
| `--since YYYY-MM-DD` | 2 years ago (first run only) | Only import events starting after this date. Overrides the stored cursor. |
| `--limit N` | unlimited | Stop after N new rows are written. Useful for a first sanity check. |

### What gets written

Each paired event produces:

- One row in `processed_messages` with `status='imported_from_google'`
  and `handled_by_name='google_calendar_import'`.
- One row in `training_examples` with `label='event'`,
  `label_source='google'`. The `extracted_json` column holds the
  Google-parsed event (title, start, end, location) plus the Gmail
  internal message ID for later re-fetch.

Re-runs are incremental: the `google_sync_state` table persists an
`updatedMin` cursor that advances to the max `event.updated` seen.
Already-imported messages are skipped via the `UNIQUE(message_id)`
constraint on `training_examples` — safe to re-run at any time.

### Sanity-check the output

```bash
sqlite3 /data/email-concierge.db \
  "SELECT sender, subject,
          json_extract(extracted_json, '$.event.title') AS extracted_title,
          json_extract(extracted_json, '$.event.location') AS extracted_loc
   FROM training_examples
   WHERE label_source = 'google'
   LIMIT 10"
```

## Running the import on a prod / containerized deployment

The OAuth consent flow starts a local HTTP server on a random free
port bound to `127.0.0.1` *inside the container*
(`flow.run_local_server(port=0)` in
[`integrations/google/auth.py`](../email_concierge/integrations/google/auth.py)).
Docker port mapping can't reach that bind, and the random port
wouldn't match a fixed `-p` anyway, so you can't just SSH-tunnel into
consent on a prod container. (Google itself is happy to redirect to
`http://localhost:<port>` through an SSH tunnel — loopback is trusted
for Desktop-app clients — but the library's defaults get in the way
first.)

Easiest path: **seed the token from a machine that does have a
browser**, then copy the token into the prod volume. The token
includes a refresh grant that doesn't expire unless Google revokes it
or you rotate, so this is a one-time setup.

### Seeding the token from a dev machine

1. On a workstation with a browser, point the same
   `GOOGLE_CALENDAR_OAUTH_JSON` at your dev `.env` and run:
   ```bash
   python -m email_concierge import-training --from-google --limit=1
   ```
   Click through the consent screen. This produces
   `data/google_token.json` (refresh-token included — it doesn't
   expire on its own, only if Google revokes it or you rotate).
2. Copy that token into the prod container's volume:
   ```bash
   # For the docker-compose.prod.yaml named volume:
   docker cp data/google_token.json <stack-container>:/data/google_token.json
   # Or with a helper container if the main one isn't running yet:
   docker run --rm -v email_concierge_data:/data -v "$PWD/data":/src \
     alpine cp /src/google_token.json /data/google_token.json
   ```
3. Run the import inside the prod container:
   ```bash
   docker exec -it <stack-container> python -m email_concierge \
     import-training --from-google
   ```
   The cached token loads, auto-refreshes as needed, and never opens
   a browser.

The `GOOGLE_CALENDAR_OAUTH_JSON` env var still needs to be set on the
prod stack — it's used for the refresh grant, and to re-run the
consent flow if the token is ever invalidated (in which case you'd
just re-seed it the same way).

### Scheduling periodic imports

Training data import isn't a daemon — it's one-shot. To keep the
training corpus growing, cron it on the host:

```cron
# Weekly catch-up — stays idempotent via the UNIQUE(message_id)
# constraint and the google_sync_state cursor.
15 3 * * 0  docker exec <stack-container> python -m email_concierge import-training --from-google
```

Or use Arcane's stack-local task scheduler if you have one configured.

### Volume persistence

The prod compose file (`docker-compose.prod.yaml`) mounts the named
volume `email_concierge_data` at `/data`. Everything Google-related
lives there:

| Path | What | Survives redeploy? |
|---|---|---|
| `/data/email-concierge.db` | SQLite — training_examples, processed_messages, google_sync_state | yes |
| `/data/google_token.json` | Cached OAuth token (refresh included) | yes |
| `/data/google_client_secrets.json` | Optional on-disk client JSON (skip if using the env var) | yes |

Back it up with:

```bash
docker run --rm -v email_concierge_data:/data -v "$PWD":/backup \
  alpine tar czf /backup/email-concierge-$(date +%F).tgz -C /data .
```

## Revoking access

If you uninstall or rotate:

1. Visit [Google Account → Third-party
   access](https://myaccount.google.com/permissions) and revoke the
   OAuth client.
2. Delete `google_token.json` (whichever path you configured).
3. In Google Cloud Console, you can also delete the OAuth client
   entirely if you don't plan to use it again.
