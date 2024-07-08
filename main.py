from flask import Flask, send_file
import praw
import os
import requests
from io import BytesIO
import time
from apscheduler.schedulers.background import BackgroundScheduler

app = Flask(__name__)

# Reddit API credentials
reddit = praw.Reddit(
    client_id='if4qb7WtCGNBY4miAkjH1g',
    client_secret='ftZJP810pIrTWRbn8fUtvVoFJgrdng',
    user_agent='GithubReadMe'
)

# Variables to store cached data
cached_post_url = None
cached_post_image = None
cached_time = 0

# Function to fetch top post URL and image from r/ProgrammerHumor
def fetch_top_post():
    global cached_post_url, cached_post_image, cached_time
    
    subreddit = reddit.subreddit('ProgrammerHumor')
    top_post = next(subreddit.top('day', limit=1))  # Fetch top post of the day
    
    cached_post_url = top_post.url
    cached_post_image = requests.get(cached_post_url)
    cached_time = time.time()
    
    print("Fetched new top post.")
    return cached_post_image

# Schedule the fetch_top_post function to run once every 24 hours
scheduler = BackgroundScheduler()
scheduler.add_job(fetch_top_post, 'interval', hours=24)
scheduler.start()

# Ensure the top post is fetched when the app starts
fetch_top_post()

@app.route('/')
def index():
    global cached_post_image, cached_time

    return send_file(BytesIO(cached_post_image.content), mimetype='image/png')

if __name__ == '__main__':
    try:
        app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
    finally:
        scheduler.shutdown()