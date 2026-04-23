import os, json, asyncio, logging, re
from datetime import datetime, timezone
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

ACCOUNTS = ['OfficialJoelF', 'tradedmiami', 'growingmiami', 'Kevin_Rutois', 'RealMikeSchall']

# Set once at startup. All accounts use this as start_time on their first poll
# so we never flood with old tweets on redeploy. After the first poll finds tweets,
# each account switches to since_id tracking.
BOT_START_TIME = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

# None = never polled, 'initialized' = polled but no tweets yet, tweet_id = normal tracking
SEEN_CURSORS = {}

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

def post_tweet_to_x(text, media_ids=None):
    auth = OAuth1(
        X_CONSUMER_KEY,
        X_CONSUMER_SECRET,
        X_ACCESS_TOKEN,
        X_ACCESS_TOKEN_SECRET,
        force_include_body=False
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
                post_tweet_to_x(reworded, media_ids=media_ids if media_ids else None)
            else:
                post_tweet_to_x(reworded)
            await query.edit_message_text(f"✅ Posted!\n\n{reworded}")
        except Exception as e:
            logging.error(f"Error posting tweet: {e}")
            await query.edit_message_text(f"❌ Failed to post: {e}")
        del pending[tweet_id]
        save_json(PENDING_FILE, pending)

    elif action == 'edit':
        edit_state = {'tweet_id': tweet_id}
        save_json(EDIT_FILE, edit_state)
        reworded = pending[tweet_id]['reworded']
        await query.edit_message_text(
            f"✏️ Send me your edited version and I'll post it.\n\nCurrent version:\n{reworded}\n\n"
            f"💡 Tip: Send a photo or video with your message to replace the original media."
        )

    elif action == 'reject':
        await query.edit_message_text("❌ Skipped.")
        del pending[tweet_id]
        save_json(PENDING_FILE, pending)

async def handle_edit_reply(update, context):
    if update.message.chat_id != TELEGRAM_CHAT_ID:
        return

    edit_state = load_json(EDIT_FILE, {})
    if not edit_state.get('tweet_id'):
        return

    tweet_id = edit_state['tweet_id']
    pending = load_json(PENDING_FILE, {})

    edited_text = (
        update.message.caption
        or update.message.text
        or pending.get(tweet_id, {}).get('reworded', '')
    )

    if len(edited_text) > 280:
        await update.message.reply_text(
            f"⚠️ That's {len(edited_text)} characters — too long! Keep it under 280."
        )
        return

    try:
        media_ids = []

        if update.message.photo or update.message.video:
            if update.message.photo:
                tg_file = await update.message.photo[-1].get_file()
                ext = 'jpg'
            else:
                tg_file = await update.message.video.get_file()
                ext = 'mp4'
            path = f'/tmp/edit_media.{ext}'
            await tg_file.download_to_drive(path)
            media_id = upload_media_to_x(path)
            if media_id:
                media_ids.append(media_id)
            logging.info(f"Edit: using new media from Telegram for tweet {tweet_id}")
        else:
            media_urls = pending.get(tweet_id, {}).get('media_urls', [])
            if media_urls:
                media_files = await download_media(media_urls)
                for path in media_files:
                    media_id = upload_media_to_x(path)
                    if media_id:
                        media_ids.append(media_id)
                logging.info(f"Edit: preserving {len(media_ids)} original media item(s) for tweet {tweet_id}")

        post_tweet_to_x(edited_text, media_ids=media_ids if media_ids else None)
        await update.message.reply_text(f"✅ Posted your edited version!\n\n{edited_text}")

    except Exception as e:
        logging.error(f"Error posting edited tweet: {e}")
        await update.message.reply_text(f"❌ Failed to post: {e}")

    if tweet_id in pending:
        del pending[tweet_id]
        save_json(PENDING_FILE, pending)
    save_json(EDIT_FILE, {})

async def check_tweets(app):
    client = get_x_client()

    for account in ACCOUNTS:
        try:
            user = client.get_user(username=account)
            user_id = user.data.id
            cursor = SEEN_CURSORS.get(account)  # None | 'initialized' | tweet_id_string

            kwargs = dict(
                id=user_id,
                max_results=5,
                exclude=['retweets', 'replies'],
                expansions=['attachments.media_keys', 'referenced_tweets.id'],
                media_fields=['url', 'preview_image_url', 'type', 'variants'],
                tweet_fields=['text', 'attachments', 'entities', 'note_tweet']
            )

            if cursor and cursor != 'initialized':
                # Have a real tweet ID — use since_id for efficient polling
                kwargs['since_id'] = cursor
                logging.info(f"Polling @{account} with since_id={cursor}")
            else:
                # First poll or no tweets found yet — use the startup timestamp
                kwargs['start_time'] = BOT_START_TIME
                logging.info(f"Polling @{account} with start_time={BOT_START_TIME}")

            response = client.get_users_tweets(**kwargs)

            if response.data:
                SEEN_CURSORS[account] = str(response.data[0].id)
                logging.info(f"@{account}: {len(response.data)} new tweet(s), cursor -> {SEEN_CURSORS[account]}")

                media_lookup = {}
                if response.includes and 'media' in response.includes:
                    for m in response.includes['media']:
                        if m.type == 'photo':
                            media_lookup[m.media_key] = m.url
                        elif m.type in ('video', 'animated_gif'):
                            variants = [v for v in m.variants if v.get('content_type') == 'video/mp4']
                            if variants:
                                best = max(variants, key=lambda v: v.get('bit_rate', 0))
                                media_lookup[m.media_key] = best['url']

                for tweet in reversed(response.data):
                    media_urls = []
                    if hasattr(tweet, 'attachments') and tweet.attachments:
                        for key in tweet.attachments.get('media_keys', []):
                            if key in media_lookup:
                                media_urls.append(media_lookup[key])

                    full_text = tweet.text
                    if hasattr(tweet, 'note_tweet') and tweet.note_tweet:
                        full_text = tweet.note_tweet.get('text', tweet.text)

                    reworded = reword_tweet(full_text)
                    await send_for_approval(app, str(tweet.id), full_text, reworded, account, media_urls)
                    await asyncio.sleep(2)
            else:
                # No new tweets — mark initialized so we know we've polled at least once
                if cursor is None:
                    SEEN_CURSORS[account] = 'initialized'
                logging.info(f"No new tweets for @{account}")

        except Exception as e:
            logging.error(f"Error checking @{account}: {e}")

async def poll_loop(app):
    while True:
        await check_tweets(app)
        await asyncio.sleep(900)

async def main():
    logging.info(f"Bot starting. BOT_START_TIME={BOT_START_TIME}")

    # Brief startup delay so Railway's previous container fully disconnects
    # from Telegram before we connect — avoids 409 Conflict on redeploy
    await asyncio.sleep(8)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_reply))
    app.add_handler(MessageHandler(filters.PHOTO | filters.VIDEO, handle_edit_reply))

    async with app:
        await app.initialize()
        await app.start()
        await asyncio.gather(
            poll_loop(app),
            app.updater.start_polling(drop_pending_updates=True)
        )

if __name__ == '__main__':
    asyncio.run(main())
