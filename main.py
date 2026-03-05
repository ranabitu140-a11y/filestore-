import os
import asyncio
import random
import string
import time
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

# State and Process managers
user_states = {}
active_processes = {}

# ==========================================
# 🚨 PEER CACHE & ADMIN HELPERS 🚨
# ==========================================
async def ensure_peer_loaded(client, chat_id, retries=3, delay=1.0):
    """
    Force Telegram to load the peer into session cache by calling get_chat().
    Retry a few times with small delay if it fails.
    Returns True if loaded, False otherwise.
    """
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
    """Return True if bot is a member/admin of chat_id and has permission to post."""
    me = await client.get_me()
    try:
        member = await client.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", "")
        is_admin = status in ("administrator", "creator")
        print(f"[admin-check] bot status in {chat_id}: {status}")
        return is_admin
    except Exception as e:
        print(f"[admin-check] get_chat_member failed for {chat_id}: {e}")
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
# 1. BATCH CREATION WORKFLOW
# ==========================================

@app.on_message(filters.command("batch") & filters.private)
async def start_batch(client, message):
    user_states[message.from_user.id] = {"state": "waiting_first_msg"}
    await message.reply("Send or forward the **FIRST** message from your channel.")

@app.on_message(filters.private & ~filters.command("start") & ~filters.command("batch"))
async def handle_batch_messages(client, message):
    user_id = message.from_user.id
    state_data = user_states.get(user_id)

    if not state_data:
        return 

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

        print(f"[debug] trying to resolve source={source_chat_id} db={DB_CHANNEL_ID}")

        for i in range(0, total_files, chunk_size):
            if active_processes.get(process_id):
                is_cancelled = True
                break

            chunk = message_ids_to_process[i:i + chunk_size]

            try:
                # Ensure source and DB channel peers are loaded (force)
                ok1 = await ensure_peer_loaded(client, source_chat_id)
                ok2 = await ensure_peer_loaded(client, DB_CHANNEL_ID)
                if not ok1 or not ok2:
                    await status_msg.edit_text("❌ Unable to load channel peers. Please ensure bot is in both channels.")
                    break

                # Try high-level forward first
                try:
                    forwarded_msgs = await client.forward_messages(
                        chat_id=DB_CHANNEL_ID,
                        from_chat_id=source_chat_id,
                        message_ids=chunk,
                        disable_notification=True,
                        drop_author=True
                    )
                    # Safely collect the newly generated message IDs
                    for msg in forwarded_msgs:
                        if msg and msg.id:
                            db_message_ids.append(msg.id)
                            
                except Exception as e_forward:
                    print(f"[batch] forward_messages failed, trying raw invoke: {e_forward}")
                    # Fallback to raw MTProto invoke
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
                    for update in result.updates:
                        if hasattr(update, "message") and hasattr(update.message, "id"):
                            db_message_ids.append(update.message.id)

                processed_count += len(chunk)
                prog_str = get_progress_string(processed_count, total_files, start_time)
                
                try:
                    await status_msg.edit_text(f"🔄 **Batching in Progress...**\n\n{prog_str}", reply_markup=cancel_kb)
                except Exception:
                    pass

                await asyncio.sleep(2)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                print(f"[batch] skipping chunk due to error: {e}")
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
# 2. RETRIEVAL & HYBRID DELIVERY WORKFLOW
# ==========================================

