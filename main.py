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
API_HASH = os.environ.get("API_HASH", ""))
BOT_TOKEN = os.environ.get("BOT_TOKEN", ""))
DB_CHANNEL_ID = int(os.environ.get("DB_CHANNEL_ID", "0"))
MONGO_URI = os.environ.get("MONGO_URI", ""))

# --- Initialization ---
app = Client("permanent_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

db_client = AsyncIOMotorClient(MONGO_URI)
db = db_client.bot_database
links_collection = db.stored_links
active_deliveries = db.active_deliveries
peer_cache_col = db.peer_cache          # ← NEW: stores access_hash per channel

user_states = {}
active_processes = {}

# ==========================================
# 🔑 PEER CACHE — save & restore access_hash
# ==========================================

async def save_peer(client, chat_id: int):
    """
    Resolve a channel peer and persist its access_hash to MongoDB.
    Returns the raw InputPeer on success, None on failure.
    """
    try:
        peer = await client.resolve_peer(chat_id)
        access_hash = getattr(peer, "access_hash", None)
        channel_id  = getattr(peer, "channel_id", None)
        if access_hash is not None and channel_id is not None:
            await peer_cache_col.update_one(
                {"chat_id": chat_id},
                {"$set": {
                    "chat_id":     chat_id,
                    "channel_id":  channel_id,
                    "access_hash": access_hash,
                }},
                upsert=True,
            )
            print(f"[peer-cache] saved  chat_id={chat_id}  access_hash={access_hash}")
        return peer
    except Exception as e:
        print(f"[peer-cache] save_peer failed for {chat_id}: {e}")
        return None


async def restore_peers(client):
    """
    On startup: inject every saved access_hash straight into Pyrogram's
    SQLite session so resolve_peer() works immediately, without any warmup.
    """
    async for doc in peer_cache_col.find({}):
        chat_id     = doc["chat_id"]
        channel_id  = doc["channel_id"]
        access_hash = doc["access_hash"]
        try:
            # Pyrogram 2.x stores peers via its SQLite storage layer.
            # update_peers() accepts a list of (id, access_hash, type, username, phone_number)
            await client.storage.update_peers(
                [(channel_id, access_hash, "channel", None, None)]
            )
            print(f"[peer-cache] restored chat_id={chat_id} into session")
        except Exception as e:
            print(f"[peer-cache] restore failed for {chat_id}: {e}")


async def get_peer(client, chat_id: int):
    """
    Guaranteed peer resolution:
      1. Try resolve_peer() directly (works if already in session).
      2. Try get_messages() to force Telegram to send us the peer info.
      3. Fall back to saved access_hash from MongoDB and build InputChannel manually.
    Saves to MongoDB on every success so the next restart is instant.
    """
    # ── Step 1: direct resolve ──────────────────────────────────────────────
    try:
        peer = await client.resolve_peer(chat_id)
        await save_peer(client, chat_id)   # keep DB fresh
        return peer
    except Exception:
        pass

    # ── Step 2: force Telegram to hand us the peer via get_messages ─────────
    try:
        # Fetching any message (even non-existent id=1) makes Telegram reply
        # with the channel object, which populates Pyrogram's peer cache.
        await client.get_messages(chat_id, message_ids=[1])
        peer = await client.resolve_peer(chat_id)
        await save_peer(client, chat_id)
        print(f"[get_peer] warmed via get_messages: {chat_id}")
        return peer
    except Exception as e:
        print(f"[get_peer] get_messages warmup failed for {chat_id}: {e}")

    # ── Step 3: build InputChannel from saved access_hash ───────────────────
    doc = await peer_cache_col.find_one({"chat_id": chat_id})
    if doc:
        try:
            peer = raw.types.InputChannel(
                channel_id=doc["channel_id"],
                access_hash=doc["access_hash"],
            )
            print(f"[get_peer] using cached access_hash for {chat_id}")
            return peer
        except Exception as e:
            print(f"[get_peer] cached peer build failed for {chat_id}: {e}")

    print(f"[get_peer] ❌ all strategies exhausted for {chat_id}")
    return None


def db_id():
    return DB_CHANNEL_ID


# ==========================================
# Utilities
# ==========================================
def generate_hash(length=8):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

def get_progress_string(current, total, start_time):
    percent = (current / total) * 100 if total else 0
    elapsed = time.time() - start_time
    speed = current / elapsed if elapsed > 0 else 0
    remaining = total - current
    eta = remaining / speed if speed > 0 else 0
    m, s = divmod(int(eta), 60)
    h, m = divmod(m, 60)
    eta_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
    filled = int(percent / 10) if total else 0
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
            "first_msg_id": message.forward_from_message_id,
        }
        await message.reply("First message saved. Now forward the **LAST** message.")

    elif state_data["state"] == "waiting_last_msg":
        if not message.forward_from_chat:
            return await message.reply("Please *forward* the message directly from the channel.")
        first_msg_id = state_data["first_msg_id"]
        last_msg_id  = message.forward_from_message_id
        source_chat_id = state_data["source_chat_id"]
        del user_states[user_id]

        process_id = f"batch_{user_id}"
        active_processes[process_id] = False
        cancel_kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Batch", callback_data=f"cancel_batch_{user_id}")]])
        status_msg = await message.reply("⏳ **Initializing batch process...**", reply_markup=cancel_kb)

        db_message_ids = []
        message_ids_to_process = list(range(first_msg_id, last_msg_id + 1))
        total_files = len(message_ids_to_process)
        chunk_size  = 100
        start_time  = time.time()
        processed_count = 0
        is_cancelled = False

        # ── Pre-resolve both peers once before the loop ──────────────────────
        src_peer = await get_peer(client, source_chat_id)
        dst_peer = await get_peer(client, db_id())
        if not src_peer or not dst_peer:
            return await status_msg.edit_text("❌ Cannot access source or DB channel. Make sure I'm an admin in both.")

        for i in range(0, total_files, chunk_size):
            if active_processes.get(process_id):
                is_cancelled = True
                break

            chunk = message_ids_to_process[i:i + chunk_size]
            try:
                # High-level forward (uses resolved peers, already warm)
                try:
                    forwarded = await client.forward_messages(
                        chat_id=db_id(),
                        from_chat_id=source_chat_id,
                        message_ids=chunk,
                        disable_notification=True,
                        drop_author=True,
                    )
                    msgs = forwarded if isinstance(forwarded, list) else [forwarded]
                    for msg in msgs:
                        if msg and getattr(msg, "id", None):
                            db_message_ids.append(msg.id)

                except Exception as e_fwd:
                    print(f"[batch] forward_messages failed, trying raw: {e_fwd}")
                    result = await client.invoke(
                        raw.functions.messages.ForwardMessages(
                            from_peer=src_peer,
                            id=chunk,
                            to_peer=dst_peer,
                            random_id=[client.rnd_id() for _ in chunk],
                            drop_author=True,
                        )
                    )
                    for upd in result.updates:
                        if hasattr(upd, "message") and hasattr(upd.message, "id"):
                            db_message_ids.append(upd.message.id)

                processed_count += len(chunk)
                try:
                    prog = get_progress_string(processed_count, total_files, start_time)
                    await status_msg.edit_text(f"🔄 **Batching in Progress...**\n\n{prog}", reply_markup=cancel_kb)
                except Exception:
                    pass
                await asyncio.sleep(2)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                print(f"[batch] chunk skipped: {e}")
                continue

        if process_id in active_processes:
            del active_processes[process_id]

        if not db_message_ids:
            return await status_msg.edit_text("❌ **Batch Cancelled.** No files were processed.")

        url_hash = generate_hash()
        await links_collection.insert_one({
            "hash": url_hash,
            "db_message_ids": db_message_ids,
            "creator_id": user_id,
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
            [InlineKeyboardButton("Send to my DM 📩",      callback_data=f"dm_{url_hash}")],
            [InlineKeyboardButton("Send to my Channel 📢", callback_data=f"ch_{url_hash}")],
        ])
        await message.reply("Where would you like to receive these files?", reply_markup=keyboard)
    else:
        await message.reply("Hello! I am a permanent file store bot.")

