import os
import asyncio
import random
import string
import time
from datetime import datetime
from pyrogram import Client, filters, raw, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from motor.motor_asyncio import AsyncIOMotorClient
from pyrogram.errors import FloodWait
from keep_alive import keep_alive

# --- Configuration & Credentials ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "0"))
MONGO_URI = os.environ.get("MONGO_URI", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

# --- Initialization ---
app = Client("permanent_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Async MongoDB Connection
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.bot_database
links_collection = db.stored_links
active_deliveries = db.active_deliveries
media_collection = db.media # 🚨 THE IMMORTAL FILE ID DATABASE
admins_collection = db.admins
db_channels_collection = db.db_channels

# State and Process managers
user_states = {}
active_processes = {}

ADMINS_CACHE = set()
DB_CHANNELS_CACHE = set()

def is_authorized(user_id):
    return user_id == OWNER_ID or user_id in ADMINS_CACHE

def get_db_channel():
    if DB_CHANNELS_CACHE:
        return random.choice(list(DB_CHANNELS_CACHE))
    return DB_CHANNEL_ID

# ==========================================
# 🚨 PEER CACHE & ADMIN HELPERS 🚨
# ==========================================
async def ensure_peer_loaded(client, chat_id, retries=3, delay=1.0):
    for attempt in range(1, retries + 1):
        try:
            info = await client.get_chat(chat_id)
            print(f"[warmup] chat loaded: id={getattr(info, 'id', None)} title={getattr(info, 'title', None)}")
            return True
        except Exception as e:
            print(f"[warmup] attempt {attempt} failed for {chat_id}: {e}")
            try:
                # Exhaustively search dialogs for the missing chat to force peer caching!
                found = False
                async for dialog in client.get_dialogs():
                    if dialog.chat and dialog.chat.id == chat_id:
                        found = True
                        break
                if found:
                    await client.get_chat(chat_id)
                    return True
            except Exception: pass
            
            if attempt < retries:
                await asyncio.sleep(delay)
    return False

async def is_bot_admin(client, chat_id):
    me = await client.get_me()
    try:
        member = await client.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "")
        is_admin = status in ("administrator", "creator")
        return is_admin
    except Exception as e:
        return False

def generate_hash(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def get_progress_string(current, total, start_time):
    percent = (current / total) * 100
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    remaining = total - current
    eta = remaining / speed if speed > 0 else 0
    
    m, s = divmod(int(eta), 60)
    h, m = divmod(m, 60)
    eta_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    
    filled = int(percent / 10)
    bar = "█" * filled + "░" * (10 - filled)
    
    return (
        f"[{bar}] {percent:.1f}%\n\n"
        f"📦 **Total Files:** {total}\n"
        f"✅ **Processed:** {current}\n"
        f"🚀 **Speed:** {speed:.1f} files/sec\n"
        f"⏳ **ETA:** {eta_str}"
    )

# ==========================================
# 🚨 ADMIN & CONFIG COMMANDS 🚨
# ==========================================
@app.on_message(filters.command("addadmin") & filters.private)
async def add_admin_cmd(client, message):
    if message.from_user.id != OWNER_ID: return await message.reply("⛔ Owner only.")
    try:
        user_id = int(message.command[1])
        await admins_collection.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)
        ADMINS_CACHE.add(user_id)
        await message.reply(f"✅ User {user_id} added as admin.")
    except (IndexError, ValueError):
        await message.reply("Usage: /addadmin <user_id>")

@app.on_message(filters.command("deladmin") & filters.private)
async def del_admin_cmd(client, message):
    if message.from_user.id != OWNER_ID: return await message.reply("⛔ Owner only.")
    try:
        user_id = int(message.command[1])
        await admins_collection.delete_one({"user_id": user_id})
        ADMINS_CACHE.discard(user_id)
        await message.reply(f"✅ User {user_id} removed from admins.")
    except (IndexError, ValueError):
        await message.reply("Usage: /deladmin <user_id>")

@app.on_message(filters.command("adddb") & filters.private)
async def add_db_cmd(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    try:
        channel_id = int(message.command[1])
        await db_channels_collection.update_one({"channel_id": channel_id}, {"$set": {"channel_id": channel_id}}, upsert=True)
        DB_CHANNELS_CACHE.add(channel_id)
        await message.reply(f"✅ Channel {channel_id} added to DB channels.")
    except (IndexError, ValueError):
        await message.reply("Usage: /adddb <channel_id>")

@app.on_message(filters.command("deldb") & filters.private)
async def del_db_cmd(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    try:
        channel_id = int(message.command[1])
        await db_channels_collection.delete_one({"channel_id": channel_id})
        DB_CHANNELS_CACHE.discard(channel_id)
        await message.reply(f"✅ Channel {channel_id} removed from DB channels.")
    except (IndexError, ValueError):
        await message.reply("Usage: /deldb <channel_id>")

@app.on_message(filters.command("renamechannel") & filters.private)
async def rename_channel_cmd(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    parts = message.text.split(" ", 2)
    if len(parts) < 3:
        return await message.reply("Usage: `/renamechannel <channel_id> <New Name>`\nExample: `/renamechannel -10012345 My Movies`")
    
    channel_id = parts[1]
    new_title = parts[2]
    
    status = await message.reply("⏳ **Updating channel name...**")
    result = await media_collection.update_many(
        {"source_channel": channel_id},
        {"$set": {"source_title": new_title}}
    )
    
    if result.matched_count == 0:
        await status.edit_text("❌ **No media found for this channel ID.**")
    else:
        await status.edit_text(f"✅ **Success!**\nUpdated the name to **{new_title}** for {result.modified_count} files.")

@app.on_message(filters.command("help") & filters.private)
async def help_cmd(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    text = (
        "🛠 **Bot Commands**\n"
        "━━━━━━━━━━━━━━━━━━━━━\n\n"

        "📦 **Batch & Storage**\n"
        "• `/batch` — Clone a channel's media into the DB.\n"
        "• `/url` — List all stored channels with permanent links.\n"
        "• `/getchannel <channel_id>` — Package a specific channel into a link.\n"
        "• `/dbupload` — Package the **entire** MongoDB into one master link.\n\n"

        "🏷️ **Channel Management**\n"
        "• `/renamechannel <channel_id> <name>` — Set a display name for a stored channel.\n"
        "• `/reassign <old_source> | <channel_id> [title]` — Move orphaned files to a real channel.\n\n"

        "🧹 **Database Maintenance**\n"
        "• `/dedupe` — Remove duplicate files from database.\n"
        "• `/makeimmortal` — Convert ALL old links to immortal format & label orphaned files as **Free Files**.\n\n"

        "⚙️ **DB Channel Config**\n"
        "• `/adddb <channel_id>` — Add a dynamic DB storage channel.\n"
        "• `/deldb <channel_id>` — Remove a DB storage channel.\n\n"

        "👑 **Owner Only**\n"
        "• `/addadmin <user_id>` — Add an admin.\n"
        "• `/deladmin <user_id>` — Remove an admin.\n\n"

        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 **Tip:** Run `/makeimmortal` once to make all your existing data\n"
        "survive DB channel or source channel deletion permanently."
    )
    await message.reply(text)

@app.on_message(filters.command("getchannel") & filters.private)
async def get_channel_cmd(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    try:
        channel_id = message.command[1]
    except IndexError:
        return await message.reply("Usage: `/getchannel <channel_id>`")
        
    status = await message.reply(f"⏳ **Searching for media from {channel_id}...**")
    count = await media_collection.count_documents({"source_channel": channel_id})
    
    if count == 0:
        return await status.edit_text("❌ **No media found for this channel.**")
        
    url_hash = generate_hash()
    
    await links_collection.insert_one({
        "hash": url_hash,
        "source_channel_query": channel_id,
        "creator_id": message.from_user.id
    })

    bot_username = (await client.get_me()).username
    shareable_link = f"https://t.me/{bot_username}?start={url_hash}"

    await status.edit_text(
        f"✅ **Channel Content Packaged!**\n\n"
        f"📦 **Total Files:** {count}\n"
        f"🔗 **Your Link:**\n`{shareable_link}`"
    )

@app.on_message(filters.command(["url", "urls"]) & filters.private)
async def url_list_cmd(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    
    status = await message.reply("⏳ **Generating channel links...**")
    
    pipeline = [
        {"$group": {
            "_id": "$source_channel",
            "title": {"$first": "$source_title"},
            "count": {"$sum": 1}
        }}
    ]
    cursor = media_collection.aggregate(pipeline)
    channels = await cursor.to_list(length=None)
    
    if not channels:
        return await status.edit_text("❌ **No stored channels found.**")
        
    text = "📚 **Stored Channels:**\n\n"
    bot_username = (await client.get_me()).username
    
    for ch in channels:
        ch_id = ch["_id"]
        title = ch.get("title") or ch_id
        count = ch["count"]
        
        url_hash = generate_hash()
        await links_collection.insert_one({
            "hash": url_hash,
            "source_channel_query": ch_id,
            "creator_id": message.from_user.id
        })
        
        shareable_link = f"https://t.me/{bot_username}?start={url_hash}"
        text += f"**{title}** (`{ch_id}`) - {count} files\n🔗 `{shareable_link}`\n\n"
        
    if len(text) > 4000:
        await status.delete()
        for i in range(0, len(text), 4000):
            await message.reply(text[i:i+4000])
    else:
        await status.edit_text(text)

# ==========================================
# 🚨 IMMORTAL MEDIA EXTRACTION ENGINE 🚨
# ==========================================
async def extract_and_save_media(msg, source_name="DB Channel", source_title=None, overwrite_source=True):
    """Extracts the immortal file_id from a message and saves it to MongoDB.
    
    Uses file_unique_id as _id so the same physical file is NEVER duplicated,
    even if re-batched or re-forwarded (which generates new file_ids).
    """
    if not source_title:
        source_title = str(source_name)
        
    if not msg or not msg.media: return False
    mt = msg.media.value
    if mt in ["document", "video", "audio", "photo", "animation"]:
        media = getattr(msg, mt)
        fid = media.file_id
        uid = media.file_unique_id
        fname = getattr(media, "file_name", f"{mt}_{uid}")
        try:
            if overwrite_source:
                # Full upsert: insert new or update all fields including source
                await media_collection.update_one(
                    {"_id": uid},                          # 🔥 file_unique_id as _id (content-stable)
                    {"$set": {
                        "file_id": fid,                    # file_id stored as regular field
                        "file_unique_id": uid,             # 🔥 FIX: also store as field for cross-format dup queries
                        "type": mt,
                        "file_name": fname,
                        "source_channel": str(source_name),
                        "source_title": source_title,
                        "added_date": datetime.now().isoformat()
                    }},
                    upsert=True
                )
            else:
                # Only insert if new; never overwrite source info of existing records
                await media_collection.update_one(
                    {"_id": uid},
                    {
                        "$setOnInsert": {              # Only sets these fields on INSERT, not update
                            "file_id": fid,
                            "file_unique_id": uid,     # 🔥 FIX: store as field for cross-format dup queries
                            "type": mt,
                            "file_name": fname,
                            "source_channel": str(source_name),
                            "source_title": source_title,
                            "added_date": datetime.now().isoformat()
                        },
                        "$set": {"file_id": fid}       # Always refresh file_id (Telegram may rotate it)
                    },
                    upsert=True
                )
            return True
        except Exception as e:
            print(f"[DB Error] Could not save media {uid}: {e}")
    return False

@app.on_message(filters.channel)
async def auto_index_channel_media(client, message):
    """Passively listens to DB Channels and saves manually dropped files.
    
    Uses overwrite_source=False so batch operations that already indexed
    a file with the correct source channel are NOT overwritten with 'Manual Drop'.
    """
    if message.chat.id in DB_CHANNELS_CACHE or message.chat.id == DB_CHANNEL_ID:
        await extract_and_save_media(message, source_name="Manual Drop", overwrite_source=False)

# ==========================================
# 🚨 DATABASE PACKAGING COMMANDS 🚨
# ==========================================
@app.on_message(filters.command("dbupload") & filters.private)
async def db_upload_command(client, message):
    """Packages every single immortal file_id in the MongoDB into a single delivery link."""
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    status = await message.reply("⏳ **Packaging all media from MongoDB...**")
    
    cursor = media_collection.find({})
    all_media = await cursor.to_list(length=None)
    
    if not all_media:
        return await status.edit_text("❌ **MongoDB is empty.**\nRun a /batch or send files to the DB Channel first.")
    
    total_files = len(all_media)
    url_hash = generate_hash()
    
    # Build payload: for NEW records use doc["file_id"]; for OLD records _id IS the file_id
    payload = [
        {"file_id": doc.get("file_id") or doc["_id"], "type": doc["type"]}
        for doc in all_media
        if doc.get("file_id") or doc.get("_id")
    ]
    
    await links_collection.insert_one({
        "hash": url_hash,
        "immortal_files": payload,
        "creator_id": message.from_user.id
    })

    bot_username = (await client.get_me()).username
    shareable_link = f"https://t.me/{bot_username}?start={url_hash}"

    await status.edit_text(
        f"✅ **Database Packaged Successfully!**\n\n"
        f"📦 **Total Immortal Files:** {total_files}\n\n"
        f"🔗 **Your Master Link:**\n`{shareable_link}`\n\n"
        f"*(Tap the link to deliver all these files to your DM or Channel)*"
    )

@app.on_message(filters.command("dedupe") & filters.private)
async def dedupe_cmd(client, message):
    """Scans MongoDB for duplicate file_unique_ids and removes extras, keeping the best source info."""
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    status = await message.reply("🔍 **Scanning for duplicates in database...**\n⏳ This may take a while for large collections.")

    # Group by file_unique_id field.
    # Old records: _id=file_id, file_unique_id stored as a field.
    # New records: _id=file_unique_id, file_unique_id ALSO stored as a field (after fix).
    # Records with NO file_unique_id field at all are grouped under _id:null — we SKIP those
    # to avoid accidentally deleting orphaned records that can't be compared.
    pipeline = [
        {"$match": {"file_unique_id": {"$exists": True, "$ne": None}}},  # skip null-uid records
        {"$group": {
            "_id": "$file_unique_id",
            "all_doc_ids": {"$push": "$_id"},
            "sources": {"$push": {"$ifNull": ["$source_channel", ""]}},
            "count": {"$sum": 1}
        }},
        {"$match": {"count": {"$gt": 1}}}
    ]
    cursor = media_collection.aggregate(pipeline)
    dup_groups = await cursor.to_list(length=None)

    # Also count records with no file_unique_id for reporting
    orphan_count = await media_collection.count_documents(
        {"$or": [{"file_unique_id": {"$exists": False}}, {"file_unique_id": None}]}
    )

    if not dup_groups:
        total = await media_collection.count_documents({})
        orphan_note = f"\n⚠️ **{orphan_count} orphaned records** (no file_unique_id — safe, not touched)" if orphan_count else ""
        return await status.edit_text(
            f"✅ **No duplicates found!**\n"
            f"📦 Database has **{total}** unique files — all clean.{orphan_note}"
        )

    total_dups = sum(g["count"] - 1 for g in dup_groups)
    orphan_note = f" | ⚠️ {orphan_count} orphaned (skipped)" if orphan_count else ""
    await status.edit_text(
        f"🗑️ **Found {len(dup_groups)} duplicate groups** ({total_dups} extra entries{orphan_note})\n"
        f"⏳ Removing extras, keeping best source info..."
    )

    removed = 0
    LOW_PRIORITY_SOURCES = {"Manual Drop", "Batch Raw Fallback", "Direct PM", "DB Channel", "None", "", "none"}

    for group in dup_groups:
        all_ids = group["all_doc_ids"]
        sources = group["sources"]
        # Pick the doc with the best (real channel ID) source to KEEP
        # Default: keep index 0 (safe even if all sources are low-priority)
        best_idx = 0
        for idx, src in enumerate(sources):
            if str(src or "").strip() not in LOW_PRIORITY_SOURCES:
                best_idx = idx
                break
        keep_id = all_ids[best_idx]
        delete_ids = [d for d in all_ids if d != keep_id]
        if delete_ids:
            await media_collection.delete_many({"_id": {"$in": delete_ids}})
            removed += len(delete_ids)

    total_after = await media_collection.count_documents({})
    await status.edit_text(
        f"✅ **Deduplication Complete!**\n\n"
        f"🗑️ **Removed:** {removed} duplicate entries\n"
        f"📦 **Remaining:** {total_after} unique files\n\n"
        f"💡 Run `/url` to see your updated channel counts."
    )

@app.on_message(filters.command("reassign") & filters.private)
async def reassign_cmd(client, message):
    """Moves orphaned files (e.g. 'Uncategorized' or 'Batch Raw Fallback') to a real channel ID.
    Usage: /reassign <old_source> | <channel_id> [optional title]
    """
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    
    raw_text = message.text.split(None, 1)
    if len(raw_text) < 2 or "|" not in raw_text[1]:
        return await message.reply(
            "❌ **Usage:** `/reassign <old_source> | <channel_id> [title]`\n\n"
            "**Examples:**\n"
            "`/reassign Uncategorized | -1003868096217 Vip Channel`\n"
            "`/reassign Batch Raw Fallback | -1003868096217`\n\n"
            "💡 Find old source names from `/url` output."
        )
    
    parts = raw_text[1].split("|", 1)
    old_source = parts[0].strip()
    right_parts = parts[1].strip().split(None, 1)
    
    if not right_parts:
        return await message.reply("❌ Missing channel_id after `|`")
    
    new_channel_id = right_parts[0].strip()
    new_title = right_parts[1].strip() if len(right_parts) > 1 else new_channel_id
    
    try:
        int(new_channel_id)
    except ValueError:
        return await message.reply(f"❌ `{new_channel_id}` is not a valid channel ID. Must be numeric like `-1003868096217`.")
    
    status = await message.reply(
        f"⏳ **Reassigning files...**\n"
        f"From: `{old_source}`\n"
        f"To: `{new_channel_id}` ({new_title})"
    )
    
    count = await media_collection.count_documents({"source_channel": old_source})
    if count == 0:
        return await status.edit_text(
            f"❌ **No files found** with source `{old_source}`.\n"
            f"Check `/url` for exact source names."
        )
    
    result = await media_collection.update_many(
        {"source_channel": old_source},
        {"$set": {
            "source_channel": new_channel_id,
            "source_title": new_title
        }}
    )
    
    await status.edit_text(
        f"✅ **Reassignment Complete!**\n\n"
        f"📂 **Old source:** `{old_source}`\n"
        f"📡 **New channel:** `{new_channel_id}` ({new_title})\n"
        f"📦 **Files moved:** {result.modified_count}\n\n"
        f"💡 Run `/url` to see the updated channel in your list."
    )


@app.on_message(filters.command("makeimmortal") & filters.private)
async def make_immortal_cmd(client, message):
    """Converts ALL existing stored_links that use db_message_ids into immortal links.
    After this runs, every link survives DB channel deletion forever.
    Also labels orphaned 'Batch Raw Fallback' files as 'Free Files'.
    """
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")

    status = await message.reply(
        "⚡ **Immortalization Started...**\n\n"
        "**Step 1/2:** Labelling orphaned files as *Free Files*...\n"
        "**Step 2/2:** Converting old links to immortal format...\n\n"
        "⏳ Please wait."
    )

    # ── STEP 1: Label all orphaned generic-source files as "Free Files" ──
    orphan_sources = ["Batch Raw Fallback", "Manual Drop", "DB Channel"]
    free_count = 0
    for src in orphan_sources:
        res = await media_collection.update_many(
            {"source_channel": src},
            {"$set": {"source_channel": "free_files", "source_title": "Free Files"}}
        )
        free_count += res.modified_count

    step1_note = f"✅ Labelled **{free_count}** orphaned files as **Free Files**.\n" if free_count else "✅ No orphaned files to relabel.\n"

    # ── STEP 2: Convert old stored_links to immortal format ──
    cursor = links_collection.find({
        "db_message_ids": {"$exists": True},
        "$or": [
            {"immortal_files": {"$exists": False}},
            {"immortal_files": {"$size": 0}}
        ]
    })
    links_to_convert = await cursor.to_list(length=None)

    if not links_to_convert:
        return await status.edit_text(
            f"{step1_note}\n"
            "✅ **All links are already immortal!**\n\n"
            "💡 Run `/url` to see your updated channel list."
        )

    total_links = len(links_to_convert)
    converted = 0
    failed = 0
    total_files_immortalized = 0

    await status.edit_text(
        f"{step1_note}\n"
        f"⚡ **Found {total_links} old links to convert...**\n"
        "⏳ Fetching file IDs from DB channel..."
    )

    for idx, link_doc in enumerate(links_to_convert):
        msg_ids = link_doc.get("db_message_ids", [])
        db_channel = link_doc.get("db_channel_id", DB_CHANNEL_ID)

        if not msg_ids or not db_channel:
            failed += 1
            continue

        try:
            await ensure_peer_loaded(client, db_channel)
            immortal_files = []

            for i in range(0, len(msg_ids), 100):
                chunk = msg_ids[i:i + 100]
                try:
                    msgs = await client.get_messages(db_channel, chunk)
                    for msg in msgs:
                        if not msg or msg.empty or not msg.media:
                            continue
                        mt = msg.media.value
                        if mt in ["document", "video", "audio", "photo", "animation"]:
                            media_obj = getattr(msg, mt)
                            immortal_files.append({"file_id": media_obj.file_id, "type": mt})
                    await asyncio.sleep(0.5)
                except FloodWait as fw:
                    await asyncio.sleep(fw.value + 1)
                except Exception as ce:
                    print(f"Chunk error for link {link_doc.get('hash','?')}: {ce}")

            if immortal_files:
                await links_collection.update_one(
                    {"_id": link_doc["_id"]},
                    {"$set": {"immortal_files": immortal_files}}
                )
                converted += 1
                total_files_immortalized += len(immortal_files)
            else:
                failed += 1  # Messages already deleted from DB channel

        except Exception as e:
            print(f"Link conversion error: {e}")
            failed += 1

        if (idx + 1) % 3 == 0 or (idx + 1) == total_links:
            try:
                await status.edit_text(
                    f"{step1_note}\n"
                    f"⚡ **Converting links... {idx+1}/{total_links}**\n\n"
                    f"✅ Converted: {converted}\n"
                    f"❌ Failed (msgs deleted): {failed}\n"
                    f"📦 Files immortalized: {total_files_immortalized}"
                )
            except Exception:
                pass

    await status.edit_text(
        f"✅ **Immortalization Complete!**\n\n"
        f"🏷️ **Orphaned files labelled:** {free_count} → **Free Files**\n"
        f"🔗 **Links converted:** {converted}/{total_links}\n"
        f"📦 **Total files immortalized:** {total_files_immortalized}\n"
        f"❌ **Failed** (DB msgs already gone): {failed}\n\n"
        f"💡 Run `/url` to see your updated channels.\n"
        f"⚠️ Failed links had their DB channel messages already deleted —\n"
        f"use `/dbupload` to re-package from MongoDB instead."
    )


# ==========================================
# 1. MANUAL BATCH & SINGLE UPLOAD WORKFLOW
# ==========================================


@app.on_message(filters.command("batch") & filters.private)
async def start_batch(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    user_states[message.from_user.id] = {"state": "waiting_first_msg"}
    await message.reply("Send or forward the **FIRST** message from your channel.")

@app.on_message(filters.private & ~filters.command([
    "start", "batch", "dbupload", "help",
    "addadmin", "deladmin",
    "adddb", "deldb",
    "getchannel", "url", "urls",
    "renamechannel", "dedupe", "reassign", "makeimmortal"
]))
async def handle_batch_messages(client, message):
    user_id = message.from_user.id
    if not is_authorized(user_id): return
    state_data = user_states.get(user_id)

    # --- PM SINGLE UPLOAD INTERCEPTOR ---
    if not state_data:
        if message.media and message.media.value in ["document", "video", "audio", "photo", "animation"]:
            status = await message.reply("⏳ Extracting Immortal ID...")
            try:
                db_channel = get_db_channel()
                if db_channel == 0: return await status.edit_text("❌ No DB Channel Configured.")
                copied_msg = await message.copy(db_channel)
                await extract_and_save_media(copied_msg, source_name="Direct PM")
                
                media = getattr(message, message.media.value)
                url_hash = generate_hash()
                
                # Single links act exactly like /dbupload links, just with 1 item.
                await links_collection.insert_one({
                    "hash": url_hash,
                    "immortal_files": [{"file_id": media.file_id, "type": message.media.value}]
                })
                
                bot_username = (await client.get_me()).username
                shareable_link = f"https://t.me/{bot_username}?start={url_hash}"
                await status.edit_text(
                    f"✅ **Single Immortal File Saved!**\n\n"
                    f"🔗 **Your Link:**\n`{shareable_link}`"
                )
            except Exception as e:
                await status.edit_text(f"❌ Error saving media: {e}")
        return 

    # --- MANUAL BATCH PROCESSING ---
    if state_data["state"] == "waiting_first_msg":
        if not message.forward_from_chat:
            return await message.reply("Please *forward* the message directly from the channel.")
        
        user_states[user_id] = {
            "state": "waiting_last_msg",
            "source_chat_id": message.forward_from_chat.id,
            "source_chat_title": message.forward_from_chat.title if message.forward_from_chat else "Unknown",
            "first_msg_id": message.forward_from_message_id
        }
        await message.reply("First message saved. Now forward the **LAST** message.")

    elif state_data["state"] == "waiting_last_msg":
        if not message.forward_from_chat:
            return await message.reply("Please *forward* the message directly from the channel.")
        
        first_msg_id = state_data["first_msg_id"]
        last_msg_id = message.forward_from_message_id
        source_chat_id = state_data["source_chat_id"]
        source_chat_title = state_data.get("source_chat_title", "Unknown")
        
        del user_states[user_id]
        
        process_id = f"batch_{user_id}"
        active_processes[process_id] = False
        
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Batch", callback_data=f"cancel_batch_{user_id}")]])
        status_msg = await message.reply("⏳ **Initializing batch process...**", reply_markup=cancel_kb)
        
        db_message_ids = []
        immortal_files = []  # 🔥 FIX: Also store file_ids so links survive DB channel edits/deletions
        message_ids_to_process = list(range(first_msg_id, last_msg_id + 1))
        total_msgs = len(message_ids_to_process)   # total message ID range (includes non-media)
        media_count = 0                             # only actual media processed
        skipped_count = 0                           # duplicates skipped
        chunk_size = 100 
        
        start_time = time.time()
        processed_count = 0   # tracks position in message_ids_to_process for ETA display
        is_cancelled = False

        for i in range(0, total_msgs, chunk_size):
            if active_processes.get(process_id):
                is_cancelled = True
                break

            chunk = message_ids_to_process[i:i + chunk_size]
            original_chunk_len = len(chunk)

            # --- DUPLICATE FILTERING (done BEFORE entering while True) ---
            # BUG FIX: Was inside while True → `continue` would re-enter while True forever
            # on all-duplicate chunks instead of advancing to the next outer for-loop chunk.
            all_duplicates = False
            try:
                fetched_msgs = await client.get_messages(source_chat_id, chunk)
                valid_ids = []
                for msg in fetched_msgs:
                    if not msg or msg.empty or not msg.media:
                        continue
                    mt = msg.media.value
                    if mt in ["document", "video", "audio", "photo", "animation"]:
                        media_obj = getattr(msg, mt)
                        if hasattr(media_obj, "file_unique_id"):
                            uid = media_obj.file_unique_id
                            # 🔥 FIX: Query covers BOTH formats:
                            # - Old records: _id=file_id, file_unique_id stored as a field
                            # - New records: _id=file_unique_id, file_unique_id also stored as a field
                            # Without $or, new-format records were invisible to this check
                            # and the same file would be re-batched and re-inflated.
                            exists = await media_collection.find_one({
                                "$or": [
                                    {"file_unique_id": uid},  # catches old AND new format records
                                    {"_id": uid}              # catches new format if field was missing
                                ]
                            })
                            if exists:
                                skipped_count += 1
                                continue
                    valid_ids.append(msg.id)

                if not valid_ids:
                    # Entire chunk is duplicates — advance and skip
                    all_duplicates = True
                    processed_count += original_chunk_len
                    prog_str = get_progress_string(processed_count, total_msgs, start_time)
                    try:
                        await status_msg.edit_text(
                            f"🔄 **Skipped {skipped_count} duplicates so far...**\n\n{prog_str}",
                            reply_markup=cancel_kb
                        )
                    except Exception:
                        pass
                else:
                    chunk = valid_ids  # Only forward the non-duplicate IDs
            except Exception as filter_err:
                print(f"Duplicate check error: {filter_err}")
            # --- END DUPLICATE FILTERING ---

            if all_duplicates:
                continue  # ✅ Now this correctly continues the outer FOR loop

            while True:
                try:
                    db_channel = get_db_channel()
                    if db_channel == 0:
                        await status_msg.edit_text("❌ No DB Channel Configured.")
                        break

                    ok1 = await ensure_peer_loaded(client, source_chat_id)
                    ok2 = await ensure_peer_loaded(client, db_channel)
                    if not ok1 or not ok2:
                        await status_msg.edit_text("❌ Unable to load channel peers.")
                        break

                    try:
                        forwarded_msgs = await client.forward_messages(
                            chat_id=db_channel,
                            from_chat_id=source_chat_id,
                            message_ids=chunk,
                            disable_notification=True,
                            drop_author=True
                        )
                        for msg in forwarded_msgs:
                            if msg and msg.id:
                                db_message_ids.append(msg.id)
                                # 🔥 FIX: Save immortal file_id too so link is not tied to message IDs
                                saved = await extract_and_save_media(msg, source_name=str(source_chat_id), source_title=source_chat_title)
                                if saved and msg.media:
                                    mt = msg.media.value
                                    if mt in ["document", "video", "audio", "photo", "animation"]:
                                        media_obj = getattr(msg, mt)
                                        immortal_files.append({"file_id": media_obj.file_id, "type": mt})
                                        media_count += 1
                    except Exception as e_forward:
                        random_ids = [client.rnd_id() for _ in chunk]
                        result = await client.invoke(
                            raw.functions.messages.ForwardMessages(
                                from_peer=await client.resolve_peer(source_chat_id),
                                id=chunk,
                                to_peer=await client.resolve_peer(db_channel),
                                random_id=random_ids,
                                drop_author=True
                            )
                        )
                        raw_msg_ids = [u.message.id for u in result.updates if hasattr(u, "message") and hasattr(u.message, "id")]
                        if raw_msg_ids:
                            db_message_ids.extend(raw_msg_ids)
                            try:
                                raw_fetched = await client.get_messages(db_channel, raw_msg_ids)
                                for msg in raw_fetched:
                                    saved = await extract_and_save_media(msg, source_name=str(source_chat_id), source_title=source_chat_title)
                                    if saved and msg and msg.media:
                                        mt = msg.media.value
                                        if mt in ["document", "video", "audio", "photo", "animation"]:
                                            media_obj = getattr(msg, mt)
                                            immortal_files.append({"file_id": media_obj.file_id, "type": mt})
                                            media_count += 1
                            except Exception:
                                pass

                    processed_count += original_chunk_len
                    # 🔥 FIX: Progress bar is based on message-range position, capped at 100%
                    display_count = min(processed_count, total_msgs)
                    prog_str = get_progress_string(display_count, total_msgs, start_time)

                    # Update status every 5 chunks (500 files) to avoid edit limits on massive batches
                    if processed_count % 500 == 0 or processed_count >= total_msgs:
                        try:
                            await status_msg.edit_text(
                                f"🔄 **Batching in Progress...**\n"
                                f"🔁 Skipped: {skipped_count} duplicates\n\n{prog_str}",
                                reply_markup=cancel_kb
                            )
                        except Exception:
                            pass

                    await asyncio.sleep(2)
                    break  # Success — exit while True retry loop

                except FloodWait as e:
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    print(f"Batch chunk error: {e}")
                    processed_count += original_chunk_len
                    break  # Non-floodwait error — skip chunk to avoid infinite loop
            
        if process_id in active_processes:
            del active_processes[process_id]

        if media_count == 0 and len(db_message_ids) == 0:
            return await status_msg.edit_text(
                f"❌ **Batch Complete — No new files saved.**\n"
                f"🔁 All {skipped_count} files were already in the database."
            )

        url_hash = generate_hash()
        # 🔥 FIX: Store BOTH db_message_ids (fast channel delivery) AND immortal_files
        # (survives DB channel message deletions/additions). Delivery will prefer immortal_files
        # if db_message_ids become stale.
        await links_collection.insert_one({
            "hash": url_hash,
            "db_message_ids": db_message_ids,
            "immortal_files": immortal_files,       # <- NEW: permanent fallback
            "db_channel_id": db_channel,
            "creator_id": user_id
        })

        bot_username = (await client.get_me()).username
        shareable_link = f"https://t.me/{bot_username}?start={url_hash}"
        final_str = get_progress_string(min(processed_count, total_msgs), total_msgs, start_time)
        
        summary = (
            f"📦 **New files saved:** {media_count}\n"
            f"🔁 **Duplicates skipped:** {skipped_count}\n\n"
        )
        if is_cancelled:
            await status_msg.edit_text(
                f"⚠️ **Batch Partially Saved!**\n\n{summary}{final_str}\n\n"
                f"🔗 **Partial Link:**\n`{shareable_link}`"
            )
        else:
            await status_msg.edit_text(
                f"✅ **Batch Successful!**\n\n{summary}{final_str}\n\n"
                f"🔗 **Your permanent link:**\n`{shareable_link}`"
            )

    elif state_data["state"] == "waiting_for_channel_id":
        try:
            target_channel = int(message.text)
        except ValueError:
            return await message.reply("❌ Please send a valid numeric Channel ID (e.g., -1001234567890)")
            
        url_hash = state_data["hash"]
        del user_states[user_id]
        
        await deliver_content(client, message, url_hash, target_chat_id=target_channel)

# ==========================================
# 2. RETRIEVAL & UNIFIED DELIVERY WORKFLOW
# ==========================================

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    if len(message.command) > 1:
        url_hash = message.command[1]
        
        link_data = await links_collection.find_one({"hash": url_hash})
        if link_data:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Send to my DM 📩", callback_data=f"dm_{url_hash}")],
                [InlineKeyboardButton("Send to my Channel 📢", callback_data=f"ch_{url_hash}")]
            ])
            return await message.reply("Where would you like to receive the file(s)?", reply_markup=keyboard)
        
        await message.reply("❌ Invalid or expired link.")
    else:
        await message.reply("Hello! I am a permanent file store bot.\n\nUse `/batch` to clone channels, or `/dbupload` to package the immortal database.")

@app.on_callback_query(filters.regex(r"^(dm_|ch_)"))
async def handle_delivery_choice(client, callback_query):
    action, url_hash = callback_query.data.split("_")
    
    if action == "dm":
        await deliver_content(client, callback_query.message, url_hash, target_chat_id=callback_query.from_user.id)
        
    elif action == "ch":
        user_states[callback_query.from_user.id] = {
            "state": "waiting_for_channel_id",
            "hash": url_hash
        }
        await callback_query.message.edit_text("Please ensure I am an **Admin** in your destination channel.\n\nSend me the Channel ID (e.g., `-1001987654321`):")

@app.on_callback_query(filters.regex(r"^cancel_batch_"))
async def cancel_batch_cb(client, callback_query):
    user_id = callback_query.data.split("_")[2]
    if str(callback_query.from_user.id) != user_id:
        return await callback_query.answer("This is not your process!", show_alert=True)
    active_processes[f"batch_{user_id}"] = True
    await callback_query.answer("Stopping batch process...", show_alert=True)

@app.on_callback_query(filters.regex(r"^cancel_deliver_"))
async def cancel_deliver_cb(client, callback_query):
    url_hash = callback_query.data.split("_")[2]
    active_processes[f"deliver_{url_hash}"] = True
    await callback_query.answer("Stopping file delivery...", show_alert=True)

async def deliver_content(client, message, url_hash, target_chat_id):
    link_data = await links_collection.find_one({"hash": url_hash})
    if not link_data:
        return await message.reply("❌ Error: Link data not found.")
        
    if "source_channel_query" in link_data:
        ch_id = link_data["source_channel_query"]
        cursor = media_collection.find({"source_channel": ch_id})
        all_media = await cursor.to_list(length=None)
        # 🔥 FIX: _id is now file_unique_id; actual file_id is in doc["file_id"]
        files_list = [
            {"file_id": doc.get("file_id") or doc["_id"], "type": doc["type"]}
            for doc in all_media
            if doc.get("file_id") or doc.get("_id")
        ]
        is_immortal = True
        total_files = len(files_list)
        source_db_channel = DB_CHANNEL_ID
    else:
        # 🔥 FIX: Prefer immortal_files if available (survives DB channel edits/deletions).
        # Fall back to db_message_ids only if immortal_files is absent or empty.
        immortal_files_in_link = link_data.get("immortal_files", [])
        db_msg_ids_in_link = link_data.get("db_message_ids", [])
        if immortal_files_in_link:
            is_immortal = True
            files_list = immortal_files_in_link
        elif db_msg_ids_in_link:
            is_immortal = False
            files_list = db_msg_ids_in_link
        else:
            files_list = []
            is_immortal = False
        total_files = len(files_list)
        source_db_channel = link_data.get("db_channel_id", DB_CHANNEL_ID)

    if total_files == 0:
        return await message.reply("❌ Error: No files found in this link.")

    checkpoint = await active_deliveries.find_one({"hash": url_hash, "target": str(target_chat_id)})
    start_index = checkpoint["last_sent_index"] if checkpoint else 0

    process_id = f"deliver_{url_hash}"
    active_processes[process_id] = False
    start_time = time.time()
    cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Delivery", callback_data=f"cancel_deliver_{url_hash}")]])
    
    if start_index > 0:
        status_msg = await message.reply(f"🔄 **Resuming Delivery** to {target_chat_id} from file {start_index}...", reply_markup=cancel_kb)
    else:
        await active_deliveries.insert_one({"hash": url_hash, "target": str(target_chat_id), "last_sent_index": 0})
        status_msg = await message.reply(f"⏳ **Initializing Delivery** to {target_chat_id}...", reply_markup=cancel_kb)
    
    success_count = start_index
    remaining_files = files_list[start_index:]
    is_channel = str(target_chat_id).startswith("-100")

    # ==========================================
    # PATH A: ULTRA-FAST UPLOAD (Channels + Message_ids ONLY)
    # ==========================================
    if is_channel and not is_immortal:
        chunk_size = 100
        for i in range(0, len(remaining_files), chunk_size):
            if active_processes.get(process_id):
                await status_msg.edit_text(f"❌ **Delivery Cancelled at file {success_count}.**")
                del active_processes[process_id]
                return

            chunk = remaining_files[i:i + chunk_size]

            if not await ensure_peer_loaded(client, source_db_channel) or not await ensure_peer_loaded(client, target_chat_id):
                await status_msg.edit_text(f"❌ Unable to load destination peer {target_chat_id}.")
                break

            try:
                await client.forward_messages(
                    chat_id=target_chat_id,
                    from_chat_id=source_db_channel,
                    message_ids=chunk,
                    disable_notification=True,
                    drop_author=True
                )

                success_count += len(chunk)
                await active_deliveries.update_one(
                    {"hash": url_hash, "target": str(target_chat_id)},
                    {"$set": {"last_sent_index": success_count}},
                    upsert=True
                )

                prog_str = get_progress_string(success_count, total_files, start_time)
                try:
                    await status_msg.edit_text(f"🚀 **Fast-Uploading to Channel...**\n\n{prog_str}", reply_markup=cancel_kb)
                except Exception: pass

                await asyncio.sleep(2)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                try:
                    await asyncio.sleep(1)
                    await ensure_peer_loaded(client, target_chat_id, retries=2, delay=1)
                    await client.forward_messages(chat_id=target_chat_id, from_chat_id=source_db_channel, message_ids=chunk, drop_author=True)
                    success_count += len(chunk)
                except Exception: continue

    # ==========================================
    # PATH B: DRIP-FEED UPLOAD (DMs or Immortal File_IDs)
    # ==========================================
    else:
        for idx, item in enumerate(remaining_files, start=start_index + 1):
            if active_processes.get(process_id):
                await status_msg.edit_text(f"❌ **Delivery Cancelled at file {success_count}.**")
                del active_processes[process_id]
                return

            try:
                # If Immortal, use direct file sending (Indestructible backup)
                if is_immortal:
                    file_id = item["file_id"]
                    media_type = item["type"]
                    send_methods = {
                        "document": client.send_document, "video": client.send_video,
                        "audio": client.send_audio, "photo": client.send_photo, "animation": client.send_animation
                    }
                    send_fn = send_methods.get(media_type, client.send_document)
                    await send_fn(target_chat_id, file_id)
                # If Standard Batch, use copy_message
                else:
                    await client.resolve_peer(source_db_channel)
                    await client.copy_message(target_chat_id, source_db_channel, item)
                    
                success_count += 1
                
                if success_count % 50 == 0 or success_count == total_files:
                    await active_deliveries.update_one(
                        {"hash": url_hash, "target": str(target_chat_id)},
                        {"$set": {"last_sent_index": success_count}},
                        upsert=True
                    )

                if success_count % 10 == 0 or success_count == total_files:
                    prog_str = get_progress_string(success_count, total_files, start_time)
                    try:
                        await status_msg.edit_text(f"📥 **Sending files...**\n\n{prog_str}", reply_markup=cancel_kb)
                    except Exception: pass

                await asyncio.sleep(0.05) 
                
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                await status_msg.reply_text(f"❌ Error sending file. Process paused at {success_count}.\n\nRaw Error: `{e}`")
                break

    if process_id in active_processes:
        del active_processes[process_id]
            
    if success_count == total_files:
        await active_deliveries.delete_one({"hash": url_hash, "target": str(target_chat_id)})
        final_str = get_progress_string(success_count, total_files, start_time)
        await status_msg.edit_text(f"✅ **Delivery Complete!**\n\n{final_str}")


# ==========================================
# 🚨 BOOT SEQUENCE & AUTOMATED WARMUP 🚨
# ==========================================
async def warmup_peers():
    try:
        print("Loading DB Channels and Admins from MongoDB...")
        async for admin in admins_collection.find({}):
            ADMINS_CACHE.add(admin["user_id"])
        async for db_chan in db_channels_collection.find({}):
            DB_CHANNELS_CACHE.add(db_chan["channel_id"])
            
        print("Loading peer cache via dialogs...")
        # Exhaustively find the DB channels in dialogs to guarantee they are cached
        needed = DB_CHANNELS_CACHE.copy()
        if DB_CHANNEL_ID != 0: needed.add(DB_CHANNEL_ID)
        
        if needed:
            async for dialog in app.get_dialogs():
                if dialog.chat and dialog.chat.id in needed:
                    needed.remove(dialog.chat.id)
                if not needed:
                    break
            
        if DB_CHANNEL_ID != 0:
            try: await app.get_chat(DB_CHANNEL_ID)
            except Exception: pass
            
        for db_chan in DB_CHANNELS_CACHE:
            try: await app.get_chat(db_chan)
            except Exception: pass
            
        print("✅ Peer cache loaded successfully")
    except Exception as e:
        print("Warmup failed:", e)

async def resume_interrupted_deliveries():
    cursor = active_deliveries.find({})
    async for delivery in cursor:
        url_hash = delivery["hash"]
        print(f"Server restart detected. Auto-resume requires user to click /start {url_hash} again to re-bind the DM status window.")

if __name__ == "__main__":
    print("Bot starting...")

    # 🌐 Start the Flask live-status server first so the host sees an open port
    keep_alive()

    app.start()
    
    app.loop.run_until_complete(warmup_peers())
    app.loop.run_until_complete(resume_interrupted_deliveries())
    
    print("Bot ready")
    
    idle()
    app.stop()
