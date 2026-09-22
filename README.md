# Meme API

A tiny FastAPI service that returns a random image meme from Reddit. It keeps a
pool of hot-post image URLs in memory, refreshed in the background, pre-warms
the images into a byte-bounded in-memory cache, and serves each request
straight from RAM - sub-millisecond, ~10k req/s on one core, no database,
nothing to host besides the app. Put a CDN in front and embed it in a README:

```markdown
![random meme](https://your-host.example/)
```

## Endpoints

| Method | Path       | Description |
|--------|------------|-------------|
| GET    | `/`        | A random meme image (`image/png`, `image/jpeg`, `image/gif`, …). Adds `X-Meme-Title`, `X-Meme-Permalink`, `X-Meme-Subreddit` and `X-Cache: HIT\|MISS` headers. |
| HEAD   | `/`        | Same headers, no body. |
| GET    | `/meme`    | JSON metadata for a random meme (`title`, `url`, `permalink`, `subreddit`). |
| GET    | `/health`  | `healthy` when the pool has memes, `degraded` when it is empty. Always `200`. |
| GET    | `/stats`   | Pool size, images in memory, cache age and configured intervals. |
| POST   | `/refresh` | Force a refresh. Needs `Authorization: Bearer $REFRESH_TOKEN`; disabled unless `REFRESH_TOKEN` is set. `409` if one is already running, `502` if Reddit failed. |
| GET    | `/docs`    | Interactive OpenAPI docs. |

Errors are JSON (`{"error": "..."}`) and always sent with `Cache-Control: no-store`
so a CDN never pins a failure.

## Running

1. Create a Reddit **script** app at <https://www.reddit.com/prefs/apps> to get a
   client id and secret.
2. `cp .env.example .env` and fill in `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`
   and a descriptive `REDDIT_USER_AGENT`.
3. `docker compose up --build`

The API listens on <http://localhost:7860>. The first request populates the
pool if the background refresher has not finished yet.

### Without Docker

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export REDDIT_CLIENT_ID=... REDDIT_CLIENT_SECRET=...
uvicorn main:app --port 7860
```

## Memory footprint

The process is about 55 MB of Python + libraries plus at most `IMAGE_CACHE_BYTES`
of image data - roughly 90 MB worst case with the defaults, comfortably inside a
512 MB free instance. Raise `IMAGE_CACHE_BYTES` if you have RAM to spare and
want a larger warm pool; lower it (or `POOL_SIZE`) if you don't.

## Deploying on Render (free tier)

Create a **Web Service** from this repo (runtime: Docker) and set
`REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` in its environment. That's it:

* The container binds to Render's `$PORT`.
* Render idles free services after 15 minutes without inbound traffic. The app
  pings its own public URL (`$RENDER_EXTERNAL_URL/health`) every 10 minutes to
  prevent that - `RENDER_EXTERNAL_URL` is set by Render automatically.
* No database is needed, so nothing else to provision.

## Configuration

Everything is read from the environment (`.env` with docker compose).

| Variable | Default | Meaning |
|----------|---------|---------|
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | *(required)* | Reddit script-app credentials. |
| `REDDIT_USER_AGENT` | `meme-api/3.0 (self-hosted)` | Reddit asks for a unique, descriptive UA. |
| `SUBREDDITS` | `ProgrammerHumor` | Comma-separated subreddits to pull from. |
| `POSTS_PER_SUBREDDIT` | `100` | Hot posts inspected per subreddit. |
| `POOL_SIZE` | `100` | Meme URLs kept in memory. |
| `MAX_IMAGE_BYTES` | `2097152` (2 MB) | Larger images are skipped (limit enforced while streaming). |
| `IMAGE_CACHE_BYTES` | `33554432` (32 MB) | Hard cap on RAM used for cached image bytes (LRU); the warmer stops when it is full. |
| `WARM_CONCURRENCY` | `8` | Parallel downloads while pre-warming the cache after a refresh. |
| `SERVE_ATTEMPTS` | `3` | Dead links tried per request before returning `503`. |
| `REFRESH_INTERVAL` | `21600` (6 h) | How often the pool is replaced with fresh posts. |
| `REFRESH_BACKOFF` | `300` | Pause before retrying after a failed Reddit fetch. |
| `CDN_MAX_AGE` | `3600` | `Cache-Control: public, max-age=…` on served images. |
| `REFRESH_TOKEN` | *(unset)* | Enables `POST /refresh`. |
| `KEEPALIVE_URL` | auto on Render | Public URL to self-ping so a free-tier host never idles the service out. |
| `KEEPALIVE_INTERVAL` | `600` | Seconds between self-pings. |
| `PORT` | `7860` | Listen port (Docker image only; Render sets this). |
| `WEB_CONCURRENCY` | `1` | Uvicorn workers (Docker image only). Each worker keeps its own pool and queries Reddit itself. |

### A note on `CDN_MAX_AGE`

`GET /` picks a random image per request, but with `CDN_MAX_AGE=3600` a CDN in
front (Cloudflare, GitHub's camo proxy for READMEs, …) will hand every visitor
the *same* image for up to an hour. That is the intended trade-off for cheap
hosting. Set `CDN_MAX_AGE=0` if you want every hit to reach the origin.

## How it works

* A background task checks every minute whether the pool is older than
  `REFRESH_INTERVAL` and, if so, pulls the hot posts of every configured
  subreddit (via `praw` in a worker thread, so the event loop never blocks),
  filters to image posts, shuffles them and keeps `POOL_SIZE` URLs. Only the
  URL, title and permalink are kept - no image bytes.
* After every refresh a warmer downloads the pooled images (size-capped while
  streaming) into an LRU cache bounded by `IMAGE_CACHE_BYTES`. Links that 404,
  are not images or are too large are dropped from the pool on the way, so the
  pool is validated before requests ever see it.
* `GET /` picks a random image that is already in memory and returns it with
  its real content type - no network on the request path. Only while the
  warmer is still running (a few seconds after startup/refresh) can a request
  miss; it then fetches the image itself and caches it (`X-Cache: MISS`).
* The container runs uvicorn on uvloop + httptools under Python 3.13.
* A failed refresh keeps the old pool and backs off for `REFRESH_BACKOFF`
  seconds; the pool is only ever replaced by a successful fetch.

## Tests

```sh
pip install -r requirements-dev.txt
pytest
```

Reddit is stubbed and images come from a local test server, so the suite runs offline.
