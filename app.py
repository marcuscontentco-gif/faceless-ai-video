import os
import requests
import tempfile
from flask import Flask, request, jsonify
try:
    from moviepy.editor import VideoFileClip, AudioFileClip, concatenate_videoclips
except ImportError:
    from moviepy import VideoFileClip, AudioFileClip, concatenate_videoclips
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

# ── Environment Variables (set these in Railway dashboard) ────────────────────
PEXELS_API_KEY        = os.environ.get('PEXELS_API_KEY')
YOUTUBE_CLIENT_ID     = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')
# ─────────────────────────────────────────────────────────────────────────────

# Output resolution — 720p keeps quality high while encoding 4-5x faster than 1080p
OUTPUT_WIDTH  = 1280
OUTPUT_HEIGHT = 720

# ── Helpers ───────────────────────────────────────────────────────────────────

def download_file(url, output_path):
    """Download any file from a URL and save it locally."""
    response = requests.get(url, stream=True, timeout=120)
    response.raise_for_status()
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

def get_pexels_clips(keywords):
    """Search Pexels and return a list of video clip metadata.
    
    Uses up to 4 keywords with 5 results each (20 candidates total).
    Prefers clips already at 1280px wide so no rescaling is needed.
    """
    headers = {'Authorization': PEXELS_API_KEY}
    all_clips = []

    for keyword in keywords[:4]:          # 4 keywords = good topic coverage
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
                # Prefer files already at our target width (no resize needed)
                exact = [f for f in video['video_files'] if f.get('width') == OUTPUT_WIDTH]
                hd    = [f for f in video['video_files'] if f.get('width', 0) > OUTPUT_WIDTH]
                sd    = [f for f in video['video_files'] if f.get('width', 0) >= 640]
                files = exact or hd or sd or video['video_files']
                if files:
                    best = sorted(files, key=lambda x: abs(x.get('width', 0) - OUTPUT_WIDTH))[0]
                    all_clips.append({
                        'url':      best['link'],
                        'duration': video['duration'],
                    })
        except Exception as e:
            print(f"[Pexels] Error for '{keyword}': {e}")

    return all_clips

def assemble_video(audio_path, clip_paths, output_path):
    """Stitch stock clips together under the voiceover and export an MP4.
    
    Encodes at 720p with ultrafast preset — still great on YouTube, encodes
    in ~60-90 s instead of 10+ minutes.
    """
    audio    = AudioFileClip(audio_path)
    total_dur = audio.duration
    segments = []
    filled   = 0.0
    idx      = 0

    while filled < total_dur and clip_paths:
        path = clip_paths[idx % len(clip_paths)]
        idx += 1
        try:
            clip      = VideoFileClip(path)
            remaining = total_dur - filled

            if clip.duration > remaining:
                clip = clip.subclip(0, remaining)

            # Standardise to OUTPUT resolution
            if clip.size != [OUTPUT_WIDTH, OUTPUT_HEIGHT]:
                clip = clip.resize((OUTPUT_WIDTH, OUTPUT_HEIGHT))

            segments.append(clip)
            filled += clip.duration
        except Exception as e:
            print(f"[MoviePy] Skipping clip {path}: {e}")

    if not segments:
        raise RuntimeError("Could not load any video segments -- check clip downloads.")

    final = concatenate_videoclips(segments, method='compose')
    final = final.set_audio(audio)

    final.write_videofile(
        output_path,
        codec='libx264',
        audio_codec='aac',
        fps=24,
        preset='ultrafast',        # encodes ~5x faster; YouTube re-encodes anyway
        temp_audiofile=output_path.replace('.mp4', '_tmp.m4a'),
        remove_temp=True,
        verbose=False,
        logger=None,
    )

    for seg in segments:
        seg.close()
    audio.close()
    final.close()

