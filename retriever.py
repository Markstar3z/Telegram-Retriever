import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient, events
from telethon.errors import (
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    RPCError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
)
from telethon.sessions import MemorySession, StringSession
from telethon.tl.types import (
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChannelParticipantsAdmins,
    UserStatusEmpty,
    UserStatusLastMonth,
    UserStatusLastWeek,
    UserStatusOffline,
    UserStatusOnline,
    UserStatusRecently,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

MAX_ACTIVE_ADMINS = 3
MESSAGE_CHUNK_SIZE = 3800
DELAY_BETWEEN_PROJECTS = 1

user_client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH,
    auto_reconnect=True,
    connection_retries=10,
    retry_delay=3,
    request_retries=5,
)

bot_client = TelegramClient(
    MemorySession(),
    API_ID,
    API_HASH,
    auto_reconnect=True,
    connection_retries=10,
    retry_delay=3,
    request_retries=5,
)

active_jobs: set[int] = set()
user_connection_lock = asyncio.Lock()


def clean_url(url: str) -> str:
    """Remove labels and punctuation accidentally attached to a URL."""

    value = url.strip()
    value = re.sub(r"^(?:X|TWITTER|TG|TELEGRAM)\s*:\s*", "", value, flags=re.I)
    return value.rstrip(".,;:!?)]}>\"'")


def normalize_url(url: str) -> str:
    """Normalize X and Telegram links, including links without a scheme."""

    url = clean_url(url)

    if not re.match(r"^https?://", url, flags=re.I):
        url = "https://" + url.lstrip("/")

    url = re.sub(r"^http://", "https://", url, flags=re.I)
    url = re.sub(r"^https://www\.", "https://", url, flags=re.I)

    return url.rstrip("/")


def extract_links_from_line(line: str) -> list[str]:
    """Extract X, Twitter and Telegram links from any part of a line."""

    pattern = re.compile(
        r"(?:https?://)?(?:www\.)?"
        r"(?:x\.com|twitter\.com|t\.me)/[^\s<>()\[\]{}]+",
        flags=re.I,
    )

    return [normalize_url(match.group(0)) for match in pattern.finditer(line)]


def is_x_link(url: str) -> bool:
    """Check whether a URL is an X or Twitter profile link."""

    return bool(
        re.match(
            r"https://(?:x\.com|twitter\.com)/"
            r"[A-Za-z0-9_]+(?:/.*)?$",
            normalize_url(url),
            flags=re.I,
        )
    )


def is_telegram_link(url: str) -> bool:
    """Check whether a URL is a Telegram link."""

    return bool(
        re.match(
            r"https://t\.me/",
            normalize_url(url),
            flags=re.I,
        )
    )


def extract_x_username(url: str) -> Optional[str]:
    """Extract the username from an X or Twitter URL."""

    match = re.match(
        r"https://(?:x\.com|twitter\.com)/([A-Za-z0-9_]+)",
        normalize_url(url),
        flags=re.I,
    )

    return match.group(1) if match else None


def extract_public_tg_username(url: str) -> Optional[str]:
    """Extract a public Telegram username from a t.me URL."""

    match = re.match(
        r"https://t\.me/([A-Za-z0-9_]+)",
        normalize_url(url),
        flags=re.I,
    )

    if not match:
        return None

    username = match.group(1)

    if username.lower() in {
        "joinchat",
        "share",
        "addstickers",
        "proxy",
        "socks",
        "c",
        "iv",
    }:
        return None

    return username


def is_private_invite_link(url: str) -> bool:
    """Identify private Telegram invitation links."""

    return bool(
        re.match(
            r"https://t\.me/(?:\+|joinchat/)",
            normalize_url(url),
            flags=re.I,
        )
    )


def derive_project_name(x_link: str) -> str:
    """Create a readable fallback project name from the X username."""

    username = extract_x_username(x_link)

    if not username:
        return "Unknown Project"

    known_names = {
        "taggerai": "Tagger AI",
        "iota": "IOTA",
        "thorchain": "THORChain",
        "curvefinance": "Curve Finance",
        "1inch": "1inch",
    }

    if username.lower() in known_names:
        return known_names[username.lower()]

    cleaned = username.replace("_", " ").strip()

    return " ".join(
        word.upper() if len(word) <= 3 else word.capitalize()
        for word in cleaned.split()
    )


