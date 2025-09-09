import os
import re
from moviepy.editor import VideoFileClip
import yt_dlp
import discord
from datetime import datetime, timezone, timedelta
import asyncio

# === CONFIGURATION ===
MAX_DISCORD_FILESIZE_MB = 8
CHANNEL_IDS = [911164219238514688, 911164241216667678, 
               1005713269094350938, 912017250909847623, 
               912017322993139752, 912017388042592286, 
               912017417142689872, 1218954199916871691]
LOG_CHANNEL_ID = 1056057016235348039  # Channel for summary logs
LOOKBACK_DAYS = int(os.getenv('LOOKBACK_DAYS', 3))  # Default to 1 day; configurable via env

def sanitize_filename(text: str) -> str:
    """Remove unsafe characters from filename."""
    return re.sub(r'[\\/*?:"<>|]', '', text).strip()

def get_platform(url: str) -> str:
    """Detect platform from URL."""
    if "instagram.com/reel" in url:
        return "instagram"
    elif "youtube.com" in url or "youtu.be" in url:
        return "youtube"
    elif "reddit.com" in url:
        return "reddit"
    return "unknown"

def download_video(url: str) -> tuple[str, str]:
    """Download video using yt-dlp and return (filename, source_label)."""
    try:
        with yt_dlp.YoutubeDL({'quiet': True}) as ydl:
            info = ydl.extract_info(url, download=False)

            title = info.get("title", "untitled").split("\n")[0][:60]
            uploader = info.get("uploader") or info.get("channel") or info.get("subreddit") or "unknown"

            filename = sanitize_filename(f"{uploader} {title}.mp4")

            ydl_opts = {
                'format': 'bestvideo+bestaudio/best',
                'outtmpl': filename,
                'quiet': True,
                'merge_output_format': 'mp4',
            }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        return filename, uploader
    except Exception as e:
        print(f"❌ Download failed: {e}")
        return "", ""

def compress_video(input_path: str, max_mb: float) -> str:
    """Compress video if size > max_mb. Return path to final video."""
    size_mb = os.path.getsize(input_path) / (1024 * 1024)
    if size_mb <= max_mb:
        return input_path  # No need to compress

    output_path = f"compressed_{input_path}"
    try:
        clip = VideoFileClip(input_path)
        duration = clip.duration
        # Estimate a bitrate that results in the desired file size
        target_bitrate = (max_mb * 8192) / duration  # kbps

        clip.write_videofile(
            output_path,
            bitrate=f"{int(target_bitrate)}k",
            codec="libx264",
            audio_codec="aac",
            temp_audiofile="temp-audio.m4a",
            remove_temp=True,
            threads=4,
            verbose=False,
            logger=None
        )
        clip.close()
        return output_path
    except Exception as e:
        print(f"❌ Compression failed: {e}")
        return input_path  # Fallback to original

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

@client.event
async def on_ready():
    print(f'Logged in as {client.user}')

    # Track processed messages per channel for summary
    processed_counts = {channel_id: 0 for channel_id in CHANNEL_IDS}

    for idx, channel_id in enumerate(CHANNEL_IDS):
        channel = client.get_channel(channel_id)
        if channel is None:
            print(f"Channel {channel_id} not found. Ensure the bot has access to the channel.")
            continue

        # Get channel name or fallback to ID
        channel_display = f"{channel.name} ({channel_id})" if channel and hasattr(channel, 'name') else str(channel_id)

        # Find the last time the bot sent a message in this channel
        last_bot_msg = None
        async for msg in channel.history(limit=500):  # Limit to avoid fetching too many
            if msg.author == client.user:
                last_bot_msg = msg
                break

        if last_bot_msg:
            last_time = last_bot_msg.created_at + timedelta(seconds=1)  # Slight offset
        else:
            last_time = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

        # Fetch new messages after last_time, in oldest to newest order
        new_messages = [msg async for msg in channel.history(after=last_time, oldest_first=True)]

        for msg in new_messages:
            # Extract URLs from message content
            urls = re.findall(r'(https?://\S+)', msg.content)
            for url in urls:
                platform = get_platform(url)
                if platform != "unknown":
                    print(f"Processing URL: {url} from message {msg.id} in channel {channel_display}")
                    video_file, _ = download_video(url)
                    if video_file:
                        size_mb = os.path.getsize(video_file) / (1024 * 1024)
                        video_path = compress_video(video_file, MAX_DISCORD_FILESIZE_MB) if size_mb > MAX_DISCORD_FILESIZE_MB else video_file

                        # Prepare reply text
                        date_str = msg.created_at.strftime("%d-%b-%y")
                        discord_tag = str(msg.author)
                        text = f"sent on {date_str} by {discord_tag}"

                        try:
                            await msg.reply(content=text, file=discord.File(video_path), mention_author=False)
                            print(f"✅ Replied to message {msg.id} with video from {url} in channel {channel_display}")
                            processed_counts[channel_id] += 1
                        except Exception as e:
                            print(f"❌ Failed to reply to {msg.id} in channel {channel_display}: {e}")

                        # Clean up files
                        if os.path.exists(video_file):
                            os.remove(video_file)
                        if video_path != video_file and os.path.exists(video_path):
                            os.remove(video_path)

        print(f"Processing complete for channel {channel_display}.")

        # 1-minute cooldown before switching to the next channel (skip for the last one)
        if idx < len(CHANNEL_IDS) - 1:
            print("Waiting 1 minute before switching to the next channel...")
            await asyncio.sleep(60)

    # Send summary log to the log channel
    log_channel = client.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        summary = "\n".join(f"total {count} msg converted in <#{channel_id}>" for channel_id, count in processed_counts.items())
        try:
            await log_channel.send(summary if summary else "No messages processed.")
            print(f"✅ Sent summary to log channel {LOG_CHANNEL_ID}")
        except Exception as e:
            print(f"❌ Failed to send summary to log channel {LOG_CHANNEL_ID}: {e}")
    else:
        print(f"❌ Log channel {LOG_CHANNEL_ID} not found.")

    await client.close()

if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_BOT_TOKEN")
    if not TOKEN:
        print("❌ Error: DISCORD_BOT_TOKEN environment variable not set.")
        exit(1)
    client.run(TOKEN)
