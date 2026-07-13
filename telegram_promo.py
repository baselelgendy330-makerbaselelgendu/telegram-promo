import asyncio
import logging
import os

from telethon import TelegramClient, errors
from telethon.sessions import StringSession


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing Railway variable: {name}")
    return value


API_ID = int(required_env("TG_API_ID"))
API_HASH = required_env("TG_API_HASH")
SESSION_STRING = required_env("TG_SESSION")

TARGET_CHATS = [
    item.strip().replace("https://t.me/", "").lstrip("@")
    for item in os.getenv(
        "TG_TARGETS",
        "ChatgptPlusDeal,clashers_market_1",
    ).split(",")
    if item.strip()
]

INTERVAL_SECONDS = int(os.getenv("PROMO_INTERVAL_SECONDS", "480"))

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


async def send_to_chat(chat, promo_message):
    await telegram_call(
        lambda: client.send_message(chat, promo_message)
    )


async def send_round():
    promo_message = await get_latest_saved_message()

    for chat in TARGET_CHATS:
        try:
            await send_to_chat(chat, promo_message)
            logging.info("Sent successfully to @%s", chat)

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

        await asyncio.sleep(5)


async def main():
    await reconnect()

    me = await telegram_call(client.get_me)
    logging.info("Logged in as %s", me.first_name)
    logging.info(
        "Broadcaster running for %s chats every %s seconds.",
        len(TARGET_CHATS),
        INTERVAL_SECONDS,
    )

    while True:
        try:
            await send_round()
        except Exception:
            logging.exception("Broadcast round failed.")

        logging.info(
            "Next round in %s minutes.",
            INTERVAL_SECONDS / 60,
        )
        await asyncio.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
