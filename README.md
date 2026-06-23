# 📦 Telegram Permanent File Store Bot

A powerful Telegram bot that stores files **permanently** using MongoDB and Telegram's own CDN as a backend. It supports bulk channel cloning, immortal file-ID storage, and resumable delivery to DMs or channels — all via shareable deep-link URLs.

---

## ✨ Features

- **Bulk Batch Upload** — Forward a range of messages from any channel and get a single permanent shareable link.
- **Single File Upload** — Drop any file in the bot's DM to instantly get a permanent link.
- **Immortal File IDs** — File IDs are saved to MongoDB, surviving channel deletions.
- **Resumable Delivery** — If delivery is interrupted, the bot picks up from where it left off.
- **DM & Channel Delivery** — Send files to your own DM or push them to any channel where the bot is admin.
- **Auto-Indexing** — Any file manually dropped into the DB Channel is automatically indexed.
- **Master Database Link** — Package the entire MongoDB into a single `/dbupload` link.
- **Docker-Ready** — Ships with a `Dockerfile` and a GitHub Actions CI/CD pipeline.

---

## 🔧 Required Environment Variables

| Variable        | Required | Description                                                                 |
|-----------------|----------|-----------------------------------------------------------------------------|
| `API_ID`        | ✅ Yes    | Your Telegram App's API ID — get it from [my.telegram.org](https://my.telegram.org) |
| `API_HASH`      | ✅ Yes    | Your Telegram App's API Hash — get it from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN`     | ✅ Yes    | Your bot token from [@BotFather](https://t.me/BotFather)                   |
| `DB_CHANNEL_ID` | ✅ Yes    | Default DB Channel ID. Falls back to dynamic DB channels if set. |
| `MONGO_URI`     | ✅ Yes    | MongoDB connection string (e.g., `mongodb+srv://...`) |
| `OWNER_ID`      | ✅ Yes    | The numeric Telegram User ID of the bot owner. |

> **Note:** If any variable is missing or wrong, the bot will silently fail to connect. All 5 variables are mandatory.

### GitHub Actions Secrets (for Docker CI/CD)

| Secret            | Description                              |
|-------------------|------------------------------------------|
| `DOCKER_USERNAME` | Your Docker Hub username                 |
| `DOCKER_PASSWORD` | Your Docker Hub password or access token |

---

## 🚀 Deployment

### Option 1 — Docker (Recommended)

```bash
docker run -d \
  -e API_ID=your_api_id \
  -e API_HASH=your_api_hash \
  -e BOT_TOKEN=your_bot_token \
  -e DB_CHANNEL_ID=-1001234567890 \
  -e MONGO_URI=mongodb+srv://... \
  bitu757/filestore:latest
```

### Option 2 — Manual (Python)

```bash
# 1. Clone the repo
git clone https://github.com/ranabitu140-a11y/filestore-.git
cd filestore-

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set environment variables
export API_ID=your_api_id
export API_HASH=your_api_hash
export BOT_TOKEN=your_bot_token
export DB_CHANNEL_ID=-1001234567890
export MONGO_URI=mongodb+srv://...

# 4. Run the bot
python main.py
```

### Option 3 — `.env` File (Local Dev)

Create a `.env` file in the root directory (it is git-ignored):

```env
API_ID=12345678
API_HASH=abcdef1234567890abcdef1234567890
BOT_TOKEN=123456789:AAFxxxxxxxxxxxxxxxxx
DB_CHANNEL_ID=-1001234567890
MONGO_URI=mongodb+srv://user:password@cluster.mongodb.net/
```

> Then use `python-dotenv` to load it, or export manually before running.

---

## 📋 Bot Commands

### User / Admin Commands
| Command | Description |
|---|---|
| `/start` | Delivery mechanism for hashed links. |
| `/batch` | Clone a channel by forwarding the first and last message. |
| `/dbupload` | Package the entire MongoDB into one master link. |
| `/dbclear` | Clear the MongoDB media collection. |
| `/getchannel <id>`| Generate a link for all media from a specific source channel. |
| `/url` | List all stored channels by name and ID with permanent links. |
| `/renamechannel <id> <name>` | Assign a custom name to a channel in MongoDB. |
| `/adddb <id>` | Dynamically add a channel to act as a database. |
| `/deldb <id>` | Remove a dynamic database channel. |
| `/help` | List all commands. |

