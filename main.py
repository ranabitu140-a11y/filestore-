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
        "🛠 **Bot Commands:**\n\n"
        "**User/Admin Commands:**\n"
        "• `/batch` - Clone a channel's media.\n"
        "• `/url` - List all stored channels and get their permanent links.\n"
        "• `/getchannel <channel_id>` - Get a link for all stored media from a specific channel.\n"
        "• `/renamechannel <channel_id> <new_name>` - Give a custom name to a stored channel.\n"
        "• `/dbupload` - Package the entire MongoDB immortal DB.\n"
        "• `/adddb <channel_id>` - Add a new dynamic DB channel.\n"
        "• `/deldb <channel_id>` - Remove a dynamic DB channel.\n\n"
        "**Owner Commands:**\n"
        "• `/addadmin <user_id>` - Add an admin.\n"
        "• `/deladmin <user_id>` - Remove an admin.\n"
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
async def extract_and_save_media(msg, source_name="DB Channel", source_title=None):
    """Extracts the immortal file_id from a message and saves it to MongoDB."""
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
            await media_collection.update_one(
                {"_id": fid},
                {"$set": {
                    "file_unique_id": uid,
                    "type": mt,
                    "file_name": fname,
                    "source_channel": str(source_name),
                    "source_title": source_title,
                    "added_date": datetime.now().isoformat()
                }},
                upsert=True
            )
            return True
        except Exception as e:
            print(f"[DB Error] Could not save media {fid}: {e}")
    return False

@app.on_message(filters.channel)
async def auto_index_channel_media(client, message):
    """Passively listens to the DB Channels and saves anything manually dropped there."""
    if message.chat.id in DB_CHANNELS_CACHE or message.chat.id == DB_CHANNEL_ID:
        await extract_and_save_media(message, source_name="Manual Drop")

