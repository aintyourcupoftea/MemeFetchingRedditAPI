"""Meme API - serves a random image meme from Reddit. No database required.

Flow:
  * A background task pulls the hot posts of the configured subreddits and keeps
    a list of image post URLs (plus title/permalink) in memory.
  * A warmer downloads those images into a byte-bounded in-memory LRU cache.
  * `GET /` serves a random cached image straight from memory (no network on
    the request path); on a cache miss it fetches from Reddit's CDN and caches
    the result. Dead links are dropped from the pool.

All configuration is read from the environment - see README.md.
"""

import asyncio
import logging
import os
import random
import secrets
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from urllib.parse import quote

import aiohttp
import praw
from fastapi import FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("meme-api")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET")
REDDIT_USER_AGENT = os.getenv("REDDIT_USER_AGENT", "meme-api/3.0 (self-hosted)")

SUBREDDITS = [
    s.strip() for s in os.getenv("SUBREDDITS", "ProgrammerHumor").split(",") if s.strip()
]
POSTS_PER_SUBREDDIT = _env_int("POSTS_PER_SUBREDDIT", 100)
POOL_SIZE = _env_int("POOL_SIZE", 100)  # meme URLs kept in memory
MAX_IMAGE_BYTES = _env_int("MAX_IMAGE_BYTES", 2 * 1024 * 1024)  # bigger memes are skipped
DOWNLOAD_TIMEOUT = _env_int("DOWNLOAD_TIMEOUT", 10)
SERVE_ATTEMPTS = _env_int("SERVE_ATTEMPTS", 3)  # dead links tried per request before giving up
IMAGE_CACHE_BYTES = _env_int("IMAGE_CACHE_BYTES", 32 * 1024 * 1024)  # hard cap on cached image bytes
WARM_CONCURRENCY = _env_int("WARM_CONCURRENCY", 8)  # parallel downloads while pre-warming

REFRESH_INTERVAL = _env_int("REFRESH_INTERVAL", 6 * 3600)  # pull new posts this often
REFRESH_CHECK_INTERVAL = _env_int("REFRESH_CHECK_INTERVAL", 60)
REFRESH_BACKOFF = _env_int("REFRESH_BACKOFF", 300)  # wait this long after a failed refresh
CDN_MAX_AGE = _env_int("CDN_MAX_AGE", 3600)  # how long CDN/browsers may cache a served meme

REFRESH_TOKEN = os.getenv("REFRESH_TOKEN")  # bearer token for POST /refresh; unset = disabled

# Self-ping so free-tier hosts (Render, Koyeb, ...) do not spin the service down
# while idle. Must be the *public* URL: only inbound traffic counts. Render sets
# RENDER_EXTERNAL_URL itself, so there it works with no extra configuration.
KEEPALIVE_URL = os.getenv("KEEPALIVE_URL") or (
    os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/") + "/health"
    if os.getenv("RENDER_EXTERNAL_URL")
    else None
)
KEEPALIVE_INTERVAL = _env_int("KEEPALIVE_INTERVAL", 600)  # Render idles out after 15 min

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
IMAGE_HOSTS = ("i.redd.it", "i.imgur.com")


# --------------------------------------------------------------------------- #
# In-memory state
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Meme:
    url: str
    title: str
    permalink: str
    subreddit: str


class ImageCache:
    """LRU of image bytes bounded by total size, keyed by URL."""

    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self.size = 0
        self._items: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, url: str) -> bool:
        return url in self._items

    def get(self, url: str) -> tuple[bytes, str] | None:
        item = self._items.get(url)
        if item is not None:
            self._items.move_to_end(url)
        return item

    def put(self, url: str, data: bytes, content_type: str) -> None:
        self.discard(url)
        while self._items and self.size + len(data) > self.max_bytes:
            _, (old, _) = self._items.popitem(last=False)
            self.size -= len(old)
        self._items[url] = (data, content_type)
        self.size += len(data)

    def discard(self, url: str) -> None:
        item = self._items.pop(url, None)
        if item is not None:
            self.size -= len(item[0])

    def urls(self) -> list[str]:
        return list(self._items)