### Owner Commands
| Command | Description |
|---|---|
| `/addadmin <id>` | Authorize a user to use admin commands. |
| `/deladmin <id>` | Revoke a user's admin access. |

---

## 🏗️ Architecture

```
User ──/batch──► Bot ──forward──► DB Channel (Telegram)
                       │               │
                       │         extract_and_save_media()
                       │               │
                       └──────────────►MongoDB (motor)
                                       │
User ──/start?hash──► Bot ──find_one──►MongoDB
                           │
                    deliver_content()
                           │
                   DM or Target Channel
```

---

## 📦 Tech Stack

| Technology       | Role                                        |
|------------------|---------------------------------------------|
| **Python 3.11**  | Runtime                                     |
| **Pyrogram**     | Telegram MTProto client                     |
| **TgCrypto**     | Fast crypto backend for Pyrogram            |
| **Motor**        | Async MongoDB driver                        |
| **MongoDB**      | Persistent file ID & link storage           |
| **Docker**       | Containerization                            |
| **GitHub Actions** | CI/CD — auto-build & push to Docker Hub   |

---

## ⚠️ Security Vulnerabilities & Known Issues

> These are documented so you can fix them before going to production.

### 🔴 Critical

1. **~~No Authorization Check~~ (Fixed)** — A strict `OWNER_ID` and admin-whitelist system is now active. Only authorized users can execute bot commands.
2. **Bare `except` blocks silently swallow errors** — Several `except Exception: pass` blocks hide real failures, making debugging very difficult and allowing corrupt states to persist.

### 🟠 High

3. **`active_processes` is an in-memory dict** — If the bot restarts, all running process states are lost, but the MongoDB `active_deliveries` records remain, causing ghost checkpoints.
4. **`cancel_batch_cb` uses `split("_")[2]`** — The `callback_data` is `cancel_batch_<user_id>`. Splitting on `_` and taking index `[2]` will break if the user_id ever gets prefixed with additional underscores. Use `rsplit("_", 1)[1]` instead.
5. **`cancel_deliver_cb` has the same `split` bug** — `cancel_deliver_<url_hash>`. If the hash contains `_`, index `[2]` will be wrong.

### 🟡 Medium

6. **No rate-limit protection for `/dbclear`** — A malicious user can spam the command to repeatedly wipe the database.
7. **`user_states` is in-memory** — Restarting the bot while a user is mid-batch workflow drops their state silently.
8. **`handle_delivery_choice` splits on first `_`** — `action, url_hash = callback_query.data.split("_")` — this will **crash** if the hash contains an underscore, since `split("_")` returns more than 2 parts.
9. **MongoDB `_id` is the `file_id`** — Telegram `file_id`s are not permanent across different bots. Using them as `_id` means a DB restored on a different bot will have broken IDs.

### 🟢 Low / Best Practices

10. **No logging framework** — All output uses `print()`. Use Python's `logging` module for log levels, file output, and structured logs.
11. **`asyncio.sleep(0.05)` anti-flood is too aggressive** — 20 messages/second will trigger Telegram's flood limit for DMs quickly.
12. **`requirements.txt` has no pinned versions** — `pyrogram`, `motor`, etc. have no version pins. A future breaking release will silently break the bot.

---

## 🔒 Security Hardening Checklist

- [x] Add an `OWNER_ID` env variable and guard all admin commands with `is_authorized` check.
- [ ] Pin all dependency versions in `requirements.txt`
- [ ] Replace `except Exception: pass` with proper logging
- [ ] Fix `split("_")` callback parsing to use `split("_", 1)` or `split("_", 2)`
- [ ] Add a `.env.example` file for new contributors
- [ ] Store `user_states` in Redis or MongoDB for persistence across restarts

---

## 📄 License

This project is open-source. Use responsibly and comply with [Telegram's ToS](https://telegram.org/tos).