@app.on_callback_query(filters.regex(r"^(dm_|ch_)"))
async def handle_delivery_choice(client, callback_query):
    action, url_hash = callback_query.data.split("_", 1)
    if action == "dm":
        await deliver_content(client, callback_query.message, url_hash, target_chat_id=callback_query.from_user.id)
    elif action == "ch":
        user_states[callback_query.from_user.id] = {"state": "waiting_for_channel_id", "hash": url_hash}
        await callback_query.message.edit_text(
            "Please ensure I am an **Admin** in your destination channel.\n\n"
            "Send me the Channel ID (e.g., `-1001987654321`):"
        )

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
        return await message.reply("❌ Link data not found or expired.")

    db_message_ids = link_data["db_message_ids"]
    total_files    = len(db_message_ids)

    checkpoint  = await active_deliveries.find_one({"hash": url_hash, "target": str(target_chat_id)})
    start_index = checkpoint["last_sent_index"] if checkpoint else 0

    process_id = f"deliver_{url_hash}"
    active_processes[process_id] = False
    start_time = time.time()
    cancel_kb  = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel Delivery", callback_data=f"cancel_deliver_{url_hash}")]])

    if start_index > 0:
        status_msg = await message.reply(f"🔄 **Resuming Delivery** from file {start_index}...", reply_markup=cancel_kb)
    else:
        await active_deliveries.insert_one({"hash": url_hash, "target": str(target_chat_id), "last_sent_index": 0})
        status_msg = await message.reply(f"⏳ **Initializing Delivery**...", reply_markup=cancel_kb)

    success_count   = start_index
    remaining_files = db_message_ids[start_index:]
    is_channel      = str(target_chat_id).startswith("-100")

    # ── Pre-resolve DB peer once ─────────────────────────────────────────────
    db_peer = await get_peer(client, db_id())
    if not db_peer:
        return await status_msg.edit_text(f"❌ Cannot access DB channel. Check bot membership.")

    # PATH A: Channel destination — chunk forward
    if is_channel:
        target_peer = await get_peer(client, target_chat_id)
        if not target_peer:
            return await status_msg.edit_text(f"❌ Cannot access destination channel {target_chat_id}. Make sure I'm an admin.")

        chunk_size = 100
        for i in range(0, len(remaining_files), chunk_size):
            if active_processes.get(process_id):
                await status_msg.edit_text(f"❌ **Delivery Cancelled at file {success_count}.**\n(Click link again to resume later)")
                del active_processes[process_id]
                return

            chunk = remaining_files[i:i + chunk_size]
            try:
                await client.forward_messages(
                    chat_id=target_chat_id,
                    from_chat_id=db_id(),
                    message_ids=chunk,
                    disable_notification=True,
                    drop_author=True,
                )
                success_count += len(chunk)
                await active_deliveries.update_one(
                    {"hash": url_hash, "target": str(target_chat_id)},
                    {"$set": {"last_sent_index": success_count}},
                    upsert=True,
                )
                prog = get_progress_string(success_count, total_files, start_time)
                try:
                    await status_msg.edit_text(f"🚀 **Fast-Uploading to Channel...**\n\n{prog}", reply_markup=cancel_kb)
                except Exception:
                    pass
                await asyncio.sleep(2)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                print(f"[deliver channel] chunk error: {e}")
                continue

    # PATH B: DM destination — copy one by one
    else:
        for idx, msg_id in enumerate(remaining_files, start=start_index + 1):
            if active_processes.get(process_id):
                await status_msg.edit_text(f"❌ **Delivery Cancelled at file {success_count}.**\n(Click link again to resume later)")
                del active_processes[process_id]
                return
            try:
                await client.copy_message(
                    chat_id=target_chat_id,
                    from_chat_id=db_id(),
                    message_id=msg_id,
                )
                success_count += 1

                if success_count % 50 == 0 or success_count == total_files:
                    await active_deliveries.update_one(
                        {"hash": url_hash, "target": str(target_chat_id)},
                        {"$set": {"last_sent_index": success_count}},
                        upsert=True,
                    )
                if success_count % 10 == 0 or success_count == total_files:
                    prog = get_progress_string(success_count, total_files, start_time)
                    try:
                        await status_msg.edit_text(f"📥 **Sending to DM...**\n\n{prog}", reply_markup=cancel_kb)
                    except Exception:
                        pass
                await asyncio.sleep(0.05)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception as e:
                await status_msg.reply_text(f"❌ Error at file {success_count}.\n\n`{e}`")
                break

    if process_id in active_processes:
        del active_processes[process_id]

    if success_count == total_files:
        await active_deliveries.delete_one({"hash": url_hash, "target": str(target_chat_id)})
        final_str = get_progress_string(success_count, total_files, start_time)
        await status_msg.edit_text(f"✅ **Delivery Complete!**\n\n{final_str}")


# ==========================================
# BOOT SEQUENCE
# ==========================================
async def warmup_peers():
    print("[boot] Restoring cached peers from MongoDB...")
    await restore_peers(app)           # ← inject saved access_hashes into session

    print("[boot] Resolving DB channel peer...")
    peer = await get_peer(app, db_id())
    if peer:
        print(f"[boot] DB channel peer ready: {peer}")
    else:
        print("[boot] ⚠️  DB channel peer could NOT be resolved. Check bot membership.")

async def resume_interrupted_deliveries():
    cursor = active_deliveries.find({})
    async for delivery in cursor:
        print(f"[boot] Resumable delivery found: hash={delivery['hash']} target={delivery['target']}")


if __name__ == "__main__":
    print("Bot starting...")
    app.start()
    app.loop.run_until_complete(warmup_peers())
    app.loop.run_until_complete(resume_interrupted_deliveries())
    print("Bot ready")
    try:
        idle()
    finally:
        app.stop()