@dataclass
class State:
    memes: list[Meme] = field(default_factory=list)
    images: ImageCache = field(default_factory=lambda: ImageCache(IMAGE_CACHE_BYTES))
    warmer: asyncio.Task | None = None
    updated_at: float | None = None  # unix time of the last successful refresh
    failed_at: float | None = None  # unix time of the last failed refresh (for backoff)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    http: aiohttp.ClientSession | None = None
    reddit: praw.Reddit | None = None


state = State()


def cache_age() -> float | None:
    return time.time() - state.updated_at if state.updated_at else None


def in_backoff() -> bool:
    return state.failed_at is not None and time.time() - state.failed_at < REFRESH_BACKOFF


def random_meme() -> Meme | None:
    """Prefer a meme whose bytes are already in memory; fall back to any pooled URL."""
    if not state.memes:
        return None
    warm = [m for m in state.memes if m.url in state.images]
    return random.choice(warm or state.memes)


def drop_meme(meme: Meme) -> None:
    """Remove a dead link from the pool and the cache."""
    state.memes = [m for m in state.memes if m != meme]
    state.images.discard(meme.url)
    logger.info("Dropped %s (pool now %d)", meme.url, len(state.memes))


# --------------------------------------------------------------------------- #
# Reddit + download
# --------------------------------------------------------------------------- #
def is_image_url(url: str) -> bool:
    lowered = url.lower()
    return lowered.endswith(IMAGE_EXTENSIONS) or any(host in lowered for host in IMAGE_HOSTS)


def fetch_candidate_posts() -> list[Meme]:
    """Blocking praw call - run via asyncio.to_thread so it never stalls the event loop.

    A subreddit that is private, banned or misspelled is logged and skipped so
    it cannot take the whole refresh down with it.
    """
    assert state.reddit is not None, "Reddit client not initialised"
    posts: list[Meme] = []
    for name in SUBREDDITS:
        try:
            for post in state.reddit.subreddit(name).hot(limit=POSTS_PER_SUBREDDIT):
                if post.over_18 or post.stickied or not is_image_url(post.url):
                    continue
                posts.append(Meme(post.url, post.title, post.permalink, name))
        except Exception as e:
            logger.error("Skipping r/%s: %s", name, e)
    return posts


async def download_image(session: aiohttp.ClientSession, meme: Meme) -> tuple[bytes, str] | None:
    """Fetch one image, enforcing MAX_IMAGE_BYTES while streaming. Returns (bytes, content_type)."""
    try:
        async with session.get(
            meme.url, timeout=aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT)
        ) as response:
            if response.status != 200:
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not content_type.startswith("image/"):
                return None
            if response.content_length and response.content_length > MAX_IMAGE_BYTES:
                return None
            buf = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                buf.extend(chunk)
                if len(buf) > MAX_IMAGE_BYTES:
                    logger.info("Skipping %s: larger than %d bytes", meme.url, MAX_IMAGE_BYTES)
                    return None
            return bytes(buf), content_type
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning("Failed to download %s: %s", meme.url, e)
    return None


async def warm_cache() -> None:
    """Download every pooled image not yet in memory, until the byte budget is full.

    Dead links found on the way are dropped, so the pool is validated as a side
    effect and requests almost never see a miss.
    """
    assert state.http is not None
    semaphore = asyncio.Semaphore(WARM_CONCURRENCY)
    todo = [m for m in state.memes if m.url not in state.images]

    async def warm_one(meme: Meme) -> None:
        async with semaphore:
            if state.images.size >= state.images.max_bytes:
                return
            image = await download_image(state.http, meme)
            if image is None:
                drop_meme(meme)
            else:
                state.images.put(meme.url, *image)

    await asyncio.gather(*(warm_one(m) for m in todo))
    logger.info(
        "Cache warm: %d images, %.1f MB", len(state.images), state.images.size / 1_048_576
    )


def start_warmer() -> None:
    if state.warmer is None or state.warmer.done():
        state.warmer = asyncio.create_task(warm_cache())


