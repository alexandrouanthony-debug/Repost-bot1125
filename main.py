import os, json, time, asyncio, logging
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

def reword_tweet(text):
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=280,
        messages=[{
            "role": "user",
            "content": f"Reword this tweet slightly. Keep the same facts and meaning. Keep it under 280 characters. Sound natural. Do not add hashtags unless the original has them. Only return the reworded tweet, nothing else: {text}"
        }]
    )
    return message.content[0].text

async def send_for_approval(app, tweet_id, original, reworded, account):
    pending = load_json(PENDING_FILE, {})
    pending[tweet_id] = {'original': original, 'reworded': reworded, 'account': account}
    save_json(PENDING_FILE, pending)

    keyboard = [[
        InlineKeyboardButton("✅ Post it", callback_data=f"approve_{tweet_id}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"edit_{tweet_id}"),
        InlineKeyboardButton("❌ Skip", callback_data=f"reject_{tweet_id}")
    ]]

    text = f"📢 New post from @{account}\n\n*Original:*\n{original}\n\n*Reworded:*\n{reworded}"

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
        get_x_client().create_tweet(text=reworded)
        await query.edit_message_text(f"✅ Posted!\n\n{reworded}")
        del pending[tweet_id]
        save_json(PENDING_FILE, pending)

    elif action == 'edit':
        # Save edit state so we know what to post when they reply
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
    # Only respond to messages from you
    if update.message.chat_id != TELEGRAM_CHAT_ID:
        return

    edit_state = load_json(EDIT_FILE, {})
    if not edit_state.get('tweet_id'):
        return

    tweet_id = edit_state['tweet_id']
    edited_text = update.message.text

    if len(edited_text) > 280:
        await update.message.reply_text(f"⚠️ That's {len(edited_text)} characters — too long for X! Please keep it under 280.")
        return

    get_x_client().create_tweet(text=edited_text)
    await update.message.reply_text(f"✅ Posted your edited version!\n\n{edited_text}")

    # Clear states
    pending = load_json(PENDING_FILE, {})
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

            tweets = client.get_users_tweets(
                id=user_id,
                since_id=since_id,
                max_results=5,
                exclude=['retweets', 'replies']
            )

            if tweets.data:
                seen[account] = str(tweets.data[0].id)
                save_json(SEEN_FILE, seen)

                for tweet in reversed(tweets.data):
                    reworded = reword_tweet(tweet.text)
                    await send_for_approval(app, str(tweet.id), tweet.text, reworded, account)
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

    loop = asyncio.get_event_loop()
    loop.create_task(poll_loop(app))
    app.run_polling()

if __name__ == '__main__':
    main()