def clean_project_name(line: str) -> str:
    """Remove list numbering and labels from a supplied project name."""

    value = line.strip()
    value = re.sub(r"^\s*\d+\s*[.)-]\s*", "", value)
    value = re.sub(r"^Project\s*:\s*", "", value, flags=re.I)
    return value.strip()


def pair_project_links(text: str) -> list[dict]:
    """
    Parse all of these formats:

    Tagger AI
    https://x.com/taggerai
    https://t.me/Tagger_DATA

    1. Tagger AI
    X: https://twitter.com/taggerai
    TG: https://t.me/Tagger_DATA

    https://x.com/taggerai
    https://t.me/Tagger_DATA
    """

    projects: list[dict] = []
    current: Optional[dict] = None
    pending_name: Optional[str] = None

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            continue

        links = extract_links_from_line(line)

        if links:
            for link in links:
                if is_x_link(link):
                    if current is not None:
                        projects.append(current)

                    current = {
                        "project_name": (
                            pending_name
                            if pending_name
                            else derive_project_name(link)
                        ),
                        "x_link": link,
                        "tg_link": None,
                    }
                    pending_name = None

                elif is_telegram_link(link):
                    if current is not None and current["tg_link"] is None:
                        current["tg_link"] = link

            continue

        candidate_name = clean_project_name(line)

        if candidate_name and not candidate_name.lower() in {
            "owner",
            "admins",
            "admin",
            "top active admins",
            "status",
        }:
            pending_name = candidate_name

    if current is not None:
        projects.append(current)

    return projects

def mark_duplicates(projects: list[dict]) -> list[dict]:
    seen_x: set[str] = set()
    seen_tg: set[str] = set()

    for project in projects:
        x_user = extract_x_username(project["x_link"])
        x_identity = x_user.lower() if x_user else None
        tg_link = project.get("tg_link")
        tg_identity = tg_link.lower() if tg_link else None

        reasons: list[str] = []
        if x_identity and x_identity in seen_x:
            reasons.append("duplicate X account")
        if tg_identity and tg_identity in seen_tg:
            reasons.append("duplicate Telegram group")

        project["is_duplicate"] = bool(reasons)
        project["duplicate_reason"] = ", ".join(reasons)

        if x_identity:
            seen_x.add(x_identity)
        if tg_identity:
            seen_tg.add(tg_identity)

    return projects


def ensure_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def describe_activity(status) -> dict:
    now = datetime.now(timezone.utc)

    if isinstance(status, UserStatusOnline):
        return {"text": "Online now", "rank": 6, "timestamp": now.timestamp()}

    if isinstance(status, UserStatusOffline):
        last_seen = ensure_utc(status.was_online)
        age = (now - last_seen).total_seconds()
        rank = 5 if age <= 86400 else 4 if age <= 604800 else 3 if age <= 2592000 else 2
        return {
            "text": f"Last seen: {last_seen.strftime('%d %b %Y, %H:%M UTC')}",
            "rank": rank,
            "timestamp": last_seen.timestamp(),
        }

    if isinstance(status, UserStatusRecently):
        return {"text": "Recently active", "rank": 4, "timestamp": 0}
    if isinstance(status, UserStatusLastWeek):
        return {"text": "Active within a week", "rank": 3, "timestamp": 0}
    if isinstance(status, UserStatusLastMonth):
        return {"text": "Active within a month", "rank": 2, "timestamp": 0}
    if isinstance(status, UserStatusEmpty):
        return {"text": "Activity hidden", "rank": 1, "timestamp": 0}
    return {"text": "Activity unavailable", "rank": 0, "timestamp": 0}


async def ensure_user_client_connected() -> None:
    async with user_connection_lock:
        for attempt in range(1, 4):
            try:
                if not user_client.is_connected():
                    print(f"Reconnect attempt {attempt}/3...")
                    await user_client.connect()

                if not await user_client.is_user_authorized():
                    raise RuntimeError("STRING_SESSION is invalid or no longer authorized.")

                await user_client.get_me()
                return
            except Exception as error:
                print(f"Reconnect attempt {attempt}/3 failed: {error}")
                try:
                    if user_client.is_connected():
                        await user_client.disconnect()
                except Exception:
                    pass
                if attempt < 3:
                    await asyncio.sleep(attempt * 2)

        raise RuntimeError("Personal Telegram account could not reconnect after three attempts.")


