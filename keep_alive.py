from flask import Flask
from threading import Thread
import os
import time

app = Flask(__name__)

# Track the server start time for live uptime display
_start_time = time.time()

@app.route('/')
def home():
    uptime_seconds = int(time.time() - _start_time)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    uptime_str = f"{hours}h {minutes}m {seconds}s"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <!-- Live auto-refresh every 30 seconds so the page stays current -->
    <meta http-equiv="refresh" content="30">
    <title>🤖 FileStore Bot — Live Status</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #0f0c29, #302b63, #24243e);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
            color: #fff;
        }}
        .card {{
            background: rgba(255, 255, 255, 0.07);
            border: 1px solid rgba(255, 255, 255, 0.15);
            backdrop-filter: blur(20px);
            border-radius: 24px;
            padding: 48px 56px;
            text-align: center;
            max-width: 480px;
            width: 90%;
            box-shadow: 0 8px 40px rgba(0, 0, 0, 0.4);
            animation: fadeIn 0.6s ease;
        }}
        @keyframes fadeIn {{
            from {{ opacity: 0; transform: translateY(20px); }}
            to   {{ opacity: 1; transform: translateY(0); }}
        }}
        .pulse {{
            display: inline-block;
            width: 14px;
            height: 14px;
            background: #00e676;
            border-radius: 50%;
            margin-right: 8px;
            animation: pulse 1.4s ease-in-out infinite;
            vertical-align: middle;
        }}
        @keyframes pulse {{
            0%, 100% {{ box-shadow: 0 0 0 0 rgba(0,230,118,0.6); }}
            50%       {{ box-shadow: 0 0 0 10px rgba(0,230,118,0); }}
        }}
        h1 {{ font-size: 2rem; margin-bottom: 8px; letter-spacing: -0.5px; }}
        .status-badge {{
            display: inline-flex;
            align-items: center;
            background: rgba(0, 230, 118, 0.15);
            border: 1px solid rgba(0, 230, 118, 0.4);
            color: #00e676;
            border-radius: 999px;
            padding: 6px 18px;
            font-size: 0.85rem;
            font-weight: 600;
            margin: 16px 0 28px;
            letter-spacing: 0.5px;
            text-transform: uppercase;
        }}
        .stats {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-top: 24px;
        }}
        .stat-box {{
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 14px;
            padding: 16px 12px;
        }}
        .stat-label {{ font-size: 0.72rem; color: #aaa; text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 6px; }}
        .stat-value {{ font-size: 1.25rem; font-weight: 700; color: #e0e0ff; }}
        .refresh-note {{
            margin-top: 28px;
            font-size: 0.75rem;
            color: rgba(255,255,255,0.35);
        }}
    </style>
</head>
<body>
    <div class="card">
        <h1>🤖 FileStore Bot</h1>
        <div class="status-badge">
            <span class="pulse"></span> Live &amp; Running
        </div>
        <p style="color:rgba(255,255,255,0.6); font-size:0.95rem;">
            The Telegram bot is active and processing requests.
        </p>
        <div class="stats">
            <div class="stat-box">
                <div class="stat-label">Uptime</div>
                <div class="stat-value">{uptime_str}</div>
            </div>
            <div class="stat-box">
                <div class="stat-label">Status</div>
                <div class="stat-value" style="color:#00e676;">✓ OK</div>
            </div>
        </div>
        <p class="refresh-note">Page auto-refreshes every 30 s</p>
    </div>
</body>
</html>"""
    return html, 200

@app.route('/health')
def health():
    """Plain-text health endpoint — ideal for UptimeRobot / cron pings."""
    return "OK", 200

def run():
    port = int(os.environ.get("PORT", 8080))
    # use_reloader=False is required because we are inside a daemon thread
    app.run(host='0.0.0.0', port=port, use_reloader=False)

def keep_alive():
    """Start the Flask live-status server in a background daemon thread."""
    t = Thread(target=run)
    t.daemon = True   # thread dies automatically when the bot process exits
    t.start()
    print(f"[keep_alive] Live server started on port {os.environ.get('PORT', 8080)}")
