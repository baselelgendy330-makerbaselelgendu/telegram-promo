import asyncio
import logging
import os
import re

from telethon import TelegramClient, errors
from telethon.sessions import StringSession


def normalize_chat(value: str) -> str:
    value = value.strip()
    path = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", value, flags=re.I).rstrip("/")
    private_post = re.fullmatch(r"c/([0-9]+)/[0-9]+", path)
    if private_post:
        return str(-1000000000000 - int(private_post.group(1)))
    if path.startswith("joinchat/"):
        path = "+" + path[len("joinchat/"):]
    if path.startswith("+"):
        return "https://t.me/" + path
    return path.lstrip("@")


def chat_key(chat):
    return chat if chat.startswith("https://t.me/+") else chat.casefold()



def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing Railway variable: {name}")
    return value


API_ID = int(required_env("TG_API_ID"))
API_HASH = required_env("TG_API_HASH")
SESSION_STRING = required_env("TG_SESSION")

TARGET_CHATS = [
    normalize_chat(item)
    for item in os.getenv(
        "TG_TARGETS",
        "ChatgptPlusDeal,clashers_market_1",
    ).split(",")
    if item.strip()
]

# Keep the cancelled Elite target excluded from general broadcasts.
DISABLED_SPECIAL_TARGET = normalize_chat(
    os.getenv("TG_SPECIAL_TARGET", "elitemethodchat")
)
SECOND_SPECIAL_TARGET = normalize_chat(
    os.getenv("TG_SECOND_SPECIAL_TARGET", "gpt_nocard")
)
SECOND_SPECIAL_SOURCE = normalize_chat(
    os.getenv("TG_SECOND_SPECIAL_SOURCE", "pwivm")
)

GENERAL_TARGET_CHATS = [
    chat
    for chat in TARGET_CHATS
    if chat_key(chat)
    not in {
        chat_key(DISABLED_SPECIAL_TARGET),
        chat_key(SECOND_SPECIAL_TARGET),
    }
]

GENERAL_INTERVAL_SECONDS = int(
    os.getenv("GENERAL_PROMO_INTERVAL_SECONDS", "3600")
)
SECOND_SPECIAL_INTERVAL_SECONDS = 40 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    auto_reconnect=True,
    connection_retries=20,
    request_retries=10,
    retry_delay=5,
    flood_sleep_threshold=60,
    receive_updates=False,
)


async def reconnect() -> None:
    """Keep trying until the Telegram connection is available again."""
    while not client.is_connected():
        try:
            logging.warning("Telegram disconnected. Reconnecting...")
            await client.connect()

            if not await client.is_user_authorized():
                raise RuntimeError(
                    "TG_SESSION is no longer authorized. "
                    "Generate a new TG_SESSION."
                )

            logging.info("Telegram connection restored.")

        except Exception:
            logging.exception("Reconnect failed. Retrying in 10 seconds.")
            await asyncio.sleep(10)


async def telegram_call(operation):
    """Run one Telegram operation and retry after connection loss."""
    last_error = None

    for attempt in range(1, 4):
        try:
            await reconnect()
            return await operation()

        except (ConnectionError, OSError) as error:
            last_error = error
            logging.warning(
                "Connection lost during request (attempt %s/3).",
                attempt,
            )

            try:
                await client.disconnect()
            except Exception:
                pass

            await asyncio.sleep(attempt * 5)

    raise ConnectionError(
        "Could not restore the Telegram connection."
    ) from last_error


async def get_latest_saved_message():
    messages = await telegram_call(
        lambda: client.get_messages("me", limit=1)
    )

    if not messages:
        raise RuntimeError(
            "Saved Messages is empty. Send your promotional message "
            "to Saved Messages first."
        )

    return messages[0]


