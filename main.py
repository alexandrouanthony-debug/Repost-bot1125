import os, json, asyncio, logging, re
import httpx
import requests
import tweepy
import anthropic
from requests_oauthlib import OAuth1
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

logging.basicConfig(level=logging.INFO)

X_CONSUMER_KEY = os.environ['X_CONSUMER_KEY']
X_CONSUMER_SECRET = os.environ['X_CONSUMER_SECRET']
X_BEARER_TOKEN = os.environ['X_BEARER_TOKEN']
X_ACCESS_TOKEN = os.environ['X_ACCESS_TOKEN']
X_ACCESS_TOKEN_SECRET = os.environ['X_ACCESS_TOKEN_SECRET']
TELEGRAM_BOT_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
TELEGRAM_CHAT_ID = int(os.environ['TELEGRAM_CHAT_ID'])
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']

ACCOUNTS = ['OfficialJoelF', 'tradedmiami']
SEEN_FILE = 'seen_tweets.json'
PENDING_FILE = 'pending_tweets.json'
EDIT_FILE = 'edit_state.json'

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default

def save_json(path, data):
    with open(path, 'w') as f:
        json.dump(data, f)

def get_x_client():
    return tweepy.Client(
        bearer_token=X_BEARER_TOKEN,
        consumer_key=X_CONSUMER_KEY,
        consumer_secret=X_CONSUMER_SECRET,
        access_token=X_ACCESS_TOKEN,
        access_token_secret=X_ACCESS_TOKEN_SECRET
    )

def upload_media_to_x(file_path):
    auth = OAuth1(
        X_CONSUMER_KEY,
        X_CONSUMER_SECRET,
        X_ACCESS_TOKEN,
        X_ACCESS_TOKEN_SECRET
    )
    with open(file_path, 'rb') as f:
        files = {'media': f}
        response = requests.post(
            'https://upload.twitter.com/1.1/media/upload.json',
            auth=auth,
            files=files
        )
    if response.status_code == 200:
        return response.json()['media_id_string']
    else:
        logging.error(f"Media upload failed: {response.status_code} {response.text}")
        return None

def post_tweet_to_x(text, media_ids=None):
    auth = OAuth1(
        X_CONSUMER_KEY,
        X_CONSUMER_SECRET,
        X_ACCESS_TOKEN,
        X_ACCESS_TOKEN_SECRET
    )
    payload = {"text": text}
    if media_ids:
        payload["media"] = {"media_ids": media_ids}
    response = requests.post(
        'https://api.twitter.com/2/tweets',
        auth=auth,
        json=payload
    )
    logging.info(f"Tweet response: {response.status_code} {response.text}")
    if response.status_code not in (200, 201):
        raise Exception(f"{response.status_code} {response.text}")
    return response.json()

def reword_tweet(text):
    text = re.sub(r'https://t\.co/\S+', '', text).strip()
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=280,
        messages=[{
            "role": "user",
            "content": f"Reword this tweet slightly. Keep the same facts and meaning. Keep it under 280 characters. Sound natural. Preserve any bullet points or list formatting from the original. Do not add hashtags unless the original has them. Do not add any URLs or links. Only return the reworded tweet, nothing else: {text}"
        }]
    )
    return message.content[0].text

async def download_media(urls):
    files = []
    async with httpx.AsyncClient() as client:
        for i, url in enumerate(urls):
            try:
                r = await client.get(url, follow_redirects=True)
                ext = 'mp4' if 'video' in r.headers.get('content-type', '') else 'jpg'
                path = f'/tmp/media_{i}.{ext}'
                with open(path, 'wb') as f:
                    f.write(r.content)
                files.append(path)
            except Exception as e:
                logging.error(f"Error downloading media: {e}")
    return files

async def send_for_approval(app, tweet_id, original, reworded, account, media_urls=[]):
    pending = load_json(PENDING_FILE, {})
    pending[tweet_id] = {
        'original': original,
        'reworded': reworded,
        'account': account,
        'media_urls': media_urls
    }
    save_json(PENDING_FILE, pending)

    keyboard = [[
        InlineKeyboardButton("✅ Post it", callback_data=f"approve_{tweet_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{tweet_id}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"reject_{tweet_id}")
    ]]

    text = f"📢 New post from @{account}\n\n*Original:*\n{original}\n\n*Reworded:*\n{reworded}"

    if media_urls:
        media_files = await download_media(media_urls)
        if media_files:
            media_group = []
            for path in media_files:
                if path.endswith('.mp4'):
                    media_group.append(InputMediaVideo(open(path, 'rb')))
                else:
                    media_group.append(InputMediaPhoto(open(path, 'rb')))
            if media_group:
                try:
                    await app.bot.send_media_group(chat_id=TELEGRAM_CHAT_ID, media=media_group)
                except Exception as e:
                    logging.error(f"Error sending media group: {e}")

    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode='Markdown',
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def handle_button(update, context):
    query = update.callback_query
    await query.answer()

    action, tweet_id = query.data.split('_', 1)
    pending = load_json(PENDING_FILE, {})

    if tweet_id not in pending:
        await query.edit_message_text("Already handled.")
        return

    if action == 'approve':
        reworded = pending[tweet_id]['reworded']
        media_urls = pending[tweet_id].get('media_urls', [])
        try:
            if media_urls:
                media_files = await download_media(media_urls)
                media_ids = []
                for path in media_files:
                    media_id = upload_media_to_x(path)
                    if media_id:
                        media_ids.append(media_id)
                post_tweet_to