# --------------------------------------------------------------------------- #
# Refresh
# --------------------------------------------------------------------------- #
REFRESH_OK, REFRESH_BUSY, REFRESH_FAILED = "ok", "busy", "failed"


async def refresh_memes() -> str:
    """Replace the pool with fresh hot posts. Returns REFRESH_OK, REFRESH_BUSY or REFRESH_FAILED.

    Only one refresh runs at a time; a failure keeps the old pool and arms a
    backoff so Reddit is not hammered while it is down.
    """
    if state.lock.locked():
        logger.info("Refresh already in progress, skipping")
        return REFRESH_BUSY
    async with state.lock:
        try:
            logger.info("Fetching posts from r/%s", "+".join(SUBREDDITS))
            posts = await asyncio.to_thread(fetch_candidate_posts)
            if not posts:
                raise RuntimeError("no image posts found")
            # Shuffle so every configured subreddit gets a share of the pool.
            random.shuffle(posts)
            state.memes = posts[:POOL_SIZE]
            state.updated_at = time.time()
            state.failed_at = None
            logger.info("Pool refreshed with %d memes", len(state.memes))
            start_warmer()
            return REFRESH_OK
        except Exception:
            logger.exception("Refreshing memes failed, keeping the old pool")
            state.failed_at = time.time()
            return REFRESH_FAILED


async def ensure_pool(wait: float = 10.0) -> None:
    """Populate an empty pool; if a refresh is already running, wait for it briefly."""
    if state.memes or in_backoff():
        return
    if await refresh_memes() != REFRESH_BUSY:
        return
    try:
        await asyncio.wait_for(state.lock.acquire(), wait)
        state.lock.release()
    except asyncio.TimeoutError:
        pass


async def refresh_loop() -> None:
    """Background task: refresh the pool whenever it is older than REFRESH_INTERVAL."""
    while True:
        try:
            age = cache_age()
            if (age is None or age >= REFRESH_INTERVAL) and not in_backoff():
                await refresh_memes()
        except Exception:
            logger.exception("Background refresh failed")
        await asyncio.sleep(REFRESH_CHECK_INTERVAL)


async def keepalive_loop() -> None:
    """Background task: hit our own public URL so the host never sees us as idle."""
    assert KEEPALIVE_URL is not None and state.http is not None
    logger.info("Keep-alive enabled: pinging %s every %ds", KEEPALIVE_URL, KEEPALIVE_INTERVAL)
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        try:
            async with state.http.get(
                KEEPALIVE_URL, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                logger.debug("Keep-alive ping -> %s", response.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning("Keep-alive ping failed: %s", e)


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(_: FastAPI):
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET):
        raise RuntimeError(
            "REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set "
            "(create a 'script' app at https://www.reddit.com/prefs/apps)"
        )

    state.reddit = praw.Reddit(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=REDDIT_USER_AGENT,
        check_for_async=False,  # praw is only ever called from a worker thread
    )
    state.http = aiohttp.ClientSession(headers={"User-Agent": REDDIT_USER_AGENT})

    tasks = [asyncio.create_task(refresh_loop())]
    if KEEPALIVE_URL:
        tasks.append(asyncio.create_task(keepalive_loop()))
    try:
        yield
    finally:
        if state.warmer is not None:
            tasks.append(state.warmer)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await state.http.close()


app = FastAPI(
    title="Meme API",
    version="3.0.0",
    description="Returns a random image meme from Reddit. `GET /` for the image, `GET /meme` for metadata.",
    lifespan=lifespan,
)

# Public read-only API: any origin, no credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Meme-Title", "X-Meme-Permalink", "X-Meme-Subreddit", "X-Cache"],
)

NO_STORE = {"Cache-Control": "no-store"}
_CC = f"public, max-age={CDN_MAX_AGE}"
CACHE_HEADERS = {"Cache-Control": _CC, "CDN-Cache-Control": _CC, "Cloudflare-CDN-Cache-Control": _CC}