# ==========================================
# 🚨 DATABASE PACKAGING COMMANDS 🚨
# ==========================================
@app.on_message(filters.command("dbupload") & filters.private)
async def db_upload_command(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    """Packages every single immortal file_id in the MongoDB into a single delivery link."""
    status = await message.reply("⏳ **Packaging all media from MongoDB...**")
    
    cursor = media_collection.find({})
    all_media = await cursor.to_list(length=None)
    
    if not all_media:
        return await status.edit_text("❌ **MongoDB is empty.**\nRun a /batch or send files to the DB Channel first.")
    
    total_files = len(all_media)
    url_hash = generate_hash()
    
    # 🚨 Convert the database documents into a lightweight delivery payload
    payload = [{"file_id": doc["_id"], "type": doc["type"]} for doc in all_media]
    
    await links_collection.insert_one({
        "hash": url_hash,
        "immortal_files": payload, # Saves as immortal files instead of message_ids
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



# ==========================================
# 1. MANUAL BATCH & SINGLE UPLOAD WORKFLOW
# ==========================================

@app.on_message(filters.command("batch") & filters.private)
async def start_batch(client, message):
    if not is_authorized(message.from_user.id): return await message.reply("⛔ Unauthorized.")
    user_states[message.from_user.id] = {"state": "waiting_first_msg"}
    await message.reply("Send or forward the **FIRST** message from your channel.")

@app.on_message(filters.private & ~filters.command(["start", "batch", "dbupload", "help", "addadmin", "deladmin", "adddb", "deldb", "getchannel", "url", "urls", "renamechannel"]))
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
        message_ids_to_process = list(range(first_msg_id, last_msg_id + 1))
        total_files = len(message_ids_to_process)
        chunk_size = 100 
        
        start_time = time.time()
        processed_count = 0
        is_cancelled = False

        for i in range(0, total_files, chunk_size):
            if active_processes.get(process_id):
                is_cancelled = True
                break

            chunk = message_ids_to_process[i:i + chunk_size]
            original_chunk_len = len(chunk)

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

                    chunk_len = len(chunk)

                    # --- DUPLICATE FILTERING ---
                    try:
                        fetched_msgs = await client.get_messages(source_chat_id, chunk)
                        valid_chunk = []
                        for msg in fetched_msgs:
                            if not msg or msg.empty or not msg.media:
                                continue
                            mt = msg.media.value
                            if mt in ["document", "video", "audio", "photo", "animation"]:
                                media = getattr(msg, mt)
                                if hasattr(media, "file_unique_id"):
                                    exists = await media_collection.find_one({"file_unique_id": media.file_unique_id})
                                    if exists:
                                        continue
                            valid_chunk.append(msg.id)
                            
                        if not valid_chunk:
                            processed_count += chunk_len
                            prog_str = get_progress_string(processed_count, total_files, start_time)
                            try:
                                await status_msg.edit_text(f"🔄 **Skipped {chunk_len} duplicates...**\n\n{prog_str}", reply_markup=cancel_kb)
                            except Exception: pass
                            continue
                        
                        chunk = valid_chunk
                    except Exception as filter_err:
                        print(f"Duplicate check error: {filter_err}")
                    # --- END DUPLICATE FILTERING ---

                    try:
                        forwarded_msgs = await client.forward_messages(
                            chat_id=db_channel,
                            from_chat_id=source_chat_id,
                            message_ids=chunk,
                            disable_notification=True,
                            drop_author=True
                        )
                        # 🚨 FIXED: Forcefully push the batched files into the immortal MongoDB collection
                        for msg in forwarded_msgs:
                            if msg and msg.id:
                                db_message_ids.append(msg.id)
                                await extract_and_save_media(msg, source_name=str(source_chat_id), source_title=source_chat_title)
                                
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
                            # Fetch the raw messages so we can extract their file_ids
                            try:
                                fetched_msgs = await client.get_messages(db_channel, raw_msg_ids)
                                for msg in fetched_msgs:
                                    await extract_and_save_media(msg, source_name=str(source_chat_id), source_title=source_chat_title)
                            except Exception: pass

                    processed_count += original_chunk_len
                    prog_str = get_progress_string(processed_count, total_files, start_time)
                    
                    # Only update status every 5 chunks (500 files) to avoid edit limits on massive 50k batches
                    if processed_count % 500 == 0 or processed_count == total_files:
                        try:
                            await status_msg.edit_text(f"🔄 **Batching in Progress...**\n\n{prog_str}", reply_markup=cancel_kb)
                        except Exception:
                            pass

                    await asyncio.sleep(2)
                    break  # Chunk successful, break out of the while True retry loop

                except FloodWait as e:
                    # If we get rate limited, sleep and the while True loop will try this chunk again!
                    await asyncio.sleep(e.value + 1)
                except Exception as e:
                    print(f"Batch chunk error: {e}")
                    break  # For non-floodwait errors, skip the chunk to avoid infinite loops
            
        del active_processes[process_id]

        if len(db_message_ids) == 0:
            return await status_msg.edit_text("❌ **Batch Cancelled.** No files were processed.")

        url_hash = generate_hash()
        await links_collection.insert_one({
            "hash": url_hash,
            "db_message_ids": db_message_ids, # Standard message_ids for fast channel chunking
            "db_channel_id": db_channel, # Store which DB channel holds these messages
            "creator_id": user_id
        })

        bot_username = (await client.get_me()).username
        shareable_link = f"https://t.me/{bot_username}?start={url_hash}"
        final_str = get_progress_string(processed_count, total_files, start_time)
        
        if is_cancelled:
            await status_msg.edit_text(f"⚠️ **Batch Partially Saved!**\n\n{final_str}\n\n🔗 **Partial Link:**\n`{shareable_link}`")
        else:
            await status_msg.edit_text(f"✅ **Batch Successful!**\n\n{final_str}\n\n🔗 **Your permanent link:**\n`{shareable_link}`")

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
        files_list = [{"file_id": doc["_id"], "type": doc["type"]} for doc in all_media]
        is_immortal = True
        total_files = len(files_list)
        source_db_channel = DB_CHANNEL_ID
    else:
        is_immortal = "immortal_files" in link_data
        files_list = link_data["immortal_files"] if is_immortal else link_data.get("db_message_ids", [])
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
