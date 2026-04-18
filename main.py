import os, json, asyncio, logging, httpx
import tweepy
import anthropic
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
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

ACCOUNTS = ['OfficialJoelF', 'growingmiami']
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
    import requests
    from requests_oauthlib import OAuth1
    
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
        logging.error(f"Media upload failed: {response.text}")
        return None

def reword_tweet(text):
    # Remove t.co links from text
    import re
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
            from telegram import InputMediaPhoto, InputMediaVideo
            media_group = []
            for path in media_files:
                if path.endswith('.mp4'):
                    media_group.append(InputMediaVideo(open(path, 'rb')))
                else:
                    media_group.append(InputMediaPhoto(open(path, 'rb')))
            if media_group:
                await app.bot.send_media_group(chat_id=TELEGRAM_CHAT_ID, media=media_group)

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

        if media_urls:
            media_files = await download_media(media_urls)
            media_ids = []
            for path in media_files:
                media_id = upload_media_to_x(path)
                if media_id:
                    media_ids.append(media_id)
            get_x_client().create_tweet(text=reworded, media_ids=media_ids if media_ids else None)
        else:
            get_x_client().create_tweet(text=reworded)

        await query.edit_message_text(f"✅ Posted!\n\n{reworded}")
        del pending[tweet_id]
        save_json(PENDING_FILE, pending)

    elif action == 'edit':
        edit_state = {'tweet_id': tweet_id}
        save_json(EDIT_FILE, edit_state)
        reworded = pending[tweet_id]['reworded']
        await query.edit_message_text(
            f"✏️ Send me your edited version and I'll post it.\n\nCurrent version:\n{reworded}"
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
    edited_text = update.message.text
    pending = load_json(PENDING_FILE, {})

    if len(edited_text) > 280:
        await update.message.reply_text(f"⚠️ That's {len(edited_text)} characters — too long! Keep it under 280.")
        return

    media_urls = pending.get(tweet_id, {}).get('media_urls', [])

    if media_urls:
        api_v1 = get_x_api_v1()
        media_files = await download_media(media_urls)
        media_ids = []
        for path in media_files:
            res = api_v1.media_upload(path)
            media_ids.append(res.media_id)
        get_x_client().create_tweet(text=edited_text, media_ids=media_ids)
    else:
        get_x_client().create_tweet(text=edited_text)

    await update.message.reply_text(f"✅ Posted your edited version!\n\n{edited_text}")

    if tweet_id in pending:
        del pending[tweet_id]
        save_json(PENDING_FILE, pending)
    save_json(EDIT_FILE, {})

async def check_tweets(app):
    client = get_x_client()
    seen = load_json(SEEN_FILE, {})

    for account in ACCOUNTS:
        try:
            user = client.get_user(username=account)
            user_id = user.data.id
            since_id = seen.get(account)

            kwargs = dict(
                id=user_id,
                max_results=5,
                exclude=['retweets', 'replies'],
                expansions=['attachments.media_keys'],
                media_fields=['url', 'preview_image_url', 'type', 'variants']
            )

            if since_id:
                kwargs['since_id'] = since_id
            else:
                kwargs['start_time'] = '2026-04-18T00:00:00Z'

            response = client.get_users_tweets(**kwargs)

            if response.data:
                seen[account] = str(response.data[0].id)
                save_json(SEEN_FILE, seen)

                # Build media lookup
                media_lookup = {}
                if response.includes and 'media' in response.includes:
                    for m in response.includes['media']:
                        if m.type == 'photo':
                            media_lookup[m.media_key] = m.url
                        elif m.type in ('video', 'animated_gif'):
                            # Get highest bitrate variant
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

                    reworded = reword_tweet(tweet.text)
                    await send_for_approval(app, str(tweet.id), tweet.text, reworded, account, media_urls)
                    await asyncio.sleep(2)

        except Exception as e:
            logging.error(f"Error checking @{account}: {e}")

async def poll_loop(app):
    while True:
        await check_tweets(app)
        await asyncio.sleep(900)

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_edit_reply))

    async def run():
        async with app:
            await app.initialize()
            await app.start()
            await asyncio.gather(
                poll_loop(app),
                app.updater.start_polling()
            )

    asyncio.run(run())

if __name__ == '__main__':
    main()
