"""Offline tests: Reddit is stubbed and images are served by a local aiohttp server."""

import asyncio
import os
import time
from types import SimpleNamespace

os.environ.setdefault("REDDIT_CLIENT_ID", "test-id")
os.environ.setdefault("REDDIT_CLIENT_SECRET", "test-secret")
os.environ.setdefault("REFRESH_TOKEN", "s3cret")

import aiohttp  # noqa: E402
import pytest  # noqa: E402
from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

import main  # noqa: E402
from main import Meme  # noqa: E402

PNG = b"\x89PNG" + b"\x00" * 100


@pytest.fixture
async def server(monkeypatch):
    """Image host with one route per branch of download_image."""
    monkeypatch.setattr(main, "MAX_IMAGE_BYTES", 1024)

    async def ok(_):
        return web.Response(body=PNG, content_type="image/png")

    async def jpeg_with_charset(_):
        return web.Response(body=PNG, headers={"Content-Type": "Image/JPEG; charset=binary"})

    async def not_found(_):
        return web.Response(status=404, body=PNG, content_type="image/png")

    async def html(_):
        return web.Response(body=b"<html>removed</html>", content_type="text/html")

    async def too_big_declared(_):
        return web.Response(body=b"x" * 2048, content_type="image/png")

    async def too_big_chunked(request):
        # No Content-Length: the size cap must trip mid-stream.
        resp = web.StreamResponse(headers={"Content-Type": "image/gif"})
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        for _ in range(4):
            await resp.write(b"y" * 512)
        return resp

    app = web.Application()
    app.router.add_get("/ok.png", ok)
    app.router.add_get("/charset.jpg", jpeg_with_charset)
    app.router.add_get("/missing.png", not_found)
    app.router.add_get("/page.png", html)
    app.router.add_get("/big.png", too_big_declared)
    app.router.add_get("/big.gif", too_big_chunked)
    async with TestServer(app) as srv:
        yield srv


def meme(url, title="Tabs vs spaces 🔥", sub="ProgrammerHumor"):
    return Meme(url=url, title=title, permalink="/r/ProgrammerHumor/comments/abc/tabs/", subreddit=sub)


@pytest.fixture
async def app_state(monkeypatch):
    """Fresh in-memory state with a live HTTP session, no lifespan (Reddit never contacted)."""
    fresh = main.State()
    fresh.http = aiohttp.ClientSession()
    monkeypatch.setattr(main, "state", fresh)
    yield fresh
    await fresh.http.close()


@pytest.fixture
async def client(app_state):
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c


@pytest.fixture
def fake_reddit(monkeypatch, server):
    posts = [meme(str(server.make_url("/ok.png"))), meme(str(server.make_url("/charset.jpg")))]
    monkeypatch.setattr(main, "fetch_candidate_posts", lambda: list(posts))
    return posts


@pytest.fixture
def broken_reddit(monkeypatch):
    def boom():
        raise RuntimeError("reddit is down")

    monkeypatch.setattr(main, "fetch_candidate_posts", boom)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
async def _download(app_state, server, path):
    return await main.download_image(app_state.http, meme(str(server.make_url(path))))


async def test_download_ok(app_state, server):
    data, content_type = await _download(app_state, server, "/ok.png")
    assert data == PNG and content_type == "image/png"


async def test_download_normalises_content_type(app_state, server):
    _, content_type = await _download(app_state, server, "/charset.jpg")
    assert content_type == "image/jpeg"


@pytest.mark.parametrize("path", ["/missing.png", "/page.png", "/big.png", "/big.gif"])
async def test_download_rejects(app_state, server, path):
    assert await _download(app_state, server, path) is None


async def test_download_connection_error_is_swallowed(app_state):
    assert await main.download_image(app_state.http, meme("http://127.0.0.1:9/nope.png")) is None


# --------------------------------------------------------------------------- #
# Refresh / pool
# --------------------------------------------------------------------------- #
async def test_refresh_fills_pool(app_state, fake_reddit):
    assert await main.refresh_memes() == main.REFRESH_OK
    assert len(app_state.memes) == 2
    assert main.cache_age() < 5
    assert not main.in_backoff()


async def test_refresh_failure_keeps_pool_and_arms_backoff(app_state, broken_reddit):
    app_state.memes = [meme("http://x/a.png")]
    assert await main.refresh_memes() == main.REFRESH_FAILED
    assert len(app_state.memes) == 1
    assert main.in_backoff()
    assert not app_state.lock.locked()


