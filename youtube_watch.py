"""
youtube_watch.py
用途：監控三個台股 YouTuber 頻道，偵測新影片，抓取逐字稿，
      呼叫 Claude 產生摘要／分析／建議，發送至 Telegram。

資料來源：
  - YouTube RSS feed（偵測新影片，免金鑰）
  - youtube-transcript-api（字幕逐字稿，免金鑰，優先嘗試）
  - yt-dlp 下載音訊 + OpenAI Whisper API（字幕不可用時的備援）
  - Anthropic Messages API（摘要／分析／建議）
"""

import os
import re
import json
import time
import subprocess
import tempfile
import shutil
import xml.etree.ElementTree as ET

import requests
from dotenv import load_dotenv

from notify import send

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
# YouTube 會封鎖 GitHub Actions 的機房 IP，透過 Webshare 住宅代理繞過
WEBSHARE_PROXY_USERNAME = os.getenv("WEBSHARE_PROXY_USERNAME")
WEBSHARE_PROXY_PASSWORD = os.getenv("WEBSHARE_PROXY_PASSWORD")
YT_DLP_PROXY_ARGS = (
    ["--proxy", f"http://{WEBSHARE_PROXY_USERNAME}-rotate:{WEBSHARE_PROXY_PASSWORD}@p.webshare.io:80"]
    if WEBSHARE_PROXY_USERNAME and WEBSHARE_PROXY_PASSWORD else []
)
# yt-dlp 被 YouTube 判定為機器人時的錯誤訊息片段
YT_BOT_CHECK_MARKERS = ("not a bot", "cookies-from-browser", "--cookies for the authentication")

SEEN_PATH = os.path.join(BASE_DIR, "youtube_seen.json")
MAX_FAILURES = 3          # 同一支影片失敗超過這個次數就放棄，不再重試
MAX_SEEN_PER_CHANNEL = 30  # 每個頻道只保留最近 N 個已處理影片 ID

CHANNELS = [
    {"id": "UCvnLmiWt_zIVIh0zUm_j4Hw", "name": "老王愛說笑 (oldwangstock)"},
    {"id": "UCuXjCbU_zwwuE0rMbJfY9aQ", "name": "一綺聊股 (HAMILIU)"},
    {"id": "UCQvsuaih5lE0n_Ne54nNezg", "name": "理財達人秀 (EBCmoneyshow)"},
]

ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}

YT_DLP_RETRY_ARGS = [
    [],
    ["--extractor-args", "youtube:player_client=android"],
    ["--extractor-args", "youtube:player_client=ios"],
    ["--extractor-args", "youtube:player_client=tv"],
]

TRANSCRIPT_LANGS = ["zh-Hant", "zh-TW", "zh", "zh-Hans", "en"]


# ---------- 狀態檔 ----------

