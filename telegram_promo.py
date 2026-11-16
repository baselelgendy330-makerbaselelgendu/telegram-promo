import asyncio
import logging
import os
import re
import sqlite3
import time
from control import State, Panel
from telethon import utils
from pathlib import Path

from telethon import TelegramClient, errors, functions, types
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
    public_post = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]+)/[0-9]+", path)
    return public_post.group(1) if public_post else path.lstrip("@")


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
    flood_sleep_threshold=0,
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


class NotInGroup(Exception):
    pass


async def member_target(chat):
    if chat.startswith("https://t.me/+"):
        invite = await client(functions.messages.CheckChatInviteRequest(
            hash=chat.removeprefix("https://t.me/+")
        ))
        if not isinstance(invite, types.ChatInviteAlready):
            raise NotInGroup()
        entity = invite.chat
    else:
        numeric = re.fullmatch(r"-[0-9]+", chat)
        target = int(chat) if numeric else chat
        try:
            entity = await client.get_entity(target)
        except (ValueError, errors.ChannelPrivateError):
            if not numeric:
                raise
            # Only decide absence after a successful scan, including archived chats.
            async for dialog in client.iter_dialogs():
                if dialog.id == target:
                    return dialog.input_entity
            raise NotInGroup()
    if getattr(entity, "left", False) or getattr(entity, "kicked", False):
        raise NotInGroup()
    return entity


async def send_to_chat(chat, promo_message):
    chat = await telegram_call(lambda: member_target(chat))
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


SEND_GAP = max(1, int(os.getenv("TG_SEND_GAP_SECONDS", "30")))
send_lock = asyncio.Lock()
next_send = 0.0
slow_until = {}
panel = None

async def validate_groups(inputs):
    existing = {k for _,k,_ in state.groups()}
    excluded = {chat_key(SECOND_SPECIAL_TARGET), chat_key(DISABLED_SPECIAL_TARGET)}
    known = existing | excluded | blocked
    # Resolve existing aliases against dialogs so a username and numeric ID
    # cannot create two deliveries to the same group.
    try:
        async for dialog in client.iter_dialogs():
            aliases = {str(dialog.id)}
            if getattr(dialog.entity,"username",None):
                aliases.add(dialog.entity.username.casefold())
            if aliases & known:
                known.update(aliases)
    except Exception:
        return [], ["تعذّر التحقق من قائمة المحادثات. حاول مجددًا."]
    found,notes = [],[]
    for raw in inputs:
        k = chat_key(normalize_chat(raw))
        if k in known:
            notes.append(f"مسجّلة أو مستبعدة بالفعل · {raw[:80]}")
            continue
        try:
            entity = await member_target(k)
            if not isinstance(entity,(types.Chat,types.Channel)):
                entity = await client.get_entity(entity)
            if not isinstance(entity,(types.Chat,types.Channel)) or getattr(entity,"broadcast",False):
                raise ValueError()
            ident = str(utils.get_peer_id(entity))
            aliases = {ident,k}
            if getattr(entity,"username",None):
                aliases.add(entity.username.casefold())
            # Imported invite hashes do not appear in the dialogs listing.
            for invite in known.copy():
                if invite.startswith("https://t.me/+") and ident not in known:
                    try:
                        known.add(str(utils.get_peer_id(await member_target(invite))))
                    except (NotInGroup,errors.InviteHashExpiredError,errors.InviteHashInvalidError):
                        pass
            if aliases & known:
                notes.append(f"مسجّلة أو مستبعدة بالفعل · {entity.title}")
            else:
                found.append((ident,entity.title))
                known.update(aliases)
                notes.append(f"• {entity.title} ({ident})")
        except errors.FloodWaitError as exc:
            with database:
                state.set("flood", max(state.get("flood",0),time.time()+exc.seconds+2))
            notes.append("طلب تيليجرام الانتظار. أعد إرسال بقية الأسماء لاحقًا.")
            break
        except Exception:
            notes.append(f"تعذّر التحقق من المجموعة أو الوصول إليها · {raw[:80]}")
    return found,notes