def get_youtube_service():
    """Return an authenticated YouTube API client using the stored refresh token."""
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
    """Upload the finished MP4 to YouTube and return the video ID."""
    youtube = get_youtube_service()

    body = {
        'snippet': {
            'title':           title[:100],
            'description':     description,
            'tags':            tags if isinstance(tags, list) else tags.split(','),
            'categoryId':      '28',   # Science & Technology
            'defaultLanguage': 'en',
        },
        'status': {
            'privacyStatus':            'public',
            'selfDeclaredMadeForKids':  False,
            'madeForKids':              False,
        },
    }

    media = MediaFileUpload(
        video_path,
        mimetype='video/mp4',
        resumable=True,
        chunksize=10 * 1024 * 1024,   # 10 MB chunks
    )

    insert_request = youtube.videos().insert(
        part=','.join(body.keys()),
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status, response = insert_request.next_chunk()
        if status:
            print(f"[YouTube] Upload progress: {int(status.progress() * 100)}%")

    return response['id']

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/health', methods=['GET'])
def health():
    """Quick check that the server is running."""
    return jsonify({'status': 'ok', 'message': 'Video builder is running'})

@app.route('/build-video', methods=['POST'])
def build_video():
    """
    Main endpoint -- called by Make.com mid-workflow.

    Accepts EITHER:
    (A) Multipart/Form Data -- best for ElevenLabs binary output:
        audio       = <binary MP3 file field>
        title       = "Video title"
        keywords    = "keyword1,keyword2,keyword3"  (comma-separated)
        description = "..."
        tags        = "tag1,tag2,tag3"              (comma-separated)

    (B) JSON body with base64 audio:
        { "audio_base64": "<base64 MP3>", "title": "...", "keywords": [...], ... }

    (C) JSON body with audio URL:
        { "audio_url": "<MP3 URL>", "title": "...", ... }

    Returns:
        { "status": "success", "video_id": "...", "youtube_url": "...", "duration": 92.4 }
    """
    import base64, json as _json

    # ── Determine input source ────────────────────────────────────────────────
    content_type  = request.content_type or ''
    audio_file    = None

    if 'multipart/form-data' in content_type:
        audio_file    = request.files.get('audio')
        audio_url     = request.form.get('audio_url')
        audio_base64  = request.form.get('audio_base64')
        title         = request.form.get('title', 'AI Tech Explained')
        description   = request.form.get('description', '')
        raw_kw        = request.form.get('keywords', '')
        raw_tags      = request.form.get('tags', '')
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

    if not audio_file and not audio_url and not audio_base64:
        return jsonify({'error': 'Provide audio as a multipart file, audio_url, or audio_base64'}), 400

    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',')]
    if not keywords:
        keywords = ['technology', 'artificial intelligence', 'innovation']

    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',')]

    print(f"\n[Build] Title:    {title}")
    print(f"[Build] Keywords: {keywords}")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # 1. Save / decode the voiceover
            audio_path = os.path.join(tmpdir, 'voiceover.mp3')
            if audio_file:
                print("[Step 1] Saving uploaded audio file...")
                audio_file.save(audio_path)
            elif audio_base64:
                print("[Step 1] Decoding base64 voiceover...")
                b64 = audio_base64
                if ',' in b64:
                    b64 = b64.split(',', 1)[1]
                with open(audio_path, 'wb') as f:
                    f.write(base64.b64decode(b64))
            else:
                print("[Step 1] Downloading voiceover...")
                download_file(audio_url, audio_path)

            # 2. Measure audio duration
            tmp_audio = AudioFileClip(audio_path)
            duration  = tmp_audio.duration
            tmp_audio.close()
            print(f"[Step 1] Duration: {duration:.1f}s")

            # 3. Fetch Pexels clip metadata
            print("[Step 2] Searching Pexels for stock footage...")
            clips_data = get_pexels_clips(keywords)
            if not clips_data:
                return jsonify({'error': 'No Pexels clips found -- try different keywords'}), 500

            # 4. Download clips (up to 8 — enough variety, faster than 12)
            print(f"[Step 3] Downloading {min(len(clips_data), 8)} clips...")
            clip_paths = []
            for i, clip in enumerate(clips_data[:8]):
                path = os.path.join(tmpdir, f'clip_{i:02d}.mp4')
                try:
                    download_file(clip['url'], path)
                    clip_paths.append(path)
                except Exception as e:
                    print(f"[Step 3] Clip {i} failed: {e}")

            if not clip_paths:
                return jsonify({'error': 'All clip downloads failed'}), 500

            # 5. Assemble the final video
            print("[Step 4] Assembling video (720p ultrafast)...")
            output_path = os.path.join(tmpdir, 'final_video.mp4')
            assemble_video(audio_path, clip_paths, output_path)

            # 6. Upload to YouTube
            print("[Step 5] Uploading to YouTube...")
            video_id    = upload_to_youtube(output_path, title, description, tags)
            youtube_url = f'https://www.youtube.com/watch?v={video_id}'
            print(f"[Done] {youtube_url}")

            return jsonify({
                'status':      'success',
                'video_id':    video_id,
                'youtube_url': youtube_url,
                'title':       title,
                'duration':    round(duration, 1),
            })

        except Exception as e:
            print(f"[Error] {str(e)}")
            return jsonify({'error': str(e)}), 500

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