async def get_priority_admins(group_link: str) -> dict:
    await ensure_user_client_connected()
    entity = await user_client.get_entity(group_link)

    owner: Optional[dict] = None
    admins: list[dict] = []

    async for user in user_client.iter_participants(entity, filter=ChannelParticipantsAdmins()):
        participant = getattr(user, "participant", None)
        is_owner = isinstance(participant, ChannelParticipantCreator)
        is_admin = isinstance(participant, ChannelParticipantAdmin)

        if not is_owner and not is_admin:
            continue
        if getattr(user, "bot", False):
            continue

        username = getattr(user, "username", None)
        if not username:
            continue

        activity = describe_activity(getattr(user, "status", None))
        data = {
            "username": f"@{username}",
            "role": "Owner" if is_owner else "Admin",
            "activity": activity["text"],
            "activity_rank": activity["rank"],
            "activity_timestamp": activity["timestamp"],
        }

        if is_owner:
            owner = data
        else:
            admins.append(data)

    admins.sort(key=lambda item: (item["activity_rank"], item["activity_timestamp"]), reverse=True)
    return {"owner": owner, "active_admins": admins[:MAX_ACTIVE_ADMINS]}


def usernames_only(admins: list[dict]) -> str:
    names = [admin["username"] for admin in admins if admin.get("username")]
    return "\n".join(names) if names else "None found"


def format_accessible_project(position: int, project: dict, owner: Optional[dict], admins: list[dict]) -> str:
    sections = [
        f"{position}. {project['project_name']}",
        f"X: {project['x_link']}\nTG: {project['tg_link']}",
    ]

    if owner and owner.get("username"):
        sections.append(f"Owner\n\n{owner['username']}")

    sections.append("Admins\n\n" + usernames_only(admins))
    return "\n\n".join(sections)


def format_unavailable_project(position: int, project: dict, status: str, reason: Optional[str] = None) -> str:
    block = (
        f"{position}. {project['project_name']}\n\n"
        f"X: {project['x_link']}\n"
        f"TG: {project.get('tg_link') or 'Not provided'}\n\n"
        f"Status: {status}"
    )
    return block + (f"\nReason: {reason}" if reason else "")


async def safe_reply(event, text: str):
    try:
        return await event.reply(text, link_preview=False)
    except FloodWaitError as error:
        await asyncio.sleep(error.seconds + 1)
        return await event.reply(text, link_preview=False)
    except RPCError as error:
        print(f"Reply error: {error}")
        return None


async def safe_edit(message, text: str):
    if message is None:
        return
    try:
        await message.edit(text, link_preview=False)
    except FloodWaitError as error:
        await asyncio.sleep(error.seconds + 1)
        try:
            await message.edit(text, link_preview=False)
        except RPCError as retry_error:
            print(f"Progress edit retry failed: {retry_error}")
    except RPCError as error:
        if "message not modified" not in str(error).lower():
            print(f"Progress edit error: {error}")


def split_long_output(blocks: list[str]) -> list[str]:
    chunks: list[str] = []
    current = ""

    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= MESSAGE_CHUNK_SIZE:
            current = candidate
            continue

        if current:
            chunks.append(current)

        if len(block) <= MESSAGE_CHUNK_SIZE:
            current = block
        else:
            for start in range(0, len(block), MESSAGE_CHUNK_SIZE):
                chunks.append(block[start:start + MESSAGE_CHUNK_SIZE])
            current = ""

    if current:
        chunks.append(current)
    return chunks


def build_progress(total: int, completed: int, successful: int, failed: int, current: str) -> str:
    percentage = int((completed / total) * 100) if total else 0
    return (
        "⏳ Processing project list\n\n"
        f"Progress: {completed}/{total} ({percentage}%)\n"
        f"Successful: {successful}\n"
        f"Failed or unavailable: {failed}\n\n"
        f"Current: {current}"
    )


