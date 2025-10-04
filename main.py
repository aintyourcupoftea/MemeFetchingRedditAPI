import asyncio
import logging
import os
import pickle
import random
from datetime import datetime, timedelta
from io import BytesIO

import aiohttp
import praw
import redis.asyncio as aioredis
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Meme API", version="2.0.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
CACHE_KEY = "memes:images"
CACHE_TIMESTAMP_KEY = "memes:timestamp"
CACHE_DURATION = 86400  # 24 hours
PREFETCH_COUNT = 30

# Reddit API
reddit = praw.Reddit(
    client_id="if4qb7WtCGNBY4miAkjH1g",
    client_secret="ftZJP810pIrTWRbn8fUtvVoFJgrdng",
    user_agent="GithubReadMe",
)

# Redis connection
redis_client = None


@app.on_event("startup")
async def startup_event():
    """Initialize Redis and cache on startup"""
    global redis_client
    try:
        redis_client = await aioredis.from_url(
            REDIS_URL, encoding="utf-8", decode_responses=False, max_connections=10
        )
        logger.info("✅ Connected to Redis")

        # Initialize cache if empty
        await ensure_cache_fresh()

    except Exception as e:
        logger.error(f"❌ Redis connection failed: {e}")
        redis_client = None


@app.on_event("shutdown")
async def shutdown_event():
    """Close Redis connection"""
    if redis_client:
        await redis_client.close()
        logger.info("Closed Redis connection")


def is_image_url(url):
    """Check if URL points to an image"""
    image_extensions = (".jpg", ".jpeg", ".png", ".gif", ".webp")
    return (
        url.lower().endswith(image_extensions)
        or "i.redd.it" in url
        or "i.imgur.com" in url
    )


async def download_image(session, url, timeout=5):
    """Download image asynchronously"""
    try:
        async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout)
        ) as response:
            if response.status == 200:
                content_type = response.headers.get("content-type", "")
                if content_type.startswith("image/"):
                    content = await response.read()
                    # Limit size to 5MB
                    if len(content) <= 5 * 1024 * 1024:
                        return content
    except Exception as e:
        logger.warning(f"Failed to download {url}: {e}")
    return None


async def fetch_and_cache_memes():
    """Fetch memes from Reddit and cache in Redis"""
    try:
        logger.info("🔄 Fetching new memes from Reddit...")

        # Get posts from Reddit (sync operation)
        subreddit = reddit.subreddit("ProgrammerHumor")
        posts = list(subreddit.hot(limit=100))

        # Filter image URLs
        image_urls = [
            post.url for post in posts if is_image_url(post.url) and not post.over_18
        ][: PREFETCH_COUNT * 2]

        logger.info(f"Found {len(image_urls)} image URLs")

        # Download images asynchronously
        images = []
        async with aiohttp.ClientSession() as session:
            tasks = [download_image(session, url) for url in image_urls]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            images = [img for img in results if img and not isinstance(img, Exception)]

        if images:
            # Store in Redis
            images_data = pickle.dumps(images[:PREFETCH_COUNT])
            await redis_client.set(CACHE_KEY, images_data, ex=CACHE_DURATION)
            await redis_client.set(CACHE_TIMESTAMP_KEY, datetime.now().isoformat())

            logger.info(f"✅ Cached {len(images[:PREFETCH_COUNT])} memes in Redis")
            return True
        else:
            logger.error("❌ No images could be downloaded")
            return False

    except Exception as e:
        logger.error(f"❌ Error fetching memes: {e}")
        return False


async def get_cached_memes():
    """Get memes from Redis cache"""
    try:
        if not redis_client:
            return None

        data = await redis_client.get(CACHE_KEY)
        if data:
            return pickle.loads(data)
    except Exception as e:
        logger.error(f"Error reading from cache: {e}")
    return None


