from flask import Flask, send_file
from flask_cors import CORS  # Import Flask-CORS
import praw
import requests
from io import BytesIO
import random
import os

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Reddit API credentials
reddit = praw.Reddit(
    client_id='if4qb7WtCGNBY4miAkjH1g',
    client_secret='ftZJP810pIrTWRbn8fUtvVoFJgrdng',
    user_agent='GithubReadMe'
)

# Function to fetch top posts URL and image from r/ProgrammerHumor
def fetch_top_posts():
    subreddit = reddit.subreddit('ProgrammerHumor')
    top_posts = list(subreddit.top('day', limit=5))  # Fetch top 5 posts of the day
    random_post = random.choice(top_posts)
    
    post_url = random_post.url
    post_image = requests.get(post_url)
    
    return post_image.content

@app.route('/')
def index():
    image_data = fetch_top_posts()
    return send_file(BytesIO(image_data), mimetype='image/png')

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", 7860)))