@app.on_message(filters.command("start") & filters.private)
async def start_command(client, message):
    if len(message.command) > 1:
        url_hash = message.command[1]
        
        link_data = await links_collection.find_one({"hash": url_hash})
        if not link_data:
            return await message.reply("❌ Invalid or expired link.")
        
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Send to my DM 📩", callback_data=f"dm_{url_hash}")],
            [InlineKeyboardButton("Send to my Channel 📢", callback_data=f"ch_{url_hash}")]
        ])
        await message.reply("Where would you like to receive these files?", reply_markup=keyboard)
    else:
        await message.reply("Hello! I am a permanent file store bot.")

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
    total_files = len(link_data["db_message_ids"])
    db_message_ids = link_data["db_message_ids"]

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

    print(f"[debug] trying to resolve db={DB_CHANNEL_ID} target={target_chat_id}")

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

            # Ensure peers are locked in
            if not await ensure_peer_loaded(client, DB_CHANNEL_ID) or not await ensure_peer_loaded(client, target_chat_id):
                await status_msg.edit_text(f"❌ Unable to load destination peer {target_chat_id}. Make sure I'm a member/admin.")
                break

            try:
                # High-level forward
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
                print(f"[deliver channel] chunk skipped: {e}")
                # Defensive retry loop on failure
                try:
                    await asyncio.sleep(1)
                    await ensure_peer_loaded(client, target_chat_id, retries=2, delay=1)
                    await client.forward_messages(chat_id=target_chat_id, from_chat_id=DB_CHANNEL_ID, message_ids=chunk, drop_author=True)
                    success_count += len(chunk)
                except Exception as e2:
                    print(f"[deliver channel] retry failed: {e2}")
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
    """Finds any deliveries that were interrupted by a server restart."""
    cursor = active_deliveries.find({})
    async for delivery in cursor:
        url_hash = delivery["hash"]
        print(f"Server restart detected. Auto-resume requires user to click /start {url_hash} again to re-bind the DM status window.") 

async def diagnose_and_warm(client, chat_id):
    """
    1) Logs bot id
    2) Tries get_chat() and get_chat_member()
    3) Returns tuple (ok:boolean, reason:str)
    """
    me = await client.get_me()
    print(f"[diag] running as: id={me.id} username=@{me.username}")

    try:
        info = await client.get_chat(chat_id)
        print(f"[diag] get_chat OK: id={getattr(info,'id',None)} title={getattr(info,'title',None)}")
    except Exception as e:
        print(f"[diag] get_chat FAILED for {chat_id}: {type(e).__name__}: {e}")
        # Try get_chat_member to get clearer reason
        try:
            member = await client.get_chat_member(chat_id, me.id)
            status = getattr(member, "status", None)
            print(f"[diag] get_chat_member succeeded unexpectedly: status={status}")
            if status in ("administrator", "creator"):
                return True, "member_and_admin_but_get_chat_failed"
            return False, f"member_but_not_admin:{status}"
        except Exception as e2:
            print(f"[diag] get_chat_member FAILED for {chat_id}: {type(e2).__name__}: {e2}")
            # common exception texts: "USER_NOT_PARTICIPANT", "CHAT_ADMIN_REQUIRED", etc.
            return False, f"not_member_or_inaccessible:{type(e2).__name__}:{e2}"

    # If get_chat succeeded, check bot permissions
    try:
        member = await client.get_chat_member(chat_id, me.id)
        status = getattr(member, "status", None)
        print(f"[diag] Bot membership status in {chat_id}: {status}")
        if status in ("administrator", "creator"):
            return True, "ok_admin"
        if status in ("member", "restricted"):
            return False, f"not_admin:{status}"
        return False, f"unknown_status:{status}"
    except Exception as e:
        print(f"[diag] get_chat_member exception after get_chat: {type(e).__name__}: {e}")
        return False, f"member_check_failed:{type(e).__name__}:{e}"

# Replace your existing ensure_peer_loaded with this smarter version
async def ensure_peer_loaded(client, chat_id, retries=5, delay=1.0):
    for attempt in range(1, retries + 1):
        ok, reason = await diagnose_and_warm(client, chat_id)
        if ok:
            print(f"[warmup] peer loaded {chat_id}")
            return True
        print(f"[warmup] attempt {attempt}/{retries} failed for {chat_id}: {reason}")
        # If it's a membership issue, no amount of retries will fix it; break early
        if reason.startswith("not_member") or reason.startswith("not_admin") or reason.startswith("not_member_or_inaccessible"):
            # return False quickly so caller can show a clear message
            return False
        await asyncio.sleep(delay)
    return False

if __name__ == "__main__":
    print("Bot starting...")
    
    app.start()
    
    app.loop.run_until_complete(warmup_peers())
    app.loop.run_until_complete(resume_interrupted_deliveries())
    
    print("Bot ready")
    
    idle()
    app.stop()
