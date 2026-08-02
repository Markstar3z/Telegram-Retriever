import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Optional

from telethon import TelegramClient, events
from telethon.sessions import MemorySession, StringSession
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


# =========================================================
# CONFIGURATION
# =========================================================

# Add these four values in Railway under the worker service's Variables tab.
BOT_TOKEN = os.environ["BOT_TOKEN"]
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]

# Owner is returned separately, plus this number of active admins.
MAX_ACTIVE_ADMINS = 3

MESSAGE_CHUNK_SIZE = 3800
DELAY_BETWEEN_PROJECTS = 1
PROGRESS_UPDATE_INTERVAL = 1


# =========================================================
# TELETHON CLIENTS
# =========================================================

# Personal Telegram account used to inspect groups.
user_client = TelegramClient(
    StringSession(STRING_SESSION),
    API_ID,
    API_HASH,
)

# In-memory session is enough for the BotFather bot on Railway.
bot_client = TelegramClient(
    MemorySession(),
    API_ID,
    API_HASH,
)

# Prevent multiple batches from running in the same Telegram chat.
active_jobs: set[int] = set()


# =========================================================
# URL PARSING
# =========================================================

def clean_url(url: str) -> str:
    """Remove punctuation accidentally attached to a URL."""

    return url.strip().rstrip(".,;:!?)]}>\"'")


def normalize_url(url: str) -> str:
    """Normalize a URL for parsing and duplicate detection."""

    url = clean_url(url)

    url = re.sub(
        r"^http://",
        "https://",
        url,
        flags=re.IGNORECASE,
    )

    url = re.sub(
        r"^https://www\.",
        "https://",
        url,
        flags=re.IGNORECASE,
    )

    return url.rstrip("/")


def extract_urls(text: str) -> list[str]:
    """Extract all HTTP and HTTPS URLs from a message."""

    urls = re.findall(
        r"https?://[^\s]+",
        text,
        flags=re.IGNORECASE,
    )

    return [normalize_url(url) for url in urls]


def is_x_link(url: str) -> bool:
    """Check whether a URL is an X or Twitter profile link."""

    return bool(
        re.match(
            r"https://(?:x\.com|twitter\.com)/"
            r"[A-Za-z0-9_]+(?:/.*)?$",
            url,
            flags=re.IGNORECASE,
        )
    )


def is_telegram_link(url: str) -> bool:
    """Check whether a URL is a Telegram link."""

    return bool(
        re.match(
            r"https://t\.me/",
            url,
            flags=re.IGNORECASE,
        )
    )


