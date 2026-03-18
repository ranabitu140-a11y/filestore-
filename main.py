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

# --- Configuration & Credentials ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "0"))
MONGO_URI = os.environ.get("MONGO_URI", "")

# --- Initialization ---
app = Client("permanent_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# Async MongoDB Connection
db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.bot_database
links_collection = db.stored_links
active_deliveries = db.active_deliveries
media_collection = db.media 
auto_batch_collection = db.auto_batch # Master list for all files

# State and Process managers
user_states = {}
active_processes = {}

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
# 🚨 AUTO-BATCH COMMANDS & LISTENERS 🚨
# ==========================================
@app.on_message(filters.chat(DB_CHANNEL_ID))
async def auto_index_channel_media(client, message):
    """
    Catches files YOU manually forward to the DB Channel, saving them to the master list.
    """
    if message.media:
        try:
            await auto_batch_collection.update_one(
                {"_id": "master_backup"},
                {"$addToSet": {"message_ids": message.id}},
                upsert=True
            )
            print(f"[Auto-Batch] Indexed Manual Message ID: {message.id}")
        except Exception as e:
            print(f"[Auto-Batch] DB Error: {e}")

@app.on_message(filters.command("dbupload") & filters.private)
async def db_upload_command(client, message):
    """Packages the ENTIRE auto-saved database into a single shareable link."""
    status = await message.reply("⏳ **Packaging ALL saved media from the database...**")
    
    doc = await auto_batch_collection.find_one({"_id": "master_backup"})
    if not doc or not doc.get("message_ids"):
        return await status.edit_text("❌ **No media found in the database.**\nRun a /batch or send files to the DB Channel first.")
    
    # Sort IDs so they deliver in the exact order they were uploaded
    db_message_ids = sorted(list(doc["message_ids"])) 
    total_files = len(db_message_ids)
    
    url_hash = generate_hash()
    
    await links_collection.insert_one({
        "hash": url_hash,
        "db_message_ids": db_message_ids,
        "creator_id": message.from_user.id
    })

    bot_username = (await client.get_me()).username
    shareable_link = f"https://t.me/{bot_username}?start={url_hash}"

    await status.edit_text(
        f"✅ **Entire Database Packaged Successfully!**\n\n"
        f"📦 **Total Saved Files:** {total_files}\n\n"
        f"🔗 **Your Master Link:**\n`{shareable_link}`\n\n"
        f"*(Tap the link to deliver all these files at once)*"
    )

@app.on_message(filters.command("dbclear") & filters.private)
async def db_clear_command(client, message):
    """Empties the auto-batch MongoDB list for a fresh start."""
    await auto_batch_collection.delete_one({"_id": "master_backup"})
    await message.reply("🗑️ **Database Cleared!**\nYour master list is now empty. New batches will start fresh.")


# ==========================================
# 1. MANUAL BATCH & SINGLE UPLOAD WORKFLOW
# ==========================================

@app.on_message(filters.command("batch") & filters.private)
async def start_batch(client, message):
    user_states[message.from_user.id] = {"state": "waiting_first_msg"}
    await message.reply("Send or forward the **FIRST** message from your channel.")

@app.on_message(filters.private & ~filters.command("start") & ~filters.command("batch") & ~filters.command("dbupload") & ~filters.command("dbclear"))
async def handle_batch_messages(client, message):
    user_id = message.from_user.id
    state_data = user_states.get(user_id)

    # --- PM SINGLE UPLOAD INTERCEPTOR ---
    if not state_data:
        if message.media and message.media.value in ["document", "video", "audio", "photo", "animation"]:
            status = await message.reply("⏳ Uploading to DB Channel for permanent storage...")
            try:
                copied_msg = await message.copy(DB_CHANNEL_ID)
                
                # Instantly add this to the master auto-batch list
                await auto_batch_collection.update_one(
                    {"_id": "master_backup"},
                    {"$addToSet": {"message_ids": copied_msg.id}},
                    upsert=True
                )
                
                media = getattr(message, message.media.value)
                url_hash = generate_hash()
                
                await media_collection.insert_one({
                    "_id": media.file_id, 
                    "hash": url_hash,
                    "message_id": copied_msg.id,
                    "type": message.media.value,
                    "file_name": getattr(media, "file_name", f"{message.media.value}_{media.file_unique_id}"),
                    "source_channel": DB_CHANNEL_ID,
                    "added_date": datetime.now().isoformat()
                })
                
                bot_username = (await client.get_me()).username
                shareable_link = f"https://t.me/{bot_username}?start={url_hash}"
                await status.edit_text(
                    f"✅ **Single File Saved & Added to Database!**\n\n"
                    f"🔗 **Your Permanent Single Link:**\n`{shareable_link}`"
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
            "first_msg_id": message.forward_from_message_id
        }
        await message.reply("First message saved. Now forward the **LAST** message.")

    elif state_data["state"] == "waiting_last_msg":
        if not message.forward_from_chat:
            return await message.reply("Please *forward* the message directly from the channel.")
        
        first_msg_id = state_data["first_msg_id"]
        last_msg_id = message.forward_from_message_id
        source_chat_id = state_data["source_chat_id"]
        
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

            try:
                ok1 = await ensure_peer_loaded(client, source_chat_id)
                ok2 = await ensure_peer_loaded(client, DB_CHANNEL_ID)
                if not ok1 or not ok2:
                    await status_msg.edit_text("❌ Unable to load channel peers. Please ensure bot is in both channels.")
                    break

                try:
                    forwarded_msgs = await client.forward_messages(
                        chat_id=DB_CHANNEL_ID,
                        from_chat_id=source_chat_id,
                        message_ids=chunk,
                        disable_notification=True,
                        drop_author=True
                    )
                    
                    # 🚨 FIXED: Forcefully push the bot's own uploads into the master list
                    chunk_db_ids = []
                    for msg in forwarded_msgs:
                        if msg and msg.id:
                            db_message_ids.append(msg.id)
                            chunk_db_ids.append(msg.id)
                            
                    if chunk_db_ids:
                        await auto_batch_collection.update_one(
                            {"_id": "master_backup"},
                            {"$addToSet": {"message_ids": {"$each": chunk_db_ids}}},
                            upsert=True
                        )
                            
                except Exception as e_forward:
                    random_ids = [client.rnd_id() for _ in chunk]
                    result = await client.invoke(
                        raw.functions.messages.ForwardMessages(
                            from_peer=await client.resolve_peer(source_chat_id),
                            id=chunk,
                            to_peer=await client.resolve_peer(DB_CHANNEL_ID),
                            random_id=random_ids,
                            drop_author=True
                        )
                    )
                    
                    chunk_db_ids = []
                    for update in result.updates:
                        if hasattr(update, "message") and hasattr(update.message, "id"):
                            db_message_ids.append(update.message.id)
                            chunk_db_ids.append(update.message.id)
                            
                    if chunk_db_ids:
                        await auto_batch_collection.update_one(
                            {"_id": "master_backup"},
                            {"$addToSet": {"message_ids": {"$each": chunk_db_ids}}},
                            upsert=True
                        )

                processed_count += len(chunk)
                prog_str = get_progress_string(processed_count, total_files, start_time)
                
                try:
                    await status_msg.edit_text(f"🔄 **Batching in Progress...**\n\n{prog_str}", reply_markup=cancel_kb)
                except Exception:
                    pass

                await asyncio.sleep(2)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass
            
        del active_processes[process_id]

        if len(db_message_ids) == 0:
            return await status_msg.edit_text("❌ **Batch Cancelled.** No files were processed.")

        url_hash = generate_hash()
        await links_collection.insert_one({
            "hash": url_hash,
            "db_message_ids": db_message_ids,
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
        media_data = await media_collection.find_one({"hash": url_hash})
        
        if link_data or media_data:
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Send to my DM 📩", callback_data=f"dm_{url_hash}")],
                [InlineKeyboardButton("Send to my Channel 📢", callback_data=f"ch_{url_hash}")]
            ])
            return await message.reply("Where would you like to receive the file(s)?", reply_markup=keyboard)
        
        await message.reply("❌ Invalid or expired link.")
    else:
        await message.reply("Hello! I am a permanent file store bot.\n\nUse `/batch` to manually clone channels, or `/dbupload` to package everything I've auto-saved from the DB Channel.")

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
    media_data = await media_collection.find_one({"hash": url_hash})
    
    db_message_ids = []
    
    if link_data:
        db_message_ids = link_data.get("db_message_ids", [])
    elif media_data and "message_id" in media_data:
        db_message_ids = [media_data["message_id"]]

    total_files = len(db_message_ids)
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
    remaining_files = db_message_ids[start_index:]
    is_channel = str(target_chat_id).startswith("-100")

    # ==========================================
    # PATH A: ULTRA-FAST UPLOAD (FOR CHANNELS)
    # ==========================================
    if is_channel:
        chunk_size = 100
        for i in range(0, len(remaining_files), chunk_size):
            if active_processes.get(process_id):
                await status_msg.edit_text(f"❌ **Delivery Cancelled at file {success_count}.**\n(Click link again to resume later)")
                del active_processes[process_id]
                return

            chunk = remaining_files[i:i + chunk_size]

            if not await ensure_peer_loaded(client, DB_CHANNEL_ID) or not await ensure_peer_loaded(client, target_chat_id):
                await status_msg.edit_text(f"❌ Unable to load destination peer {target_chat_id}. Make sure I'm a member/admin.")
                break

            try:
                await client.forward_messages(
                    chat_id=target_chat_id,
                    from_chat_id=DB_CHANNEL_ID,
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
                except Exception:
                    pass

                await asyncio.sleep(2)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                try:
                    await asyncio.sleep(1)
                    await ensure_peer_loaded(client, target_chat_id, retries=2, delay=1)
                    await client.forward_messages(chat_id=target_chat_id, from_chat_id=DB_CHANNEL_ID, message_ids=chunk, drop_author=True)
                    success_count += len(chunk)
                except Exception:
                    continue

    # ==========================================
    # PATH B: SAFE DRIP-FEED (FOR USER DMs)
    # ==========================================
    else:
        for idx, msg_id in enumerate(remaining_files, start=start_index + 1):
            if active_processes.get(process_id):
                await status_msg.edit_text(f"❌ **Delivery Cancelled at file {success_count}.**\n(Click link again to resume later)")
                del active_processes[process_id]
                return

            try:
                await client.resolve_peer(DB_CHANNEL_ID)
                
                await client.copy_message(
                    chat_id=target_chat_id, 
                    from_chat_id=DB_CHANNEL_ID, 
                    message_id=msg_id
                )
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
                        await status_msg.edit_text(f"📥 **Sending to DM...**\n\n{prog_str}", reply_markup=cancel_kb)
                    except Exception:
                        pass

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
        print("Loading peer cache...")
        await app.get_chat(DB_CHANNEL_ID)
        
        async for dialog in app.get_dialogs():
            pass
            
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
    
    app.start()
    
    app.loop.run_until_complete(warmup_peers())
    app.loop.run_until_complete(resume_interrupted_deliveries())
    
    print("Bot ready")
    
    idle()
    app.stop()