async def process_project_list(event, text: str):
    chat_id = event.chat_id
    if chat_id in active_jobs:
        await safe_reply(event, "⚠️ A list is already being processed in this chat.")
        return

    active_jobs.add(chat_id)

    try:
        projects = mark_duplicates(pair_project_links(text))
        if not projects:
            await safe_reply(event, "❌ I could not find any valid X project links.")
            return

        accessible: list[str] = []
        unavailable: list[str] = []
        successful = 0
        failed = 0

        progress = await safe_reply(event, build_progress(len(projects), 0, 0, 0, "Preparing batch..."))

        for position, project in enumerate(projects, start=1):
            current_status = "Checking..."
            tg_link = project.get("tg_link")

            if project["is_duplicate"]:
                failed += 1
                current_status = "Duplicate skipped"
                unavailable.append(
                    format_unavailable_project(position, project, "⚠️ Duplicate skipped", project["duplicate_reason"])
                )

            elif not tg_link:
                failed += 1
                current_status = "Missing Telegram link"
                unavailable.append(format_unavailable_project(position, project, "⚠️ Missing Telegram link"))

            elif is_private_invite_link(tg_link):
                failed += 1
                current_status = "Private invite link"
                unavailable.append(format_unavailable_project(position, project, "🔒 Private invite link"))

            elif not extract_public_tg_username(tg_link):
                failed += 1
                current_status = "Invalid Telegram link"
                unavailable.append(format_unavailable_project(position, project, "❌ Invalid Telegram group link"))

            else:
                try:
                    result = await get_priority_admins(tg_link)
                    owner = result["owner"]
                    admins = result["active_admins"]

                    if owner or admins:
                        successful += 1
                        current_status = "Success"
                        accessible.append(format_accessible_project(position, project, owner, admins))
                    else:
                        failed += 1
                        current_status = "No public human admins"
                        unavailable.append(format_unavailable_project(position, project, "⚠️ No public human admins"))

                except FloodWaitError as error:
                    await safe_edit(progress, build_progress(len(projects), position - 1, successful, failed, f"Waiting {error.seconds} seconds"))
                    await asyncio.sleep(error.seconds + 1)
                    try:
                        result = await get_priority_admins(tg_link)
                        owner = result["owner"]
                        admins = result["active_admins"]
                        if owner or admins:
                            successful += 1
                            current_status = "Success after waiting"
                            accessible.append(format_accessible_project(position, project, owner, admins))
                        else:
                            failed += 1
                            current_status = "No public human admins"
                            unavailable.append(format_unavailable_project(position, project, "⚠️ No public human admins"))
                    except Exception as retry_error:
                        failed += 1
                        current_status = "Failed after waiting"
                        unavailable.append(format_unavailable_project(position, project, "❌ Failed after waiting", str(retry_error)))

                except (ChannelPrivateError, ChatAdminRequiredError):
                    failed += 1
                    current_status = "Group inaccessible"
                    unavailable.append(
                        format_unavailable_project(
                            position,
                            project,
                            "🔒 Group inaccessible",
                            "Your Telegram account cannot access the administrator list",
                        )
                    )

                except (UsernameInvalidError, UsernameNotOccupiedError, ValueError):
                    failed += 1
                    current_status = "Group not found"
                    unavailable.append(format_unavailable_project(position, project, "❌ Group not found or invalid"))

                except (InviteHashInvalidError, InviteHashExpiredError):
                    failed += 1
                    current_status = "Invalid private invitation"
                    unavailable.append(format_unavailable_project(position, project, "🔒 Invalid or expired private invitation"))

                except RPCError as error:
                    failed += 1
                    current_status = "Telegram error"
                    unavailable.append(format_unavailable_project(position, project, "❌ Telegram error", str(error)))

                except Exception as error:
                    failed += 1
                    current_status = "Unexpected failure"
                    unavailable.append(format_unavailable_project(position, project, "❌ Unexpected failure", str(error)))

            await safe_edit(
                progress,
                build_progress(
                    len(projects),
                    position,
                    successful,
                    failed,
                    f"{position}. {project['project_name']}\nResult: {current_status}",
                ),
            )
            await asyncio.sleep(DELAY_BETWEEN_PROJECTS)

        await safe_edit(
            progress,
            (
                "✅ Batch completed\n\n"
                f"Projects submitted: {len(projects)}\n"
                f"Accessible: {len(accessible)}\n"
                f"Inaccessible or unavailable: {len(unavailable)}"
            ),
        )

        if accessible:
            chunks = split_long_output(accessible)
            for index, chunk in enumerate(chunks, start=1):
                part = f"Part {index}/{len(chunks)}\n\n" if len(chunks) > 1 else ""
                await safe_reply(event, f"✅ ACCESSIBLE PROJECTS\nTotal: {len(accessible)}\n\n{part}{chunk}")
                await asyncio.sleep(0.5)
        else:
            await safe_reply(event, "✅ ACCESSIBLE PROJECTS\nTotal: 0")

        if unavailable:
            chunks = split_long_output(unavailable)
            for index, chunk in enumerate(chunks, start=1):
                part = f"Part {index}/{len(chunks)}\n\n" if len(chunks) > 1 else ""
                await safe_reply(event, f"❌ INACCESSIBLE / UNAVAILABLE PROJECTS\nTotal: {len(unavailable)}\n\n{part}{chunk}")
                await asyncio.sleep(0.5)
        else:
            await safe_reply(event, "❌ INACCESSIBLE / UNAVAILABLE PROJECTS\nTotal: 0")

    finally:
        active_jobs.discard(chat_id)