async def test_concurrent_refreshes_coalesce(app_state, monkeypatch):
    calls = 0

    def slow_fetch():
        nonlocal calls
        calls += 1
        time.sleep(0.2)
        return [meme("http://x/a.png")]

    monkeypatch.setattr(main, "fetch_candidate_posts", slow_fetch)
    results = await asyncio.gather(main.refresh_memes(), main.refresh_memes(), main.refresh_memes())
    assert sorted(results) == [main.REFRESH_BUSY, main.REFRESH_BUSY, main.REFRESH_OK]
    assert calls == 1


async def test_refresh_mixes_subreddits_and_caps_pool(app_state, monkeypatch):
    monkeypatch.setattr(main, "POOL_SIZE", 4)
    posts = [meme("http://x/a.png", sub="first") for _ in range(50)] + [
        meme("http://x/b.png", sub="second") for _ in range(50)
    ]
    monkeypatch.setattr(main, "fetch_candidate_posts", lambda: list(posts))
    seen = set()
    for _ in range(10):
        await main.refresh_memes()
        assert len(app_state.memes) == 4
        seen.update(m.subreddit for m in app_state.memes)
    assert seen == {"first", "second"}


def test_fetch_candidate_posts_filters_and_survives_bad_subreddit(app_state, monkeypatch):
    def post(url, over_18=False, stickied=False):
        return SimpleNamespace(url=url, title="t", permalink="/p/", over_18=over_18, stickied=stickied)

    class Sub:
        def __init__(self, name):
            self.name = name

        def hot(self, limit):
            if self.name == "private":
                raise RuntimeError("403 forbidden")
            return [
                post("https://i.redd.it/a.png"),
                post("https://i.redd.it/nsfw.png", over_18=True),
                post("https://i.redd.it/pinned.png", stickied=True),
                post("https://v.redd.it/video"),
                post("https://www.reddit.com/gallery/x"),
            ]

    app_state.reddit = SimpleNamespace(subreddit=Sub)
    monkeypatch.setattr(main, "SUBREDDITS", ["private", "good"])
    posts = main.fetch_candidate_posts()
    assert [(p.url, p.subreddit) for p in posts] == [("https://i.redd.it/a.png", "good")]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