async def resolve_chat(chat):
    if not re.fullmatch(r"-[0-9]+", chat):
        return chat

    chat_id = int(chat)
    try:
        return await client.get_input_entity(chat_id)
    except ValueError:
        # A fresh StringSession may not have encountered the private group yet.
        async for dialog in client.iter_dialogs():
            if dialog.id == chat_id:
                return dialog.input_entity
        raise ValueError(f"Chat {chat_id} was not found in the sending account's dialogs.")


async def get_latest_second_special_message():
    source = await telegram_call(lambda: resolve_chat(SECOND_SPECIAL_SOURCE))
    messages = await telegram_call(
        lambda: client.get_messages(source, limit=1)
    )

    if not messages:
        raise RuntimeError(
            f"@{SECOND_SPECIAL_SOURCE} has no messages. Send the special "
            "promotional message there first."
        )

    return messages[0]


async def send_to_chat(chat, promo_message):
    chat = await telegram_call(lambda: resolve_chat(chat))
    text = promo_message.message or ""
    entities = promo_message.entities or None

    if promo_message.media:
        await telegram_call(
            lambda: client.send_file(
                chat,
                promo_message.media,
                caption=text,
                formatting_entities=entities,
            )
        )
        return

    if not text.strip():
        raise RuntimeError(
            "Latest Saved Messages item has no text or media to send."
        )

    await telegram_call(
        lambda: client.send_message(
            chat,
            text,
            formatting_entities=entities,
        )
    )


async def send_safely(chat, promo_message, source):
    try:
        await send_to_chat(chat, promo_message)
        logging.info(
            "Sent successfully to @%s from %s.",
            chat,
            source,
        )

    except errors.SlowModeWaitError as error:
        logging.warning(
            "@%s has slow mode. Waiting %s seconds.",
            chat,
            error.seconds,
        )
        await asyncio.sleep(error.seconds + 2)

    except errors.FloodWaitError as error:
        logging.warning(
            "Telegram requested a %s-second wait.",
            error.seconds,
        )
        await asyncio.sleep(error.seconds + 2)

    except (
        errors.ChatWriteForbiddenError,
        errors.UserBannedInChannelError,
    ):
        logging.error(
            "Your account cannot send messages to @%s.",
            chat,
        )

    except Exception:
        logging.exception("Failed to send to @%s", chat)


async def send_general_round():
    promo_message = await get_latest_saved_message()

    for chat in GENERAL_TARGET_CHATS:
        await send_safely(chat, promo_message, "Saved Messages")
        await asyncio.sleep(5)


async def send_second_special_round():
    promo_message = await get_latest_second_special_message()
    await send_safely(
        SECOND_SPECIAL_TARGET,
        promo_message,
        f"@{SECOND_SPECIAL_SOURCE}",
    )


async def run_general_promos():
    while True:
        try:
            await send_general_round()
        except Exception:
            logging.exception("General broadcast round failed.")

        logging.info(
            "Next general round in %s minutes.",
            GENERAL_INTERVAL_SECONDS / 60,
        )
        await asyncio.sleep(GENERAL_INTERVAL_SECONDS)


async def run_second_special_promo():
    while True:
        try:
            await send_second_special_round()
        except Exception:
            logging.exception("Second special broadcast round failed.")

        logging.info(
            "Next second special round in %s minutes.",
            SECOND_SPECIAL_INTERVAL_SECONDS / 60,
        )
        await asyncio.sleep(SECOND_SPECIAL_INTERVAL_SECONDS)


async def main():
    await reconnect()

    me = await telegram_call(client.get_me)
    logging.info("Logged in as %s", me.first_name)
    logging.info(
        "General promo: %s chats every %s minutes.",
        len(GENERAL_TARGET_CHATS),
        GENERAL_INTERVAL_SECONDS / 60,
    )
    logging.info(
        "Second special promo: @%s receives @%s every %s minutes.",
        SECOND_SPECIAL_TARGET,
        SECOND_SPECIAL_SOURCE,
        SECOND_SPECIAL_INTERVAL_SECONDS / 60,
    )

    await asyncio.gather(
        run_general_promos(),
        run_second_special_promo(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
