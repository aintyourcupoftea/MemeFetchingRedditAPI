from flask import Flask, send_file
import praw
import os
import requests
from io import BytesIO
import time

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
    
    return cached_post_image

@app.route('/')
def index():
    global cached_post_image, cached_time

    current_time = time.time()
    if cached_post_image is None or current_time - cached_time > 86400:
        # Fetch new data from Reddit API
        fetch_top_post()

    return send_file(BytesIO(cached_post_image.content), mimetype='image/png')

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))