async def test_get_meme_serves_image_with_real_content_type(client, app_state, server):
    app_state.memes = [meme(str(server.make_url("/ok.png")))]
    r = await client.get("/", headers={"Origin": "https://github.com"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == PNG
    assert r.headers["cache-control"] == f"public, max-age={main.CDN_MAX_AGE}"
    assert r.headers["x-meme-subreddit"] == "ProgrammerHumor"
    assert r.headers["x-meme-permalink"] == "https://www.reddit.com/r/ProgrammerHumor/comments/abc/tabs/"
    assert "%F0%9F%94%A5" in r.headers["x-meme-title"]  # emoji percent-encoded, not dropped
    assert r.headers["access-control-allow-origin"] == "*"


async def test_get_meme_populates_pool_on_first_request(client, app_state, fake_reddit):
    r = await client.get("/")
    assert r.status_code == 200
    assert len(app_state.memes) == 2


async def test_get_meme_drops_dead_links_and_retries(client, app_state, server):
    dead = meme(str(server.make_url("/missing.png")))
    good = meme(str(server.make_url("/ok.png")))
    app_state.memes = [dead, dead, good]
    r = await client.get("/")
    assert r.status_code == 200
    assert good in app_state.memes and good.url in app_state.images


async def test_get_meme_miss_then_hit(client, app_state, server):
    app_state.memes = [meme(str(server.make_url("/ok.png")))]
    r = await client.get("/")
    assert r.status_code == 200 and r.headers["x-cache"] == "MISS"
    r = await client.get("/")
    assert r.status_code == 200 and r.headers["x-cache"] == "HIT"
    assert r.content == PNG


async def test_get_meme_prefers_warm_images(client, app_state, server, monkeypatch):
    """Once anything is cached, requests never leave memory even if the pool has cold URLs."""
    cold = meme(str(server.make_url("/ok.png")), title="cold")
    warm = meme("http://127.0.0.1:9/never-fetched.png", title="warm")
    app_state.memes = [cold] * 10 + [warm]
    app_state.images.put(warm.url, PNG, "image/png")
    for _ in range(20):
        r = await client.get("/")
        assert r.headers["x-cache"] == "HIT" and r.headers["x-meme-title"] == "warm"


async def test_warm_cache_downloads_pool_and_drops_dead_links(app_state, server):
    good = meme(str(server.make_url("/ok.png")))
    dead = meme(str(server.make_url("/missing.png")))
    big = meme(str(server.make_url("/big.png")))
    app_state.memes = [good, dead, big]
    await main.warm_cache()
    assert app_state.memes == [good]
    assert app_state.images.get(good.url) == (PNG, "image/png")


async def test_warm_cache_respects_byte_budget(app_state, server, monkeypatch):
    app_state.images = main.ImageCache(max_bytes=len(PNG) * 2 + 1)
    app_state.memes = [meme(str(server.make_url(f"/ok.png?{i}"))) for i in range(10)]
    monkeypatch.setattr(main, "WARM_CONCURRENCY", 1)
    await main.warm_cache()
    assert len(app_state.images) == 2
    assert len(app_state.memes) == 10  # nothing dropped, just not warmed


async def test_refresh_starts_warmer(app_state, fake_reddit):
    assert await main.refresh_memes() == main.REFRESH_OK
    assert app_state.warmer is not None
    await app_state.warmer
    assert len(app_state.images) == 2


def test_image_cache_lru_by_bytes():
    cache = main.ImageCache(max_bytes=100)
    cache.put("a", b"x" * 40, "image/png")
    cache.put("b", b"x" * 40, "image/png")
    assert cache.get("a")  # touch a -> b is now least recent
    cache.put("c", b"x" * 40, "image/png")
    assert "b" not in cache and "a" in cache and "c" in cache
    assert cache.size == 80
    cache.put("d", b"x" * 100, "image/png")
    assert cache.urls() == ["d"] and cache.size == 100
    cache.discard("d")
    assert len(cache) == 0 and cache.size == 0


async def test_get_meme_503_when_everything_is_dead(client, app_state, server):
    app_state.memes = [meme(str(server.make_url("/page.png"))) for _ in range(5)]
    app_state.updated_at = 1.0
    app_state.failed_at = 1e12  # in backoff so an empty pool is not refilled
    r = await client.get("/")
    assert r.status_code == 503
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["retry-after"] == "30"


async def test_get_meme_503_is_not_cacheable_when_reddit_down(client, app_state, broken_reddit):
    r = await client.get("/")
    assert r.status_code == 503
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {"error": "No memes available right now"}


async def test_get_meme_does_not_retry_reddit_during_backoff(client, app_state, monkeypatch):
    calls = 0

    def boom():
        nonlocal calls
        calls += 1
        raise RuntimeError("reddit is down")

    monkeypatch.setattr(main, "fetch_candidate_posts", boom)
    assert (await client.get("/")).status_code == 503
    assert (await client.get("/")).status_code == 503
    assert calls == 1


async def test_head_and_options(client, app_state, server):
    app_state.memes = [meme(str(server.make_url("/ok.png")))]
    r = await client.head("/")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.headers["content-length"] == str(len(PNG))
    assert r.content == b""

    r = await client.options(
        "/", headers={"Origin": "https://example.com", "Access-Control-Request-Method": "GET"}
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"


async def test_meme_json(client, app_state):
    app_state.memes = [meme("https://i.redd.it/def.gif", title="It works on my machine")]
    r = await client.get("/meme")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert r.json() == {
        "title": "It works on my machine",
        "url": "https://i.redd.it/def.gif",
        "permalink": "https://www.reddit.com/r/ProgrammerHumor/comments/abc/tabs/",
        "subreddit": "ProgrammerHumor",
    }


async def test_health_reports_state(client, app_state):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["cached_memes"] == 0

    app_state.memes = [meme("http://x/a.png")]
    r = await client.get("/health")
    assert r.json()["status"] == "healthy"
    assert r.json()["cached_memes"] == 1
    assert r.json()["images_in_memory"] == 0


async def test_refresh_requires_token(client, app_state, fake_reddit):
    assert (await client.post("/refresh")).status_code == 401
    assert (await client.post("/refresh", headers={"Authorization": "Bearer wrong"})).status_code == 401
    r = await client.post("/refresh", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.json()["cached_memes"] == 2


async def test_refresh_reports_failure(client, app_state, broken_reddit):
    r = await client.post("/refresh", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 502


async def test_refresh_disabled_without_token(client, app_state, monkeypatch):
    monkeypatch.setattr(main, "REFRESH_TOKEN", None)
    r = await client.post("/refresh", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 403


def test_is_image_url():
    assert main.is_image_url("https://i.redd.it/abc.png")
    assert main.is_image_url("https://example.com/x.JPG")
    assert main.is_image_url("https://i.imgur.com/abc")
    assert not main.is_image_url("https://v.redd.it/abc")
    assert not main.is_image_url("https://www.reddit.com/gallery/abc")