async def ensure_cache_fresh():
    """Ensure cache is fresh, update if needed"""
    try:
        if not redis_client:
            return False

        # Check if cache exists and is fresh
        timestamp_str = await redis_client.get(CACHE_TIMESTAMP_KEY)

        if timestamp_str:
            last_updated = datetime.fromisoformat(
                timestamp_str.decode()
                if isinstance(timestamp_str, bytes)
                else timestamp_str
            )
            if datetime.now() - last_updated < timedelta(seconds=CACHE_DURATION):
                cached_memes = await get_cached_memes()
                if cached_memes:
                    logger.info(f"✅ Cache is fresh ({len(cached_memes)} memes)")
                    return True

        # Cache is stale or empty - refresh it
        logger.info("⚠️  Cache is stale or empty, refreshing...")
        return await fetch_and_cache_memes()

    except Exception as e:
        logger.error(f"Error checking cache: {e}")
        return False


@app.options("/")
async def options_meme():
    """Handle CORS preflight"""
    return Response(
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )


@app.get("/")
async def get_meme():
    """Serve a random meme"""
    try:
        # Get memes from cache
        cached_memes = await get_cached_memes()

        if not cached_memes:
            # Cache miss - fetch synchronously as fallback
            logger.warning("⚠️  Cache miss - fetching fallback meme")
            await fetch_and_cache_memes()
            cached_memes = await get_cached_memes()

            if not cached_memes:
                return JSONResponse(
                    status_code=503,
                    content={"error": "Service temporarily unavailable"},
                )

        # Return random meme
        image_data = random.choice(cached_memes)

        return Response(
            content=image_data,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=3600",
                "CDN-Cache-Control": "public, max-age=3600",
                "Cloudflare-CDN-Cache-Control": "public, max-age=3600",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "*",
            },
        )

    except Exception as e:
        logger.error(f"Error serving meme: {e}")
        return JSONResponse(status_code=500, content={"error": "Internal server error"})


@app.get("/health")
async def health_check():
    """Health check endpoint"""
    try:
        cached_memes = await get_cached_memes()
        timestamp_str = (
            await redis_client.get(CACHE_TIMESTAMP_KEY) if redis_client else None
        )

        last_updated = None
        if timestamp_str:
            last_updated = (
                timestamp_str.decode()
                if isinstance(timestamp_str, bytes)
                else timestamp_str
            )

        return {
            "status": "healthy",
            "redis_connected": redis_client is not None,
            "cached_memes": len(cached_memes) if cached_memes else 0,
            "last_updated": last_updated,
            "cache_duration_hours": CACHE_DURATION / 3600,
        }
    except Exception as e:
        return JSONResponse(
            status_code=503, content={"status": "unhealthy", "error": str(e)}
        )


@app.post("/refresh")
async def refresh_cache():
    """Manually refresh cache"""
    success = await fetch_and_cache_memes()

    if success:
        return {"message": "Cache refreshed successfully"}
    else:
        return JSONResponse(
            status_code=500, content={"error": "Failed to refresh cache"}
        )


@app.get("/stats")
async def stats():
    """Get cache statistics"""
    try:
        cached_memes = await get_cached_memes()
        timestamp_str = (
            await redis_client.get(CACHE_TIMESTAMP_KEY) if redis_client else None
        )

        last_updated = None
        cache_age_seconds = None

        if timestamp_str:
            last_updated_dt = datetime.fromisoformat(
                timestamp_str.decode()
                if isinstance(timestamp_str, bytes)
                else timestamp_str
            )
            last_updated = last_updated_dt.isoformat()
            cache_age_seconds = (datetime.now() - last_updated_dt).total_seconds()

        return {
            "cached_memes": len(cached_memes) if cached_memes else 0,
            "last_updated": last_updated,
            "cache_age_seconds": cache_age_seconds,
            "cache_age_hours": cache_age_seconds / 3600 if cache_age_seconds else None,
            "cache_duration_hours": CACHE_DURATION / 3600,
            "redis_connected": redis_client is not None,
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
