# Sunnify Backend

Lightweight Flask API that fetches Spotify playlist, album, artist discography, and track **metadata** (no audio). Powers the [web client](../sunnify-webclient); for actual MP3 downloads, use the desktop app or its bundled CLI.

Optimized for free-tier hosting (512MB RAM, 0.1 CPU): a single reusable client, aggressive GC, metadata-only responses.

## Endpoints

| Method | Path | Purpose |
| :--- | :--- | :--- |
| `POST` | `/api/scrape-playlist` | Resolve a playlist/album/artist/track URL to its track metadata |
| `GET` | `/api/health` | Liveness probe (`{"status":"ok"}`) |
| `GET` | `/` | Service info + endpoint list |

`POST /api/scrape-playlist` body: `{"playlistUrl": "https://open.spotify.com/..."}` (playlist, album, artist, or track URL / `spotify:` URI).

For multiple sources, send `{"playlistUrls": ["spotify:artist:...", "spotify:album:..."]}`
or a whitespace/comma-separated list in `playlistUrl`. The API validates the whole
list first, ignores duplicate resources, and fetches each URL in order. Batch
responses combine unique tracks and include `data.errors` with a `url` and safe
error `message` for each failed source; successful sources are still returned.
The response shape for a single unique URL is unchanged.

## Run locally

```bash
pip install -r requirements.txt
python app.py            # dev server on :5000 (PORT overridable)
gunicorn app:app         # production (matches Procfile / Render)
```

## Deploy

Deployed on Render via `Procfile` (`web: gunicorn app:app`). The repo's [health-check workflow](../../.github/workflows/render-health.yml) pings `/api/health` every 6h to monitor uptime and reduce cold starts.

Shares the Spotify embed-API client (`spotifydown_api.py`) with the desktop app, so no credentials are required.