async def send_safely(chat, message, general):
    global next_send
    key = chat_key(chat)
    async with send_lock:
        while True:
            if key in blocked:
                return "skip","القائمة السوداء"
            if general and not database.execute("SELECT 1 FROM targets WHERE account=? AND k=?", (account,key)).fetchone():
                return "skip","حُذفت المجموعة"
            if slow_until.get(key,0)>time.time():
                return "skip","الوضع البطيء"
            if state.get("paused"):
                await state.wait()
                continue
            delay = max(next_send,state.get("flood",0))-time.time()
            if delay<=0:
                break
            await state.wait(delay)
        try:
            await send_to_chat(chat,message)
            logging.info("Sent to %s",chat)
            return "ok",""
        except (NotInGroup,errors.UserNotParticipantError):
            with database:
                database.execute("INSERT OR IGNORE INTO blacklist VALUES (?,?)",(account,key))
            blocked.add(key)
            return "skip","الحساب خارج المجموعة"
        except errors.SlowModeWaitError as exc:
            slow_until[key] = time.time()+exc.seconds+2
            return "skip",f"الوضع البطيء · {exc.seconds} ثانية"
        except errors.FloodWaitError as exc:
            with database:
                state.set("flood",time.time()+exc.seconds+2)
            return "fail",f"انتظار عام فرضه تيليجرام · {exc.seconds} ثانية"
        except (errors.ChatWriteForbiddenError,errors.UserBannedInChannelError):
            return "skip","الإرسال مقيّد"
        except Exception as exc:
            logging.exception("Skipping %s",chat)
            return "fail",type(exc).__name__
        finally:
            next_send = time.time()+SEND_GAP


async def worker(kind):
    general = kind=="general"
    active, due = "active_"+kind, "due_"+kind
    while True:
        r = state.get(active)
        if state.get("paused"):
            await state.wait()
            continue
        delay = max(state.get("flood",0), 0 if r else state.get(due,0))-time.time()
        if delay>0:
            await state.wait(delay)
            continue
        if not r:
            targets = [k for _,k,_ in state.groups()] if general else [SECOND_SPECIAL_TARGET]
            r = dict(targets=targets,index=0,ok=0,skip=0,fail=0,issues=[],message_id=None)
            with database:
                state.set(active,r)
        try:
            if r["index"]<len(r["targets"]):
                if not general and chat_key(SECOND_SPECIAL_TARGET) in blocked:
                    r.update(index=1,skip=1,issues=["المجموعة الخاصة · القائمة السوداء"])
                else:
                    if r["message_id"] is None:
                        msg = await (get_latest_saved_message() if general else get_latest_second_special_message())
                        r["message_id"] = msg.id
                        with database:
                            state.set(active,r)
                    else:
                        src = "me" if general else await resolve_chat(SECOND_SPECIAL_SOURCE)
                        msg = await telegram_call(lambda:client.get_messages(src,ids=r["message_id"]))
                        if not msg:
                            raise ValueError("Source message unavailable")
                    while r["index"]<len(r["targets"]):
                        k = r["targets"][r["index"]]
                        outcome,reason = await send_safely(k,msg,general)
                        r[outcome]+=1
                        r["index"]+=1
                        if reason:
                            r["issues"].append(f"{k} · {reason}")
                        with database:
                            state.set(active,r)
        except errors.FloodWaitError as exc:
            with database:
                state.set("flood",time.time()+exc.seconds+2)
            continue
        except Exception as exc:
            logging.exception("Promo source failed")
            r["fail"]+=len(r["targets"])-r["index"]
            r["index"]=len(r["targets"])
            r["issues"].append("تعذّرت قراءة رسالة المصدر · "+type(exc).__name__)
        interval = state.get("interval") if general else SECOND_SPECIAL_INTERVAL_SECONDS
        with database:
            state.set(active,None)
            state.set("last_"+kind,r)
            state.set(due,time.time()+interval)
        if panel:
            name = "العامة" if general else "الخاصة"
            try:
                await panel.api("sendMessage",chat_id=panel.admin,text=(
                    f"اكتملت الجولة {name}\nنجح {r['ok']} · فشل {r['fail']} · تم تخطي {r['skip']}"))
            except Exception as exc:
                logging.warning("Round notification failed (%s)",type(exc).__name__)


async def main():
    global database, blocked, account, state, panel
    await reconnect()
    account = (await telegram_call(client.get_me)).id
    root = Path(os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ".")
    database = sqlite3.connect(root / "promo_blacklist.sqlite3")
    database.execute("CREATE TABLE IF NOT EXISTS blacklist (account INTEGER, target TEXT, PRIMARY KEY(account,target))")
    blocked = {r[0] for r in database.execute("SELECT target FROM blacklist WHERE account=?",(account,))}
    state = State(database,account,[chat_key(k) for k in GENERAL_TARGET_CHATS],GENERAL_INTERVAL_SECONDS)
    token, admin = os.getenv("TG_BOT_TOKEN"),os.getenv("TG_ADMIN_ID")
    if bool(token)!=bool(admin):
        raise RuntimeError("Set both TG_BOT_TOKEN and TG_ADMIN_ID")
    jobs = []
    if token:
        panel = Panel(token,int(admin),state,validate_groups,blocked)
        jobs.append(panel.run())
    await asyncio.gather(worker("general"),worker("special"),*jobs)


if __name__ == "__main__":
    asyncio.run(main())