def error(status_code: int, message: str, **headers: str) -> JSONResponse:
    # Errors are never cacheable - otherwise the CDN would pin a 503 for an hour.
    return JSONResponse(status_code=status_code, content={"error": message}, headers={**NO_STORE, **headers})


def meme_headers(meme: Meme) -> dict[str, str]:
    return {
        # Header values must be latin-1; titles are arbitrary unicode so percent-encode.
        "X-Meme-Title": quote(meme.title, safe=" "),
        "X-Meme-Permalink": "https://www.reddit.com" + quote(meme.permalink),
        "X-Meme-Subreddit": meme.subreddit,
    }


async def pick_meme() -> Meme | None:
    meme = random_meme()
    if meme is None:
        logger.warning("Pool empty - populating on demand")
        await ensure_pool()
        meme = random_meme()
    return meme


# FastAPI does not add HEAD to GET routes automatically, and <img> prefetchers use it.
@app.api_route("/", methods=["GET", "HEAD"], summary="Random meme image")
async def get_meme(request: Request):
    assert state.http is not None
    try:
        for _ in range(SERVE_ATTEMPTS):
            meme = await pick_meme()
            if meme is None:
                break
            image = state.images.get(meme.url)
            cache_status = "HIT"
            if image is None:
                cache_status = "MISS"
                image = await download_image(state.http, meme)
                if image is None:
                    drop_meme(meme)  # dead link, deleted post, oversized: try another
                    continue
                state.images.put(meme.url, *image)
            data, content_type = image
            headers = {**CACHE_HEADERS, "X-Cache": cache_status, **meme_headers(meme)}
            if request.method == "HEAD":
                headers["Content-Length"] = str(len(data))
                return Response(media_type=content_type, headers=headers)
            return Response(content=data, media_type=content_type, headers=headers)
    except Exception:
        logger.exception("Error serving meme")
        return error(500, "Internal server error")
    return error(503, "No memes available right now", **{"Retry-After": "30"})


@app.get("/meme", summary="Random meme metadata (JSON)")
async def get_meme_json():
    try:
        meme = await pick_meme()
    except Exception:
        logger.exception("Error serving meme metadata")
        return error(500, "Internal server error")
    if meme is None:
        return error(503, "No memes available right now", **{"Retry-After": "30"})
    return JSONResponse(
        content={
            "title": meme.title,
            "url": meme.url,
            "permalink": "https://www.reddit.com" + meme.permalink,
            "subreddit": meme.subreddit,
        },
        headers=NO_STORE,
    )


def pool_summary() -> dict:
    age = cache_age()
    return {
        "subreddits": SUBREDDITS,
        "cached_memes": len(state.memes),
        "images_in_memory": len(state.images),
        "image_cache_mb": round(state.images.size / 1_048_576, 1),
        "cache_age_seconds": round(age) if age is not None else None,
        "refresh_interval_seconds": REFRESH_INTERVAL,
        "in_backoff": in_backoff(),
    }


@app.get("/health", summary="Liveness/readiness check")
async def health():
    summary = pool_summary()
    status = "healthy" if summary["cached_memes"] else "degraded"
    return JSONResponse(content={"status": status, **summary}, headers=NO_STORE)


@app.get("/stats", summary="Pool statistics")
async def stats():
    return JSONResponse(content=pool_summary(), headers=NO_STORE)


@app.post("/refresh", summary="Force a refresh (requires REFRESH_TOKEN)")
async def refresh(authorization: str | None = Header(default=None)):
    if not REFRESH_TOKEN:
        return error(403, "Refresh endpoint disabled: set REFRESH_TOKEN to enable it")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not secrets.compare_digest(supplied, REFRESH_TOKEN):
        return error(401, "Invalid token")
    result = await refresh_memes()
    if result == REFRESH_OK:
        return JSONResponse(content={"message": "Pool refreshed", **pool_summary()}, headers=NO_STORE)
    if result == REFRESH_BUSY:
        return error(409, "A refresh is already running")
    return error(502, "Refresh failed - see server logs")
