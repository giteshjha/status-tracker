import asyncio
import random
from datetime import datetime, timezone

import httpx

# ── Config ─────────────────────────────────────────────────────────────────────

BASE_POLL_INTERVAL = 60   # seconds
MAX_POLL_INTERVAL  = 600  # backoff ceiling

PROVIDERS = [
    {"url": "https://status.openai.com",       "name": "OpenAI",    "poll_interval": 60},
    # {"url": "https://www.githubstatus.com",  "name": "GitHub",    "poll_interval": 30},
    # {"url": "https://status.anthropic.com",  "name": "Anthropic", "poll_interval": 60},
]

# One timestamp per provider — O(1) storage, lost on restart (acceptable).
# Swap for Redis only if multi-worker or stateless deployment is needed.
watermarks: dict[str, datetime] = {}


# ── Poller ─────────────────────────────────────────────────────────────────────

async def _poll(client: httpx.AsyncClient, url: str, name: str, etag: str | None) -> str | None:
    # Conditional request — server returns 304 + no body if nothing changed (~95% bandwidth saved)
    headers = {"If-None-Match": etag} if etag else {}

    try:
        resp = await client.get(f"{url}/api/v2/incidents.json", headers=headers)
    except Exception as exc:
        raise RuntimeError(f"{name}: network error — {exc}") from exc

    if resp.status_code == 304:
        return etag

    if resp.status_code != 200:
        raise RuntimeError(f"{name}: unexpected status {resp.status_code}")

    # Parse before updating ETag — prevents silently skipping this version on parse failure
    try:
        data = resp.json()
    except Exception as exc:
        raise RuntimeError(f"{name}: JSON parse error — {exc}") from exc

    new_etag = resp.headers.get("ETag", etag)

    last_ts   = watermarks.get(name, datetime.min.replace(tzinfo=timezone.utc))
    newest_ts = last_ts

    # On first run, silently advance watermark without printing history.
    # Without this, every historical incident fires on startup as there is
    # no prior watermark — producing a flood of old/empty updates.
    is_first_run = name not in watermarks

    for incident in data.get("incidents", []):
        components = [c["name"] for c in incident.get("components", [])]
        product = ", ".join(components) if components else name

        for update in incident.get("incident_updates", []):
            update_ts = datetime.fromisoformat(update["updated_at"].replace("Z", "+00:00"))

            if update_ts > last_ts:
                # Skip printing on first run and skip updates with empty body
                if not is_first_run and update["body"].strip():
                    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Product: {product}")
                    print(f"Status:  {update['body']}\n")
                newest_ts = max(newest_ts, update_ts)

    if newest_ts > last_ts:
        watermarks[name] = newest_ts

    return new_etag


# ── Tracker ────────────────────────────────────────────────────────────────────

async def _track(client: httpx.AsyncClient, url: str, name: str, poll_interval: int) -> None:
    await asyncio.sleep(random.uniform(0, poll_interval))  # jitter — stagger startup across 500 tasks
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Tracking {name}  →  {url}")

    etag: str | None = None
    consecutive_failures = 0

    while True:
        try:
            etag = await _poll(client, url, name, etag)
            consecutive_failures = 0
            await asyncio.sleep(poll_interval)

        except asyncio.CancelledError:
            break  # clean shutdown — never `pass`, that breaks asyncio cancellation

        except Exception as exc:
            consecutive_failures += 1
            backoff = min(poll_interval * (2 ** consecutive_failures), MAX_POLL_INTERVAL)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [ERROR] {exc} — retry in {backoff}s")
            await asyncio.sleep(backoff)


# ── Entry point ────────────────────────────────────────────────────────────────

async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [
            asyncio.create_task(
                _track(client, p["url"], p["name"], p.get("poll_interval", BASE_POLL_INTERVAL))
            )
            for p in PROVIDERS
        ]
        try:
            await asyncio.Event().wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
