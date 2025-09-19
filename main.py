from flask import Flask, send_file, jsonify
from flask_cors import CORS
import praw
import requests
from io import BytesIO
import random
import os
import threading
import time
from datetime import datetime, timedelta
import logging

app = Flask(__name__)
CORS(app)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Reddit API credentials
reddit = praw.Reddit(
    client_id='if4qb7WtCGNBY4miAkjH1g',
    client_secret='ftZJP810pIrTWRbn8fUtvVoFJgrdng',
    user_agent='GithubReadMe'
)

# Global cache
meme_cache = {
    'images': [],
    'last_updated': None,
    'lock': threading.Lock()
}

# Configuration
CACHE_DURATION = 86400  # 24 hours (86400 seconds)
PREFETCH_COUNT = 20     # Number of images to prefetch
UPDATE_INTERVAL = 86400 # Update cache every 24 hours

def is_image_url(url):
    """Check if URL points to an image"""
    image_extensions = ('.jpg', '.jpeg', '.png', '.gif', '.webp')
    return url.lower().endswith(image_extensions) or 'i.redd.it' in url or 'i.imgur.com' in url

def download_image(url, timeout=5):
    """Download image with timeout and error handling"""
    try:
        response = requests.get(url, timeout=timeout, stream=True)
        response.raise_for_status()
        
        # Check content type
        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            return None
            
        # Limit image size (max 5MB)
        content_length = response.headers.get('content-length')
        if content_length and int(content_length) > 5 * 1024 * 1024:
            return None
            
        return response.content
    except Exception as e:
        logger.warning(f"Failed to download image from {url}: {e}")
        return None

def fetch_and_cache_memes():
    """Fetch memes and cache them"""
    try:
        logger.info("Fetching new memes...")
        subreddit = reddit.subreddit('ProgrammerHumor')
        
        # Get more posts to filter from
        posts = list(subreddit.hot(limit=50))  # Changed from 'top' to 'hot' for fresh content
        
        # Filter for image posts
        image_posts = []
        for post in posts:
            if is_image_url(post.url) and not post.over_18:
                image_posts.append(post.url)
                if len(image_posts) >= PREFETCH_COUNT * 2:  # Get extra in case some fail
                    break
        
        # Download images concurrently
        images = []
        
        def download_worker(url):
            image_data = download_image(url)
            if image_data:
                with meme_cache['lock']:
                    images.append(image_data)
        
        # Use threading for concurrent downloads
        threads = []
        for url in image_posts[:PREFETCH_COUNT * 2]:
            if len(images) >= PREFETCH_COUNT:
                break
            thread = threading.Thread(target=download_worker, args=(url,))
            thread.start()
            threads.append(thread)
        
        # Wait for all downloads (with timeout)
        for thread in threads:
            thread.join(timeout=10)
        
        # Update cache
        with meme_cache['lock']:
            if images:
                meme_cache['images'] = images[:PREFETCH_COUNT]
                meme_cache['last_updated'] = datetime.now()
                logger.info(f"Cached {len(meme_cache['images'])} memes")
            else:
                logger.error("No images could be downloaded")
                
    except Exception as e:
        logger.error(f"Error fetching memes: {e}")

def ensure_cache_fresh():
    """Ensure cache is fresh, update if needed"""
    with meme_cache['lock']:
        if (not meme_cache['last_updated'] or 
            datetime.now() - meme_cache['last_updated'] > timedelta(seconds=CACHE_DURATION) or
            not meme_cache['images']):
            
            # Cache expired or empty - clear it and update
            logger.info("Cache expired or empty, clearing and refreshing...")
            meme_cache['images'].clear()  # Clear expired cache
            needs_update = True
        else:
            needs_update = False
    
    if needs_update:
        # Update cache in background thread
        thread = threading.Thread(target=fetch_and_cache_memes)
        thread.daemon = True
        thread.start()

def background_updater():
    """Background thread to keep cache fresh"""
    while True:
        time.sleep(UPDATE_INTERVAL)
        logger.info("24-hour cache refresh triggered")
        with meme_cache['lock']:
            meme_cache['images'].clear()  # Clear old cache
        fetch_and_cache_memes()

@app.route('/')
def index():
    """Serve a random meme from cache"""
    # Initialize cache on first request
    initialize_cache()
    
    ensure_cache_fresh()
    
    with meme_cache['lock']:
        if not meme_cache['images']:
            # Fallback: fetch one image synchronously
            try:
                subreddit = reddit.subreddit('ProgrammerHumor')
                posts = list(subreddit.hot(limit=10))
                
                for post in posts:
                    if is_image_url(post.url):
                        image_data = download_image(post.url, timeout=10)
                        if image_data:
                            return send_file(BytesIO(image_data), mimetype='image/jpeg')
                
                return jsonify({"error": "No memes available"}), 503
                
            except Exception as e:
                logger.error(f"Fallback failed: {e}")
                return jsonify({"error": "Service temporarily unavailable"}), 503
        
        # Serve from cache
        image_data = random.choice(meme_cache['images'])
        return send_file(BytesIO(image_data), mimetype='image/jpeg')

@app.route('/health')
def health():
    """Health check endpoint"""
    with meme_cache['lock']:
        cached_count = len(meme_cache['images'])
        last_updated = meme_cache['last_updated']
    
    return jsonify({
        "status": "healthy",
        "cached_memes": cached_count,
        "last_updated": last_updated.isoformat() if last_updated else None
    })

@app.route('/refresh')
def refresh_cache():
    """Manually refresh cache"""
    thread = threading.Thread(target=fetch_and_cache_memes)
    thread.daemon = True
    thread.start()
    return jsonify({"message": "Cache refresh initiated"})

# Global flag to track initialization
cache_initialized = False

def initialize_cache():
    """Initialize cache - called once at startup"""
    global cache_initialized
    if not cache_initialized:
        logger.info("Initializing meme cache...")
        # Start background updater
        updater_thread = threading.Thread(target=background_updater)
        updater_thread.daemon = True
        updater_thread.start()
        
        # Initial cache population
        fetch_and_cache_memes()
        cache_initialized = True

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))