def load_state():
    if not os.path.exists(SEEN_PATH):
        return {}
    with open(SEEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_channel_state(state, channel_id):
    return state.setdefault(channel_id, {"seen": [], "failed": {}})


def mark_seen(state, channel_id, video_id):
    ch = get_channel_state(state, channel_id)
    if video_id not in ch["seen"]:
        ch["seen"].append(video_id)
    ch["seen"] = ch["seen"][-MAX_SEEN_PER_CHANNEL:]
    ch["failed"].pop(video_id, None)


def mark_failed(state, channel_id, video_id):
    ch = get_channel_state(state, channel_id)
    ch["failed"][video_id] = ch["failed"].get(video_id, 0) + 1
    if ch["failed"][video_id] >= MAX_FAILURES:
        print(f"⚠️  {video_id} 連續失敗 {ch['failed'][video_id]} 次，放棄並標記為已處理")
        mark_seen(state, channel_id, video_id)


# ---------- RSS：偵測新影片 ----------

def fetch_latest_videos(channel_id):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    videos = []
    for entry in root.findall("atom:entry", ATOM_NS):
        video_id = entry.findtext("yt:videoId", namespaces=ATOM_NS)
        title = entry.findtext("atom:title", namespaces=ATOM_NS)
        published = entry.findtext("atom:published", namespaces=ATOM_NS)
        link_el = entry.find("atom:link", ATOM_NS)
        link = link_el.get("href") if link_el is not None else f"https://www.youtube.com/watch?v={video_id}"
        videos.append({"video_id": video_id, "title": title, "published": published, "link": link})
    return videos


# ---------- 逐字稿：字幕優先，Whisper 備援 ----------

def get_transcript_native(video_id):
    """回傳 (逐字稿, 是否被 YouTube 封鎖)。"""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api.proxies import WebshareProxyConfig
        proxy_config = None
        if WEBSHARE_PROXY_USERNAME and WEBSHARE_PROXY_PASSWORD:
            proxy_config = WebshareProxyConfig(
                proxy_username=WEBSHARE_PROXY_USERNAME,
                proxy_password=WEBSHARE_PROXY_PASSWORD,
            )
        api = YouTubeTranscriptApi(proxy_config=proxy_config)
        fetched = api.fetch(video_id, languages=TRANSCRIPT_LANGS)
        text = " ".join(s.text for s in fetched.snippets).strip()
        return (text if text else None), False
    except Exception as e:
        print(f"  字幕抓取失敗（{type(e).__name__}），改用 Whisper 備援")
        return None, type(e).__name__ in ("RequestBlocked", "IpBlocked")


def is_live_or_upcoming(video_id):
    """直播預告／進行中的直播還沒有完整內容，先跳過，等結束後再處理。"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        result = subprocess.run(
            ["yt-dlp", "--no-warnings", *YT_DLP_PROXY_ARGS, "--print", "live_status", url],
            capture_output=True, text=True, timeout=60,
        )
        status = result.stdout.strip()
        return status in ("is_live", "is_upcoming")
    except Exception:
        return False


def download_audio(video_id, out_dir):
    """回傳 (mp3 路徑, 是否每次都被 YouTube 判定為機器人)。"""
    url = f"https://www.youtube.com/watch?v={video_id}"
    out_template = os.path.join(out_dir, "%(id)s.%(ext)s")
    all_bot_checked = True

    for extra_args in YT_DLP_RETRY_ARGS:
        cmd = [
            "yt-dlp",
            "-f", "ba/bestaudio",
            "--extract-audio",
            "--audio-format", "mp3",
            "--audio-quality", "9",
            "-o", out_template,
            *YT_DLP_PROXY_ARGS,
            *extra_args,
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            print(f"  yt-dlp 逾時（args={extra_args}）")
            all_bot_checked = False
            continue

        mp3_path = os.path.join(out_dir, f"{video_id}.mp3")
        if result.returncode == 0 and os.path.exists(mp3_path):
            return mp3_path, False

        if not any(m in result.stderr for m in YT_BOT_CHECK_MARKERS):
            all_bot_checked = False
        print(f"  yt-dlp 失敗（args={extra_args}）：{result.stderr.strip()[-300:]}")
        time.sleep(2)

    return None, all_bot_checked


def split_audio(mp3_path, out_dir, chunk_seconds=600):
    """用 ffmpeg 把音訊切成每段 chunk_seconds 秒，避免超過 Whisper API 25MB 上限。"""
    chunk_template = os.path.join(out_dir, "chunk_%03d.mp3")
    cmd = [
        "ffmpeg", "-y", "-i", mp3_path,
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-c", "copy", chunk_template,
    ]
    subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    chunks = sorted(
        os.path.join(out_dir, f) for f in os.listdir(out_dir) if re.match(r"chunk_\d+\.mp3$", f)
    )
    return chunks


WHISPER_BACKENDS = [
    ("groq", "https://api.groq.com/openai/v1/audio/transcriptions", "whisper-large-v3", GROQ_API_KEY),
    ("openai", "https://api.openai.com/v1/audio/transcriptions", "whisper-1", OPENAI_API_KEY),
]


def _transcribe_chunk(chunk_path):
    """依序嘗試 Groq → OpenAI，回傳第一個成功的轉錄文字。"""
    for backend_name, url, model, api_key in WHISPER_BACKENDS:
        if not api_key:
            continue
        try:
            with open(chunk_path, "rb") as f:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (os.path.basename(chunk_path), f, "audio/mpeg")},
                    data={"model": model},
                    timeout=300,
                )
            if resp.status_code == 200:
                return resp.json().get("text", "")
            print(f"  ❌ {backend_name} Whisper API 錯誤（{chunk_path}）：{resp.status_code} {resp.text[:200]}")
        except requests.exceptions.RequestException as e:
            print(f"  ❌ {backend_name} Whisper 請求失敗（{chunk_path}）：{e}")
    return None


def transcribe_with_whisper(mp3_path, out_dir):
    if not GROQ_API_KEY and not OPENAI_API_KEY:
        print("  ❌ 找不到 GROQ_API_KEY 或 OPENAI_API_KEY，無法使用 Whisper 備援")
        return None

    size_mb = os.path.getsize(mp3_path) / (1024 * 1024)
    chunks = [mp3_path] if size_mb <= 24 else split_audio(mp3_path, out_dir)
    if not chunks:
        return None

    texts = []
    for chunk_path in chunks:
        text = _transcribe_chunk(chunk_path)
        if text:
            texts.append(text)

    text = " ".join(t for t in texts if t).strip()
    return text if text else None


def get_transcript(video_id, out_dir):
    """回傳 (逐字稿, 是否被 YouTube 封鎖)。字幕跟音訊下載都被封鎖時才算封鎖。"""
    text, native_blocked = get_transcript_native(video_id)
    if text:
        print("  ✅ 使用原生字幕")
        return text, False

    mp3_path, download_blocked = download_audio(video_id, out_dir)
    if not mp3_path:
        print("  ❌ 音訊下載失敗")
        return None, native_blocked and download_blocked

    text = transcribe_with_whisper(mp3_path, out_dir)
    if text:
        print("  ✅ 使用 Whisper 備援轉錄")
    return text, False


# ---------- Claude 摘要／分析／建議 ----------

def summarize_with_claude(channel_name, title, published, link, transcript):
    if not ANTHROPIC_API_KEY:
        print("  ❌ 找不到 ANTHROPIC_API_KEY，無法產生摘要")
        return None

    max_chars = 60000
    if len(transcript) > max_chars:
        transcript = transcript[:max_chars]

    prompt = f"""以下是 YouTuber「{channel_name}」新上傳影片的逐字稿。

影片標題：{title}
發布時間：{published}
影片連結：{link}

逐字稿：
{transcript}

請用繁體中文、以台股投資人的角度，整理：
1. 📝 內容摘要（這支影片主要在講什麼，3-5 點）
2. 📊 分析（提到的個股、產業、或市場觀點，重點整理）
3. 💡 建議（根據影片內容，給出你自己的看法與操作建議，若影片沒有明確提到投資建議則說明目前沒有具體建議）

請控制在 Telegram 訊息適合閱讀的長度（約 800-1200 字），用條列式呈現，不要輸出逐字稿原文。"""

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-5",
                "max_tokens": 8000,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=300,
        )
        if resp.status_code != 200:
            print(f"  ❌ Anthropic API 錯誤：{resp.status_code} {resp.text[:300]}")
            return None
        data = resp.json()
        text = "".join(block.get("text", "") for block in data.get("content", []))
        if not text.strip():
            block_types = [block.get("type") for block in data.get("content", [])]
            print(f"  ❌ Claude 回傳空白內容：stop_reason={data.get('stop_reason')} blocks={block_types}")
        return text
    except requests.exceptions.RequestException as e:
        print(f"  ❌ Anthropic 請求失敗：{e}")
        return None


# ---------- Telegram 訊息組裝 ----------

def build_message(channel_name, title, published, link, analysis):
    header = (
        f"🎬 <b>{channel_name}</b> 新影片\n"
        f"📌 {title}\n"
        f"🕒 {published}\n"
        f"🔗 {link}\n\n"
    )
    return header + analysis


def send_chunked(message, limit=4000):
    if len(message) <= limit:
        return send(message)
    ok = True
    for i in range(0, len(message), limit):
        if not send(message[i:i + limit]):
            ok = False
    return ok


# ---------- 主流程 ----------

def process_video(state, channel_id, channel_name, video):
    video_id = video["video_id"]
    print(f"🎬 處理新影片：{channel_name} - {video['title']} ({video_id})")

    if is_live_or_upcoming(video_id):
        print("  ⏳ 直播尚未結束，暫不處理，下次排程再檢查")
        return

    tmp_dir = tempfile.mkdtemp(prefix=f"ytw_{video_id}_")
    try:
        transcript, blocked = get_transcript(video_id, tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not transcript and blocked:
        # 封鎖是 IP 的問題、不是影片的問題，不計入失敗次數，避免影片被誤放棄
        print("  🚫 被 YouTube 封鎖（機器人驗證），不計失敗次數，下次排程再試")
        return

    if not transcript:
        print("  ❌ 無法取得逐字稿，稍後重試")
        mark_failed(state, channel_id, video_id)
        return

    analysis = summarize_with_claude(channel_name, video["title"], video["published"], video["link"], transcript)
    if not analysis:
        print("  ❌ 摘要產生失敗，稍後重試")
        mark_failed(state, channel_id, video_id)
        return

    message = build_message(channel_name, video["title"], video["published"], video["link"], analysis)
    if send_chunked(message):
        print("  ✅ 已發送至 Telegram")
        mark_seen(state, channel_id, video_id)
    else:
        print("  ❌ Telegram 發送失敗，稍後重試")
        mark_failed(state, channel_id, video_id)


def main():
    state = load_state()
    any_processed = False

    for channel in CHANNELS:
        channel_id = channel["id"]
        channel_name = channel["name"]
        ch_state = get_channel_state(state, channel_id)

        try:
            videos = fetch_latest_videos(channel_id)
        except Exception as e:
            print(f"❌ 無法取得 {channel_name} 的 RSS feed：{e}")
            continue

        if not ch_state["seen"] and not ch_state["failed"]:
            # 第一次執行：不要把現有所有舊影片都發送出去，只記錄目前最新的影片為已讀
            print(f"🆕 {channel_name}：首次執行，記錄現有影片為已讀，不發送通知")
            for v in videos:
                mark_seen(state, channel_id, v["video_id"])
            continue

        new_videos = [v for v in videos if v["video_id"] not in ch_state["seen"]]
        new_videos.reverse()  # 舊的先處理

        for video in new_videos:
            process_video(state, channel_id, channel_name, video)
            any_processed = True
            save_state(state)  # 每支影片處理完立即存檔，避免中斷遺失進度

    if not any_processed:
        print("📭 三個頻道都沒有新影片")

    save_state(state)


if __name__ == "__main__":
    main()
