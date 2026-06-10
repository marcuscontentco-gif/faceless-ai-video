import os
import subprocess
import requests
import tempfile
import threading

# ── CRITICAL: set FFMPEG_BINARY env var BEFORE importing moviepy ─────────────
# moviepy.config reads this at module-load time via os.getenv()
try:
    import imageio_ffmpeg as _iio_ff
    os.environ["FFMPEG_BINARY"] = _iio_ff.get_ffmpeg_exe()
    print(f"[FFMPEG] Using imageio-ffmpeg binary: {_iio_ff.get_ffmpeg_exe()}")
except Exception as _e:
    print(f"[FFMPEG] imageio-ffmpeg not available: {_e}")
# ─────────────────────────────────────────────────────────────────────────────

from flask import Flask, request, jsonify
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# Environment Variables
PEXELS_API_KEY        = os.environ.get('PEXELS_API_KEY')
YOUTUBE_CLIENT_ID     = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')

OUTPUT_WIDTH  = 1280
OUTPUT_HEIGHT = 720

def get_ffmpeg():
    """Return path to the imageio-ffmpeg binary (modern, reliable)."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()

def download_file(url, output_path):
    response = requests.get(url, stream=True, timeout=120)
    response.raise_for_status()
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

def get_pexels_clips(keywords):
    headers = {'Authorization': PEXELS_API_KEY}
    all_clips = []
    for keyword in keywords[:4]:
        keyword = keyword.strip()
        if not keyword:
            continue
        url = (
            f'https://api.pexels.com/videos/search'
            f'?query={keyword}&per_page=5&orientation=landscape&size=medium'
        )
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            data = resp.json()
            for video in data.get('videos', []):
                exact = [f for f in video['video_files'] if f.get('width') == OUTPUT_WIDTH]
                hd    = [f for f in video['video_files'] if f.get('width', 0) > OUTPUT_WIDTH]
                sd    = [f for f in video['video_files'] if f.get('width', 0) >= 640]
                files = exact or hd or sd or video['video_files']
                if files:
                    best = sorted(files, key=lambda x: abs(x.get('width', 0) - OUTPUT_WIDTH))[0]
                    all_clips.append({'url': best['link'], 'duration': video['duration']})
        except Exception as e:
            print(f"[Pexels] Error for '{keyword}': {e}")
    return all_clips

def get_audio_duration(audio_path):
    """Get audio duration in seconds using ffmpeg."""
    ffmpeg = get_ffmpeg()
    result = subprocess.run(
        [ffmpeg, '-i', audio_path],
        stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    for line in result.stderr.split('\n'):
        if 'Duration:' in line:
            time_str = line.split('Duration:')[1].split(',')[0].strip()
            try:
                h, m, s = time_str.split(':')
                return int(h) * 3600 + int(m) * 60 + float(s)
            except Exception:
                pass
    return 60.0  # fallback

def assemble_video(audio_path, clip_paths, output_path):
    """
    Pure FFMPEG subprocess video assembly — bypasses MoviePy/imageio entirely.
    Uses imageio_ffmpeg's modern bundled binary so any codec works.
    """
    ffmpeg   = get_ffmpeg()
    duration = get_audio_duration(audio_path)
    print(f"[Assemble] Audio duration: {duration:.1f}s, clips available: {len(clip_paths)}")

    with tempfile.TemporaryDirectory() as work_dir:
        # Build a concat list — repeat clips until we have ~2x the audio duration
        concat_file = os.path.join(work_dir, 'concat.txt')
        lines       = []
        total       = 0.0
        while total < duration * 2:
            for path in clip_paths:
                lines.append(f"file '{path}'")
                total += 10   # rough ~10s per clip; FFMPEG cuts at audio end via -shortest
                if total >= duration * 2:
                    break

        with open(concat_file, 'w') as f:
            f.write('\n'.join(lines))

        # Single FFMPEG pass: concat clips → scale → mux voiceover → cut at audio end
        cmd = [
            ffmpeg, '-y',
            '-f', 'concat', '-safe', '0', '-i', concat_file,  # video input
            '-i', audio_path,                                   # audio input
            '-vf', (
                f'scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}'
                f':force_original_aspect_ratio=decrease,'
                f'pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:'
                f'(ow-iw)/2:(oh-ih)/2:black'
            ),
            '-c:v', 'libx264',
            '-preset', 'ultrafast',
            '-crf', '28',
            '-c:a', 'aac',
            '-b:a', '128k',
            '-map', '0:v:0',
            '-map', '1:a:0',
            '-shortest',          # cut output at end of audio
            output_path,
        ]
        print(f"[Assemble] Running FFMPEG ...")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[Assemble] FFMPEG stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"FFMPEG failed (code {result.returncode})")
        print(f"[Assemble] Video encoded OK: {output_path}")

def get_youtube_service():
    creds = Credentials(
        token=None,
        refresh_token=YOUTUBE_REFRESH_TOKEN,
        client_id=YOUTUBE_CLIENT_ID,
        client_secret=YOUTUBE_CLIENT_SECRET,
        token_uri='https://oauth2.googleapis.com/token',
        scopes=['https://www.googleapis.com/auth/youtube.upload'],
    )
    creds.refresh(Request())
    return build('youtube', 'v3', credentials=creds)

def upload_to_youtube(video_path, title, description, tags):
    youtube = get_youtube_service()
    body = {
        'snippet': {
            'title': title[:100],
            'description': description,
            'tags': tags if isinstance(tags, list) else tags.split(','),
            'categoryId': '28',
            'defaultLanguage': 'en',
        },
        'status': {
            'privacyStatus': 'public',
            'selfDeclaredMadeForKids': False,
            'madeForKids': False,
        },
    }
    media = MediaFileUpload(video_path, mimetype='video/mp4', resumable=True, chunksize=10 * 1024 * 1024)
    insert_request = youtube.videos().insert(
        part=','.join(body.keys()),
        body=body,
        media_body=media,
    )
    response = None
    while response is None:
        status, response = insert_request.next_chunk()
        if status:
            print(f"[YouTube] {int(status.progress() * 100)}% uploaded")
    return response['id']

def build_video_worker(audio_bytes, audio_url, audio_base64, title, description, keywords, tags):
    """Background thread: runs full pipeline after Make.com already got its 200 response."""
    import base64
    print(f"\n[Worker] Starting build: {title}")
    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # 1. Save audio
            audio_path = os.path.join(tmpdir, 'voiceover.mp3')
            if audio_bytes:
                with open(audio_path, 'wb') as f:
                    f.write(audio_bytes)
                print("[Worker 1] Audio saved from upload")
            elif audio_base64:
                b64 = audio_base64.split(',', 1)[1] if ',' in audio_base64 else audio_base64
                with open(audio_path, 'wb') as f:
                    f.write(base64.b64decode(b64))
                print("[Worker 1] Audio decoded from base64")
            else:
                download_file(audio_url, audio_path)
                print("[Worker 1] Audio downloaded from URL")

            # 2. Search Pexels
            print("[Worker 2] Searching Pexels...")
            clips_data = get_pexels_clips(keywords)
            if not clips_data:
                print("[Worker] ERROR: No Pexels clips found")
                return

            # 3. Download clips
            print(f"[Worker 3] Downloading up to 8 clips...")
            clip_paths = []
            for i, clip in enumerate(clips_data[:8]):
                path = os.path.join(tmpdir, f'clip_{i:02d}.mp4')
                try:
                    download_file(clip['url'], path)
                    size = os.path.getsize(path)
                    print(f"[Worker 3] Clip {i} OK ({size//1024}KB)")
                    clip_paths.append(path)
                except Exception as e:
                    print(f"[Worker 3] Clip {i} failed: {e}")

            if not clip_paths:
                print("[Worker] ERROR: All clip downloads failed")
                return

            # 4. Assemble (pure FFMPEG — no MoviePy for video reading)
            output_path = os.path.join(tmpdir, 'final_video.mp4')
            print("[Worker 4] Assembling video with FFMPEG...")
            assemble_video(audio_path, clip_paths, output_path)

            # 5. Upload to YouTube
            print("[Worker 5] Uploading to YouTube...")
            video_id    = upload_to_youtube(output_path, title, description, tags)
            youtube_url = f'https://www.youtube.com/watch?v={video_id}'
            print(f"[Worker] DONE! Published: {youtube_url}")

        except Exception as e:
            import traceback
            print(f"[Worker] ERROR: {e}")
            traceback.print_exc()

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

@app.route('/build-video', methods=['POST'])
def build_video():
    """
    Returns 200 immediately. Video build + YouTube upload runs in background thread.
    Avoids Make.com's 300-second timeout entirely.
    """
    import json as _json

    content_type = request.content_type or ''
    audio_bytes  = None
    audio_url    = None
    audio_base64 = None

    if 'multipart/form-data' in content_type:
        audio_file = request.files.get('audio')
        if audio_file:
            audio_bytes = audio_file.read()
        audio_url    = request.form.get('audio_url')
        audio_base64 = request.form.get('audio_base64')
        title        = request.form.get('title', 'AI Tech Explained')
        description  = request.form.get('description', '')
        raw_kw       = request.form.get('keywords', '')
        raw_tags     = request.form.get('tags', '')
        try:
            keywords = _json.loads(raw_kw) if raw_kw.startswith('[') else [k.strip() for k in raw_kw.split(',') if k.strip()]
        except Exception:
            keywords = [k.strip() for k in raw_kw.split(',') if k.strip()]
        try:
            tags = _json.loads(raw_tags) if raw_tags.startswith('[') else [t.strip() for t in raw_tags.split(',') if t.strip()]
        except Exception:
            tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
    else:
        data = request.get_json(force=True, silent=True)
        if not data:
            return jsonify({'error': 'No JSON body received'}), 400
        audio_url    = data.get('audio_url')
        audio_base64 = data.get('audio_base64')
        keywords     = data.get('keywords', [])
        title        = data.get('title', 'AI Tech Explained')
        description  = data.get('description', '')
        tags         = data.get('tags', [])

    if not audio_bytes and not audio_url and not audio_base64:
        return jsonify({'error': 'Provide audio as multipart file, audio_url, or audio_base64'}), 400

    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',')]
    if not keywords:
        keywords = ['technology', 'artificial intelligence', 'innovation']
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',')]

    print(f"\n[Route] Request received — Title: {title}, Keywords: {keywords}")

    threading.Thread(
        target=build_video_worker,
        args=(audio_bytes, audio_url, audio_base64, title, description, keywords, tags),
        daemon=True,
    ).start()

    return jsonify({
        'status': 'processing',
        'message': 'Video build started — uploading to YouTube in background',
        'title': title,
    }), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