@bot_client.on(events.NewMessage(pattern=r"^/start(?:@\w+)?$"))
async def start_handler(event):
    await event.reply(
        "Send projects in either format:\n\n"
        "Tagger AI\n"
        "https://x.com/taggerai\n"
        "https://t.me/Tagger_DATA\n\n"
        "or simply:\n\n"
        "https://x.com/taggerai\n"
        "https://t.me/Tagger_DATA",
        link_preview=False,
    )


@bot_client.on(events.NewMessage(pattern=r"^/help(?:@\w+)?$"))
async def help_handler(event):
    await event.reply(
        "Recommended format:\n\n"
        "Project Name\n"
        "X link\n"
        "Telegram link\n\n"
        "The bot keeps the owner separate and returns three active human admins.",
        link_preview=False,
    )


@bot_client.on(events.NewMessage)
async def project_list_handler(event):
    """Receive and parse project lists in several common formats."""

    text = event.raw_text.strip() if event.raw_text else ""

    if not text or text.startswith("/"):
        return

    projects = pair_project_links(text)

    if not projects:
        await event.reply(
            "❌ I could not detect an X/Twitter project link.\n\n"
            "Accepted examples:\n"
            "https://x.com/taggerai\n"
            "https://twitter.com/taggerai\n"
            "x.com/taggerai\n"
            "X: https://x.com/taggerai",
            link_preview=False,
        )
        return

    await process_project_list(event, text)


@bot_client.on(events.NewMessage(pattern=r"^/version(?:@\w+)?$"))
async def version_handler(event):
    await event.reply(
        "Admin Retriever v3.1 — flexible project-name and link parser",
        link_preview=False,
    )


async def user_client_keepalive() -> None:
    while True:
        try:
            await ensure_user_client_connected()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"Personal Telegram keepalive failed: {error}")
        await asyncio.sleep(30)


async def disconnect_clients():
    for name, client in (("Bot", bot_client), ("Personal account", user_client)):
        try:
            if client.is_connected():
                await client.disconnect()
            print(f"{name} disconnected.")
        except Exception as error:
            print(f"Could not disconnect {name}: {error}")


async def main():
    keepalive_task = None

    try:
        print("Starting Telegram bot...")
        await bot_client.start(bot_token=BOT_TOKEN)
        bot_account = await bot_client.get_me()
        print(f"Bot connected: @{bot_account.username}")

        try:
            await ensure_user_client_connected()
            personal = await user_client.get_me()
            name = f"@{personal.username}" if personal.username else personal.first_name
            print(f"Personal account connected: {name}")
        except Exception as error:
            print(f"Personal Telegram session is temporarily unavailable: {error}")

        keepalive_task = asyncio.create_task(user_client_keepalive())
        print("PROJECT ADMIN RETRIEVER IS RUNNING")
        await bot_client.run_until_disconnected()

    finally:
        if keepalive_task is not None:
            keepalive_task.cancel()
            await asyncio.gather(keepalive_task, return_exceptions=True)
        await disconnect_clients()


def run():
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by the user.")
    except Exception as error:
        print(f"The bot stopped because of an error: {error}")
        raise


if __name__ == "__main__":
    run()