def extract_x_username(url: str) -> Optional[str]:
    """Extract the username from an X or Twitter URL."""

    match = re.match(
        r"https://(?:x\.com|twitter\.com)/"
        r"([A-Za-z0-9_]+)",
        url,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    return match.group(1).lower()


def extract_public_tg_username(url: str) -> Optional[str]:
    """Extract a public Telegram username from a t.me URL."""

    match = re.match(
        r"https://t\.me/([A-Za-z0-9_]+)",
        url,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    username = match.group(1)

    reserved_paths = {
        "joinchat",
        "share",
        "addstickers",
        "proxy",
        "socks",
        "c",
        "iv",
    }

    if username.lower() in reserved_paths:
        return None

    return username


def is_private_invite_link(url: str) -> bool:
    """Identify private Telegram invitation links."""

    return bool(
        re.match(
            r"https://t\.me/(?:\+|joinchat/)",
            url,
            flags=re.IGNORECASE,
        )
    )


# =========================================================
# PROJECT PAIRING
# =========================================================

def pair_project_links(text: str) -> list[dict]:
    """
    Pair every X link with the Telegram link immediately following it.

    A new X link closes the previous project. This prevents a project
    without a Telegram link from taking the next project's TG link.
    """

    urls = extract_urls(text)

    projects: list[dict] = []
    current_project: Optional[dict] = None

    for url in urls:
        if is_x_link(url):
            if current_project is not None:
                projects.append(current_project)

            current_project = {
                "x_link": url,
                "tg_link": None,
            }

        elif is_telegram_link(url):
            if (
                current_project is not None
                and current_project["tg_link"] is None
            ):
                current_project["tg_link"] = url

    if current_project is not None:
        projects.append(current_project)

    return projects


# =========================================================
# DUPLICATE DETECTION
# =========================================================

def mark_duplicates(projects: list[dict]) -> list[dict]:
    """Mark repeated X accounts or Telegram groups."""

    seen_x: set[str] = set()
    seen_tg: set[str] = set()

    for project in projects:
        x_username = extract_x_username(project["x_link"])
        tg_link = project.get("tg_link")

        tg_identity = tg_link.lower() if tg_link else None

        reasons: list[str] = []

        if x_username and x_username in seen_x:
            reasons.append("duplicate X account")

        if tg_identity and tg_identity in seen_tg:
            reasons.append("duplicate Telegram group")

        project["is_duplicate"] = bool(reasons)
        project["duplicate_reason"] = ", ".join(reasons)

        if x_username:
            seen_x.add(x_username)

        if tg_identity:
            seen_tg.add(tg_identity)

    return projects


# =========================================================
# ACTIVITY RANKING
# =========================================================

def ensure_utc(value: datetime) -> datetime:
    """Convert a datetime into UTC."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def describe_activity(status) -> dict:
    """
    Convert Telegram's activity value into text and sorting values.

    Higher rank means more recent activity.
    """

    now = datetime.now(timezone.utc)

    if isinstance(status, UserStatusOnline):
        return {
            "text": "Online now",
            "rank": 6,
            "timestamp": now.timestamp(),
        }

    if isinstance(status, UserStatusOffline):
        last_seen = ensure_utc(status.was_online)
        age_seconds = (now - last_seen).total_seconds()

        if age_seconds <= 24 * 60 * 60:
            rank = 5
        elif age_seconds <= 7 * 24 * 60 * 60:
            rank = 4
        elif age_seconds <= 30 * 24 * 60 * 60:
            rank = 3
        else:
            rank = 2

        return {
            "text": (
                "Last seen: "
                f"{last_seen.strftime('%d %b %Y, %H:%M UTC')}"
            ),
            "rank": rank,
            "timestamp": last_seen.timestamp(),
        }

    if isinstance(status, UserStatusRecently):
        return {
            "text": "Recently active",
            "rank": 4,
            "timestamp": 0,
        }

    if isinstance(status, UserStatusLastWeek):
        return {
            "text": "Active within a week",
            "rank": 3,
            "timestamp": 0,
        }

    if isinstance(status, UserStatusLastMonth):
        return {
            "text": "Active within a month",
            "rank": 2,
            "timestamp": 0,
        }

    if isinstance(status, UserStatusEmpty):
        return {
            "text": "Activity hidden",
            "rank": 1,
            "timestamp": 0,
        }

    return {
        "text": "Activity unavailable",
        "rank": 0,
        "timestamp": 0,
    }


def admin_sort_key(admin: dict) -> tuple:
    """Sort administrators from most recently active to least active."""

    return (
        admin["activity_rank"],
        admin["activity_timestamp"],
    )


# =========================================================
# ADMIN RETRIEVAL
# =========================================================

async def get_priority_admins(group_link: str) -> dict:
    """
    Retrieve the owner and three most active human administrators.

    Telegram bot accounts and accounts without usernames are excluded.
    """

    entity = await user_client.get_entity(group_link)

    owner: Optional[dict] = None
    regular_admins: list[dict] = []

    async for user in user_client.iter_participants(
        entity,
        filter=ChannelParticipantsAdmins(),
    ):
        participant = getattr(user, "participant", None)

        is_owner = isinstance(
            participant,
            ChannelParticipantCreator,
        )

        is_admin = isinstance(
            participant,
            ChannelParticipantAdmin,
        )

        if not is_owner and not is_admin:
            continue

        # Remove Telegram bot accounts.
        if getattr(user, "bot", False):
            continue

        username = getattr(user, "username", None)

        # Skip accounts without public usernames.
        if not username:
            continue

        activity = describe_activity(
            getattr(user, "status", None)
        )

        custom_title = getattr(
            participant,
            "rank",
            None,
        )

        admin_data = {
            "username": f"@{username}",
            "role": "Owner" if is_owner else "Admin",
            "custom_title": custom_title,
            "activity": activity["text"],
            "activity_rank": activity["rank"],
            "activity_timestamp": activity["timestamp"],
        }

        if is_owner:
            owner = admin_data
        else:
            regular_admins.append(admin_data)

    regular_admins.sort(
        key=admin_sort_key,
        reverse=True,
    )

    return {
        "owner": owner,
        "active_admins": regular_admins[:MAX_ACTIVE_ADMINS],
    }


def format_person(person: dict) -> str:
    """Format an owner or administrator for the output."""

    custom_title = person.get("custom_title")

    if custom_title:
        return (
            f"{person['username']} "
            f"({person['role']}, {custom_title}, "
            f"{person['activity']})"
        )

    return (
        f"{person['username']} "
        f"({person['role']}, {person['activity']})"
    )


# =========================================================
# MESSAGE HELPERS
# =========================================================

async def safe_reply(event, text: str):
    """Reply without allowing a temporary Telegram error to stop the bot."""

    try:
        return await event.reply(
            text,
            link_preview=False,
        )

    except FloodWaitError as error:
        await asyncio.sleep(error.seconds + 1)

        return await event.reply(
            text,
            link_preview=False,
        )

    except RPCError as error:
        print(f"Reply error: {error}")
        return None


async def safe_edit(message, text: str):
    """Edit the live progress message safely."""

    if message is None:
        return

    try:
        await message.edit(
            text,
            link_preview=False,
        )

    except FloodWaitError as error:
        await asyncio.sleep(error.seconds + 1)

        try:
            await message.edit(
                text,
                link_preview=False,
            )

        except RPCError as retry_error:
            print(f"Progress edit retry failed: {retry_error}")

    except RPCError as error:
        if "message not modified" not in str(error).lower():
            print(f"Progress edit error: {error}")


def split_long_output(
    blocks: list[str],
    maximum: int = MESSAGE_CHUNK_SIZE,
) -> list[str]:
    """Split output without cutting through normal project blocks."""

    chunks: list[str] = []
    current_chunk = ""

    for block in blocks:
        proposed = (
            block
            if not current_chunk
            else f"{current_chunk}\n\n{block}"
        )

        if len(proposed) <= maximum:
            current_chunk = proposed
            continue

        if current_chunk:
            chunks.append(current_chunk)

        if len(block) <= maximum:
            current_chunk = block
        else:
            for start in range(0, len(block), maximum):
                chunks.append(
                    block[start:start + maximum]
                )

            current_chunk = ""

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


async def send_output_chunks(event, blocks: list[str]):
    """Send long results across multiple Telegram messages."""

    chunks = split_long_output(blocks)

    for index, chunk in enumerate(chunks, start=1):
        heading = ""

        if len(chunks) > 1:
            heading = f"Results {index}/{len(chunks)}\n\n"

        await safe_reply(
            event,
            heading + chunk,
        )

        await asyncio.sleep(0.5)


# =========================================================
# PROGRESS DISPLAY
# =========================================================

def build_progress_text(
    total: int,
    completed: int,
    successful: int,
    failed: int,
    private_links: int,
    missing_links: int,
    duplicates: int,
    current: str,
) -> str:
    """Build the live progress message."""

    percentage = (
        int((completed / total) * 100)
        if total
        else 0
    )

    return (
        "⏳ Processing project list\n\n"
        f"Progress: {completed}/{total} ({percentage}%)\n"
        f"Successful: {successful}\n"
        f"Failed or unavailable: {failed}\n"
        f"Private links: {private_links}\n"
        f"Missing TG links: {missing_links}\n"
        f"Duplicates skipped: {duplicates}\n\n"
        f"Current: {current}"
    )


# =========================================================
# PROJECT PROCESSING
# =========================================================

async def process_project_list(event, text: str):
    """Process a full project submission and return two result lists."""

    chat_id = event.chat_id

    if chat_id in active_jobs:
        await safe_reply(
            event,
            "⚠️ A list is already being processed in this chat. "
            "Wait for it to finish before submitting another.",
        )
        return

    active_jobs.add(chat_id)

    try:
        projects = mark_duplicates(pair_project_links(text))

        if not projects:
            await safe_reply(event, "❌ I could not find any valid X project links.")
            return

        total = len(projects)
        successful = 0
        failed = 0
        private_links = 0
        missing_links = 0
        duplicate_count = 0

        accessible_blocks: list[str] = []
        inaccessible_blocks: list[str] = []

        progress_message = await safe_reply(
            event,
            build_progress_text(
                total=total,
                completed=0,
                successful=0,
                failed=0,
                private_links=0,
                missing_links=0,
                duplicates=0,
                current="Preparing batch...",
            ),
        )

        for position, project in enumerate(projects, start=1):
            x_link = project["x_link"]
            tg_link = project.get("tg_link")
            current_status = "Checking..."

            if project["is_duplicate"]:
                duplicate_count += 1
                current_status = "Duplicate skipped"
                inaccessible_blocks.append(
                    f"{position}.\n"
                    f"X link: {x_link}\n"
                    f"TG link: {tg_link or 'Not provided'}\n"
                    f"Status: ⚠️ Duplicate skipped\n"
                    f"Reason: {project['duplicate_reason']}"
                )

            elif not tg_link:
                missing_links += 1
                current_status = "Missing Telegram link"
                inaccessible_blocks.append(
                    f"{position}.\n"
                    f"X link: {x_link}\n"
                    f"TG link: Not provided\n"
                    f"Status: ⚠️ Missing Telegram link"
                )

            elif is_private_invite_link(tg_link):
                private_links += 1
                current_status = "Private invite link"
                inaccessible_blocks.append(
                    f"{position}.\n"
                    f"X link: {x_link}\n"
                    f"TG link: {tg_link}\n"
                    f"Status: 🔒 Private invite link"
                )

            else:
                tg_username = extract_public_tg_username(tg_link)

                if not tg_username:
                    failed += 1
                    current_status = "Invalid Telegram link"
                    inaccessible_blocks.append(
                        f"{position}.\n"
                        f"X link: {x_link}\n"
                        f"TG link: {tg_link}\n"
                        f"Status: ❌ Invalid Telegram group link"
                    )
                else:
                    try:
                        result = await get_priority_admins(tg_link)
                        owner = result["owner"]
                        active_admins = result["active_admins"]

                        if owner or active_admins:
                            successful += 1
                            current_status = "Success"
                            owner_text = (
                                format_person(owner)
                                if owner
                                else "Owner not publicly visible or has no username"
                            )
                            active_admin_text = (
                                "\n".join(format_person(admin) for admin in active_admins)
                                if active_admins
                                else "No human administrators with public usernames found"
                            )
                            accessible_blocks.append(
                                f"{position}.\n"
                                f"X link: {x_link}\n"
                                f"TG link: {tg_link}\n"
                                f"Status: ✅ Success\n"
                                f"Owner:\n{owner_text}\n"
                                f"Top active admins:\n{active_admin_text}"
                            )
                        else:
                            failed += 1
                            current_status = "No public human admins"
                            inaccessible_blocks.append(
                                f"{position}.\n"
                                f"X link: {x_link}\n"
                                f"TG link: {tg_link}\n"
                                f"Status: ⚠️ No public human admins"
                            )

                    except FloodWaitError as error:
                        current_status = f"Waiting {error.seconds} seconds"
                        await safe_edit(
                            progress_message,
                            build_progress_text(
                                total=total,
                                completed=position - 1,
                                successful=successful,
                                failed=failed,
                                private_links=private_links,
                                missing_links=missing_links,
                                duplicates=duplicate_count,
                                current=current_status,
                            ),
                        )
                        await asyncio.sleep(error.seconds + 1)

                        try:
                            result = await get_priority_admins(tg_link)
                            owner = result["owner"]
                            active_admins = result["active_admins"]

                            if owner or active_admins:
                                successful += 1
                                current_status = "Success after waiting"
                                owner_text = (
                                    format_person(owner)
                                    if owner
                                    else "Owner not publicly visible or has no username"
                                )
                                active_admin_text = (
                                    "\n".join(format_person(admin) for admin in active_admins)
                                    if active_admins
                                    else "No human administrators with public usernames found"
                                )
                                accessible_blocks.append(
                                    f"{position}.\n"
                                    f"X link: {x_link}\n"
                                    f"TG link: {tg_link}\n"
                                    f"Status: ✅ Success\n"
                                    f"Owner:\n{owner_text}\n"
                                    f"Top active admins:\n{active_admin_text}"
                                )
                            else:
                                failed += 1
                                current_status = "No public human admins"
                                inaccessible_blocks.append(
                                    f"{position}.\n"
                                    f"X link: {x_link}\n"
                                    f"TG link: {tg_link}\n"
                                    f"Status: ⚠️ No public human admins"
                                )
                        except Exception as retry_error:
                            failed += 1
                            current_status = "Failed after waiting"
                            inaccessible_blocks.append(
                                f"{position}.\n"
                                f"X link: {x_link}\n"
                                f"TG link: {tg_link}\n"
                                f"Status: ❌ Failed after waiting\n"
                                f"Error: {retry_error}"
                            )

                    except (ChannelPrivateError, ChatAdminRequiredError):
                        failed += 1
                        current_status = "Group inaccessible"
                        inaccessible_blocks.append(
                            f"{position}.\n"
                            f"X link: {x_link}\n"
                            f"TG link: {tg_link}\n"
                            f"Status: 🔒 Group inaccessible\n"
                            f"Error: Your Telegram account cannot access the administrator list"
                        )

                    except (UsernameInvalidError, UsernameNotOccupiedError, ValueError):
                        failed += 1
                        current_status = "Group not found"
                        inaccessible_blocks.append(
                            f"{position}.\n"
                            f"X link: {x_link}\n"
                            f"TG link: {tg_link}\n"
                            f"Status: ❌ Group not found or invalid"
                        )

                    except (InviteHashInvalidError, InviteHashExpiredError):
                        private_links += 1
                        current_status = "Invalid private invitation"
                        inaccessible_blocks.append(
                            f"{position}.\n"
                            f"X link: {x_link}\n"
                            f"TG link: {tg_link}\n"
                            f"Status: 🔒 Invalid or expired private invitation"
                        )

                    except RPCError as error:
                        failed += 1
                        current_status = "Telegram error"
                        inaccessible_blocks.append(
                            f"{position}.\n"
                            f"X link: {x_link}\n"
                            f"TG link: {tg_link}\n"
                            f"Status: ❌ Telegram error\n"
                            f"Error: {error}"
                        )

                    except Exception as error:
                        failed += 1
                        current_status = "Unexpected failure"
                        inaccessible_blocks.append(
                            f"{position}.\n"
                            f"X link: {x_link}\n"
                            f"TG link: {tg_link}\n"
                            f"Status: ❌ Unexpected failure\n"
                            f"Error: {error}"
                        )

            if (
                position == 1
                or position == total
                or position % PROGRESS_UPDATE_INTERVAL == 0
            ):
                await safe_edit(
                    progress_message,
                    build_progress_text(
                        total=total,
                        completed=position,
                        successful=successful,
                        failed=failed,
                        private_links=private_links,
                        missing_links=missing_links,
                        duplicates=duplicate_count,
                        current=f"{position}. {x_link}\nResult: {current_status}",
                    ),
                )

            await asyncio.sleep(DELAY_BETWEEN_PROJECTS)

        await safe_edit(
            progress_message,
            "✅ Batch completed\n\n"
            f"Projects submitted: {total}\n"
            f"Accessible: {len(accessible_blocks)}\n"
            f"Inaccessible or unavailable: {len(inaccessible_blocks)}\n"
            f"Private links: {private_links}\n"
            f"Missing TG links: {missing_links}\n"
            f"Duplicates skipped: {duplicate_count}",
        )

        if accessible_blocks:
            accessible_chunks = split_long_output(accessible_blocks)
            for index, chunk in enumerate(accessible_chunks, start=1):
                part = (
                    f"Part {index}/{len(accessible_chunks)}\n\n"
                    if len(accessible_chunks) > 1
                    else ""
                )
                await safe_reply(
                    event,
                    f"✅ ACCESSIBLE PROJECTS\n"
                    f"Total: {len(accessible_blocks)}\n\n"
                    f"{part}{chunk}",
                )
                await asyncio.sleep(0.5)
        else:
            await safe_reply(event, "✅ ACCESSIBLE PROJECTS\nTotal: 0")

        if inaccessible_blocks:
            inaccessible_chunks = split_long_output(inaccessible_blocks)
            for index, chunk in enumerate(inaccessible_chunks, start=1):
                part = (
                    f"Part {index}/{len(inaccessible_chunks)}\n\n"
                    if len(inaccessible_chunks) > 1
                    else ""
                )
                await safe_reply(
                    event,
                    f"❌ INACCESSIBLE / UNAVAILABLE PROJECTS\n"
                    f"Total: {len(inaccessible_blocks)}\n\n"
                    f"{part}{chunk}",
                )
                await asyncio.sleep(0.5)
        else:
            await safe_reply(event, "❌ INACCESSIBLE / UNAVAILABLE PROJECTS\nTotal: 0")

    finally:
        active_jobs.discard(chat_id)


# =========================================================
# BOT COMMANDS
# =========================================================

@bot_client.on(
    events.NewMessage(pattern=r"^/start(?:@\w+)?$")
)
async def start_handler(event):
    await event.reply(
        "Send an X link followed by its Telegram group link.\n\n"
        "Example:\n\n"
        "https://x.com/project\n"
        "https://t.me/projectchat\n\n"
        "You may send several projects in one message.\n\n"
        "I will return the owner and the three most recently "
        "active human admins.",
        link_preview=False,
    )


@bot_client.on(
    events.NewMessage(pattern=r"^/help(?:@\w+)?$")
)
async def help_handler(event):
    await event.reply(
        "Use this format:\n\n"
        "https://x.com/project1\n"
        "https://t.me/project1chat\n\n"
        "https://x.com/project2\n"
        "https://t.me/project2chat\n\n"
        "Bot accounts are excluded automatically.",
        link_preview=False,
    )


@bot_client.on(events.NewMessage)
async def project_list_handler(event):
    """Receive project lists sent to the bot."""

    text = event.raw_text.strip() if event.raw_text else ""

    if not text or text.startswith("/"):
        return

    has_x_link = re.search(
        r"https?://(?:www\.)?(?:x\.com|twitter\.com)/",
        text,
        flags=re.IGNORECASE,
    )

    if not has_x_link:
        await event.reply(
            "❌ I could not find an X project link.\n\n"
            "Send an X link followed by its Telegram link.",
            link_preview=False,
        )
        return

    await process_project_list(
        event,
        text,
    )


# =========================================================
# STARTUP AND CLEAN SHUTDOWN
# =========================================================

async def disconnect_clients():
    """Disconnect both Telethon clients safely."""

    print("\nDisconnecting Telegram clients...")

    for name, client in (
        ("Bot", bot_client),
        ("Personal account", user_client),
    ):
        try:
            if client.is_connected():
                await client.disconnect()

            print(f"{name} disconnected.")

        except Exception as error:
            print(f"Could not disconnect {name}: {error}")


async def main():
    """Start the personal account and Telegram bot."""

    try:
        print("Starting personal Telethon session...")

        await user_client.start()

        personal_account = await user_client.get_me()

        personal_name = (
            f"@{personal_account.username}"
            if personal_account.username
            else personal_account.first_name
        )

        print(
            f"Personal account connected: {personal_name}"
        )

        print("Starting Telegram bot...")

        await bot_client.start(
            bot_token=BOT_TOKEN,
        )

        bot_account = await bot_client.get_me()

        print(
            f"Bot connected: @{bot_account.username}"
        )

        print("=" * 60)
        print("PROJECT ADMIN RETRIEVER IS RUNNING")
        print("Send project lists to the Telegram bot.")
        print("Press Ctrl+C to stop.")
        print("=" * 60)

        await bot_client.run_until_disconnected()

    except asyncio.CancelledError:
        print("\nShutdown requested.")

    finally:
        await disconnect_clients()


def run():
    """Run the program and handle Ctrl+C."""

    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        print("\nBot stopped by the user.")

    except Exception as error:
        print(f"\nThe bot stopped because of an error: {error}")

    finally:
        print("Program closed.")


if __name__ == "__main__":
    run()
