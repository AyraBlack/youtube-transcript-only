import yt_dlp
import os
import shutil
import re  # For VTT parsing and filename sanitization
from flask import Flask, request, jsonify, send_from_directory, url_for, Response
import logging
import uuid  # For unique temporary transcript file names
from datetime import datetime  # For timestamped filenames

# --- Flask App Setup ---
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# --- Directory Configuration ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_BASE_DIR = os.path.join(BASE_DIR, "api_downloads")  # For MP3s
TRANSCRIPTS_TEMP_DIR = os.path.join(BASE_DIR, "api_transcripts_temp")  # For temporary VTT files

for d in (DOWNLOADS_BASE_DIR, TRANSCRIPTS_TEMP_DIR):
    if not os.path.exists(d):
        os.makedirs(d)
        app.logger.info(f"Created directory: {d}")

# --- Constants ---
SOCKET_TIMEOUT_SECONDS = 180
COMMON_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)

# --- Read Proxy from Environment Variable ---
PROXY_URL_FROM_ENV = os.environ.get('PROXY_URL')
if PROXY_URL_FROM_ENV:
    hidden = (
        PROXY_URL_FROM_ENV.split('@')[1]
        if '@' in PROXY_URL_FROM_ENV else 'Proxy configured (details hidden)'
    )
    app.logger.info(f"Using proxy from environment variable: {hidden}")
else:
    app.logger.info("PROXY_URL environment variable not set. Operating without proxy.")


def is_ffmpeg_available():
    """Checks if FFmpeg is installed and accessible."""
    return shutil.which("ffmpeg") is not None


def sanitize_filename(name_str, max_length=60):
    """Sanitizes a string to be a safe filename component."""
    s = name_str.replace(' ', '_')
    s = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in s)
    s = re.sub(r'_+', '_', s)
    s = s.strip('_')
    return s[:max_length]


def vtt_to_plaintext(vtt_content):
    """
    Converts VTT subtitle content to plain text, removing duplicates and tags.
    """
    processed_lines = []
    for line in vtt_content.splitlines():
        l = line.strip()
        # skip headers, timestamps, cue numbers
        if (l == 'WEBVTT' or '-->' in l or
            (l.isdigit() and not any(c.isalpha() for c in l))):
            continue
        if l:
            # remove any tags
            clean = re.sub(r'<[^>]+>', '', l)
            processed_lines.append(clean)
    # dedupe consecutive lines
    out = []
    last = None
    for ln in processed_lines:
        if ln != last:
            out.append(ln)
            last = ln
    return "\n".join(out)


def _get_common_ydl_opts():
    """Helper for shared yt-dlp options."""
    opts = {
        'socket_timeout': SOCKET_TIMEOUT_SECONDS,
        'http_headers': {'User-Agent': COMMON_USER_AGENT},
        'logger': app.logger,
        'noplaylist': True,
        'noprogress': True,
        'verbose': False,
    }
    if PROXY_URL_FROM_ENV:
        opts['proxy'] = PROXY_URL_FROM_ENV
    return opts


def extract_audio_from_video(video_url, audio_format="mp3"):
    """Downloads audio from a video URL."""
    app.logger.info(f"Audio extraction request for URL: {video_url}")
    if not is_ffmpeg_available():
        err = "FFmpeg is not installed or not found. It is required for audio conversion."
        app.logger.error(err)
        return {"error": err, "audio_server_path": None, "audio_relative_path": None}

    try:
        common = _get_common_ydl_opts()
        # fetch metadata
        with yt_dlp.YoutubeDL({**common, 'quiet': True, 'extract_flat': 'in_playlist'}) as ydl_info:
            info = ydl_info.extract_info(video_url, download=False)
        title = info.get('title') or f'video_{uuid.uuid4().hex[:6]}'

        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        base_name = f"{ts}_{sanitize_filename(title)}"
        out_dir = os.path.join(DOWNLOADS_BASE_DIR, base_name)
        os.makedirs(out_dir, exist_ok=True)

        template = os.path.join(out_dir, f"{base_name}.%(ext)s")
        dl_opts = {
            **common,
            'format': 'bestaudio/best',
            'outtmpl': template,
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': audio_format}],
            'quiet': False,
            'noprogress': False,
        }
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            code = ydl.download([video_url])
            if code != 0:
                return {"error": f"yt-dlp failed with code {code}", "audio_server_path": None, "audio_relative_path": None}

        final = os.path.join(out_dir, f"{base_name}.{audio_format}")
        if not os.path.exists(final):
            return {"error": "Audio file not found after processing.", "audio_server_path": None, "audio_relative_path": None}

        return {"error": None, "audio_server_path": final, "audio_relative_path": os.path.join(base_name, f"{base_name}.{audio_format}")}

    except Exception as e:
        app.logger.error(f"Error in extract_audio_from_video: {e}", exc_info=True)
        return {"error": str(e), "audio_server_path": None, "audio_relative_path": None}


