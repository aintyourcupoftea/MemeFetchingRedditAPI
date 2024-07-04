from flask import Flask, render_template
import praw
import os
import time


app = Flask(__name__)

# Variables to store cached data
cached_post = None
cached_time = 0

# Reddit API credentials
reddit = praw.Reddit(
    client_id='if4qb7WtCGNBY4miAkjH1g',
    client_secret='ftZJP810pIrTWRbn8fUtvVoFJgrdng',
    user_agent='GithubReadMe'
)

# Function to fetch top post from r/ProgrammerHumor
def fetch_top_post():
    subreddit = reddit.subreddit('ProgrammerHumor')
    top_post = next(subreddit.top('day', limit=1))
    return top_post.url

# Route to display the top post image
@app.route('/')
def index():
    global cached_post, cached_time

    # Check if cached data exists and is still valid (within 24 hours)
    current_time = time.time()
    if cached_post is None or current_time - cached_time > 86400:
        # Fetch new data from Reddit API
        cached_post = fetch_top_post()
        cached_time = current_time

    return render_template('index.html', url=cached_post)

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