def get_youtube_transcript_text(video_url):
    """Downloads the most appropriate subtitle (English or Romanian), parses it, and returns text."""
    app.logger.info(f"Transcript request for YouTube URL: {video_url}")
    result = {"transcript_text": None, "language_detected": None, "error": None}

    # 1) Probe available caption langs
    probe_opts = {
        **_get_common_ydl_opts(),
        'skip_download': True,
        'writesubtitles': False,
        'writeautomaticsub': True,
        'subtitleslangs': ['en', 'ro'],
    }
    try:
        with yt_dlp.YoutubeDL(probe_opts) as ydl_probe:
            info = ydl_probe.extract_info(video_url, download=False)
        available = set(info.get('subtitles', {})) | set(info.get('automatic_captions', {}))
        # decide language: prefer English if present
        if 'en' in available and 'ro' not in available:
            lang = 'en'
        elif 'ro' in available and 'en' not in available:
            lang = 'ro'
        elif 'en' in available and 'ro' in available:
            lang = 'en'  # both exist -> choose English
        else:
            lang = 'en'  # fallback
        app.logger.info(f"Detected caption languages: {available} → choosing '{lang}'")
    except Exception as e:
        app.logger.error(f"Error detecting caption languages: {e}", exc_info=True)
        lang = 'en'

    # 2) Download only that language
    base = f"transcript_{uuid.uuid4().hex}"
    outtmpl = os.path.join(TRANSCRIPTS_TEMP_DIR, base)
    dl_opts = {
        **_get_common_ydl_opts(),
        'writesubtitles': True,
        'writeautomaticsub': True,
        'subtitleslangs': [lang],
        'subtitlesformat': 'vtt',
        'skip_download': True,
        'outtmpl': outtmpl,
        'quiet': False,
        'noprogress': False,
    }
    vtt_path = None
    try:
        with yt_dlp.YoutubeDL(dl_opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
        subs = info.get('requested_subtitles') or {}
        if lang in subs and subs[lang].get('filepath') and os.path.exists(subs[lang]['filepath']):
            vtt_path = subs[lang]['filepath']
        else:
            fallback = f"{outtmpl}.{lang}.vtt"
            if os.path.exists(fallback):
                vtt_path = fallback

        if not vtt_path:
            raise FileNotFoundError(f"No .{lang}.vtt found after download")

        content = open(vtt_path, 'r', encoding='utf-8').read()
        result['transcript_text'] = vtt_to_plaintext(content)
        result['language_detected'] = lang
        app.logger.info(f"Transcript parsed successfully for language: {lang}")

    except yt_dlp.utils.DownloadError as de:
        app.logger.error(f"yt-dlp DownloadError: {de}")
        result['error'] = str(de)
    except Exception as e:
        app.logger.error(f"Error in get_youtube_transcript_text: {e}", exc_info=True)
        result['error'] = str(e)
    finally:
        if vtt_path and os.path.exists(vtt_path):
            try:
                os.remove(vtt_path)
                app.logger.info(f"Deleted temporary file: {vtt_path}")
            except Exception as ex:
                app.logger.error(f"Could not delete temp file {vtt_path}: {ex}")

    return result

# --- API Endpoints ---
@app.route('/api/extract_audio', methods=['GET'])
def api_extract_audio():
    app.logger.info("Received request for /api/extract_audio")
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    res = extract_audio_from_video(url)
    data = {"audio_download_url": None, "audio_server_path": res.get("audio_server_path"), "error": res.get("error")}
    if res.get("audio_relative_path"):
        data['audio_download_url'] = url_for('serve_downloaded_file', relative_file_path=res['audio_relative_path'], _external=True)
    return (jsonify(data), 500) if data['error'] else (jsonify(data), 200)

@app.route('/api/get_youtube_transcript', methods=['GET'])
def api_get_youtube_transcript():
    app.logger.info("Received request for /api/get_youtube_transcript")
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "Missing 'url' parameter"}), 400
    res = get_youtube_transcript_text(url)
    if res.get('error'):
        return jsonify({"error": res['error'], "language_detected": res.get('language_detected'), "transcript_text": None}), 500
    return Response(res['transcript_text'], mimetype='text/plain; charset=utf-8')

@app.route('/files/<path:relative_file_path>')
def serve_downloaded_file(relative_file_path):
    app.logger.info(f"Serving file: {relative_file_path}")
    try:
        return send_from_directory(DOWNLOADS_BASE_DIR, relative_file_path, as_attachment=True)
    except FileNotFoundError:
        return jsonify({"error": "File not found."}), 404
    except Exception as e:
        app.logger.error(f"Error serving file: {e}", exc_info=True)
        return jsonify({"error": "Internal server error."}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy"}), 200

# --- Main Execution (dev only) ---
if __name__ == '__main__':
    app.logger.info("--- Starting Flask app locally ---")
    if not is_ffmpeg_available():
        app.logger.critical("FFmpeg not found. This API requires FFmpeg.")
    app.run(host='0.0.0.0', port=5001, debug=True)
