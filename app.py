import os
import json
import subprocess
import requests
import tempfile
import threading

# ── CRITICAL: set FFMPEG_BINARY env var BEFORE importing moviepy ─────────────
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
    """Return path to the imageio-ffmpeg binary."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def download_file(url, output_path):
    response = requests.get(url, stream=True, timeout=120)
    response.raise_for_status()
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)


def get_pexels_clips(keywords):
    """Fetch a pool of clips using multiple keywords (flat mode fallback)."""
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


def get_pexels_clip_for_scene(keywords):
    """
    Fetch the single best Pexels clip for a scene.
    Tries each keyword in order, returns first match.
    """
    headers = {'Authorization': PEXELS_API_KEY}
    for keyword in keywords:
        keyword = keyword.strip()
        if not keyword:
            continue
        url = (
            f'https://api.pexels.com/videos/search'
            f'?query={keyword}&per_page=3&orientation=landscape&size=medium'
        )
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            data = resp.json()
            videos = data.get('videos', [])
            if videos:
                video = videos[0]
                exact = [f for f in video['video_files'] if f.get('width') == OUTPUT_WIDTH]
                hd    = [f for f in video['video_files'] if f.get('width', 0) > OUTPUT_WIDTH]
                sd    = [f for f in video['video_files'] if f.get('width', 0) >= 640]
                files = exact or hd or sd or video['video_files']
                if files:
                    best = sorted(files, key=lambda x: abs(x.get('width', 0) - OUTPUT_WIDTH))[0]
                    return {'url': best['link'], 'duration': video['duration']}
        except Exception as e:
            print(f"[Pexels] Error for '{keyword}': {e}")
    return None


def get_media_duration(path):
    """Get duration in seconds of any audio or video file using ffmpeg."""
    ffmpeg = get_ffmpeg()
    result = subprocess.run(
        [ffmpeg, '-i', path],
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


def normalize_clip(ffmpeg, clip_path, out_path, target_duration=None):
    """
    Re-encode a clip to H.264 1280x720 30fps.
    If target_duration is set, trims or pads to exactly that length.
    Returns True on success.
    """
    vf = (
        f'scale={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}'
        f':force_original_aspect_ratio=decrease,'
        f'pad={OUTPUT_WIDTH}:{OUTPUT_HEIGHT}:'
        f'(ow-iw)/2:(oh-ih)/2:black,'
        f'fps=30,setsar=1'
    )
    cmd = [ffmpeg, '-y', '-i', clip_path]
    if target_duration:
        cmd += ['-t', str(target_duration)]
    cmd += [
        '-vf', vf,
        '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
        '-an',
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    ok = result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000
    if not ok and result.stderr:
        print(f"[Normalize] stderr: {result.stderr[-400:]}")
    return ok


def assemble_video_scenes(audio_path, scene_clip_paths, scene_durations, output_path):
    """
    Scene-matched assembly:
      - Each clip is trimmed to its target duration (proportional to word count).
      - Clips are concatenated in scene order.
      - Full voiceover is muxed on top.

    scene_clip_paths  : list of raw downloaded clip paths
    scene_durations   : list of target durations (seconds) per scene
    """
    ffmpeg = get_ffmpeg()
    audio_duration = get_media_duration(audio_path)
    print(f"[Assemble] Scene mode — {len(scene_clip_paths)} scenes, audio {audio_duration:.1f}s")

    with tempfile.TemporaryDirectory() as work_dir:

        # ── Step 1: normalize + trim each scene clip ─────────────────────────
        normalized_paths = []
        for i, (clip_path, target_dur) in enumerate(zip(scene_clip_paths, scene_durations)):
            norm_path = os.path.join(work_dir, f'scene_{i:02d}.mp4')
            # Give 10% extra length so -shortest has room; audio is the true master
            if normalize_clip(ffmpeg, clip_path, norm_path, target_duration=target_dur * 1.1):
                normalized_paths.append(norm_path)
                print(f"[Assemble] Scene {i} OK ({target_dur:.1f}s target)")
            else:
                print(f"[Assemble] Scene {i} failed — trying without duration limit")
                # Fallback: normalize without duration constraint, will loop later
                norm_path2 = os.path.join(work_dir, f'scene_{i:02d}_full.mp4')
                if normalize_clip(ffmpeg, clip_path, norm_path2):
                    normalized_paths.append(norm_path2)
                    print(f"[Assemble] Scene {i} fallback OK")

        if not normalized_paths:
            raise RuntimeError("All scene clips failed to normalize")

        # ── Step 2: build concat list ─────────────────────────────────────────
        # If we have fewer normalized clips than scenes (some failed), loop them
        concat_file = os.path.join(work_dir, 'concat.txt')
        lines = []
        total = 0.0
        idx   = 0
        while total < audio_duration + 3:
            p = normalized_paths[idx % len(normalized_paths)]
            d = get_media_duration(p)
            lines.append(f"file '{p}'")
            total += d
            idx   += 1
            if idx > 200:
                break
        with open(concat_file, 'w') as f:
            f.write('\n'.join(lines))
        print(f"[Assemble] Concat: {idx} entries covering {total:.1f}s")

        # ── Step 3: concat into silent video ─────────────────────────────────
        silent_video = os.path.join(work_dir, 'silent.mp4')
        result = subprocess.run(
            [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', concat_file,
             '-c:v', 'copy', silent_video],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[Assemble] Concat stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"FFMPEG concat failed (code {result.returncode})")
        print("[Assemble] Silent video assembled OK")

        # ── Step 4: mux voiceover + trim to audio length ─────────────────────
        result = subprocess.run(
            [ffmpeg, '-y',
             '-i', silent_video, '-i', audio_path,
             '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
             '-map', '0:v:0', '-map', '1:a:0',
             '-shortest',
             output_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[Assemble] Mux stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"FFMPEG mux failed (code {result.returncode})")
        print(f"[Assemble] Video encoded OK: {output_path}")


def assemble_video(audio_path, clip_paths, output_path):
    """
    Fallback flat assembly (no scenes):
    Normalize + loop clips to cover voiceover length.
    """
    ffmpeg   = get_ffmpeg()
    duration = get_media_duration(audio_path)
    print(f"[Assemble] Flat mode — {len(clip_paths)} clips, audio {duration:.1f}s")

    with tempfile.TemporaryDirectory() as work_dir:
        normalized_paths    = []
        normalized_durations = []

        for i, clip_path in enumerate(clip_paths):
            norm_path = os.path.join(work_dir, f'norm_{i:02d}.mp4')
            if normalize_clip(ffmpeg, clip_path, norm_path):
                clip_dur = get_media_duration(norm_path)
                normalized_paths.append(norm_path)
                normalized_durations.append(clip_dur)
                print(f"[Assemble] Clip {i} normalized OK ({clip_dur:.1f}s)")
            else:
                print(f"[Assemble] Clip {i} normalization failed — skipping")

        if not normalized_paths:
            raise RuntimeError("All clips failed to normalize — check Pexels downloads")

        concat_file = os.path.join(work_dir, 'concat.txt')
        lines = []
        total = 0.0
        idx   = 0
        while total < duration + 5:
            p = normalized_paths[idx % len(normalized_paths)]
            d = normalized_durations[idx % len(normalized_paths)]
            lines.append(f"file '{p}'")
            total += d
            idx   += 1
            if idx > 500:
                break
        with open(concat_file, 'w') as f:
            f.write('\n'.join(lines))
        print(f"[Assemble] Concat list: {idx} entries covering {total:.1f}s")

        silent_video = os.path.join(work_dir, 'silent.mp4')
        result = subprocess.run(
            [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', concat_file,
             '-c:v', 'copy', silent_video],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[Assemble] Concat stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"FFMPEG concat failed (code {result.returncode})")
        print("[Assemble] Silent video assembled OK")

        result = subprocess.run(
            [ffmpeg, '-y',
             '-i', silent_video, '-i', audio_path,
             '-c:v', 'copy', '-c:a', 'aac', '-b:a', '128k',
             '-map', '0:v:0', '-map', '1:a:0', '-shortest',
             output_path],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"[Assemble] Mux stderr: {result.stderr[-2000:]}")
            raise RuntimeError(f"FFMPEG mux failed (code {result.returncode})")
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
            'title':           title[:100],
            'description':     description,
            'tags':            tags if isinstance(tags, list) else tags.split(','),
            'categoryId':      '28',
            'defaultLanguage': 'en',
        },
        'status': {
            'privacyStatus':           'public',
            'selfDeclaredMadeForKids': False,
            'madeForKids':             False,
        },
    }
    media = MediaFileUpload(
        video_path, mimetype='video/mp4', resumable=True, chunksize=10 * 1024 * 1024
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
            print(f"[YouTube] {int(status.progress() * 100)}% uploaded")
    return response['id']


def build_video_worker(audio_bytes, audio_url, audio_base64, title, description,
                       keywords, tags, scenes=None):
    """
    Background thread: full pipeline.
    If `scenes` is provided (list of {narration, keywords}), uses scene-matched assembly
    where each clip is timed to its narration segment.
    Otherwise falls back to flat keyword-loop assembly.
    """
    import base64
    print(f"\n[Worker] Starting build: {title}")
    print(f"[Worker] Scenes provided: {len(scenes) if scenes else 0}")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            # ── 1. Save audio ─────────────────────────────────────────────────
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

            audio_duration = get_media_duration(audio_path)
            print(f"[Worker 1] Audio duration: {audio_duration:.1f}s")

            output_path = os.path.join(tmpdir, 'final_video.mp4')

            # ── 2. Scene-matched assembly ──────────────────────────────────────
            if scenes and len(scenes) > 0:
                print(f"[Worker 2] Scene mode: {len(scenes)} scenes")

                # Calculate proportional duration per scene based on word count
                word_counts = [max(1, len(s.get('narration', '').split())) for s in scenes]
                total_words = sum(word_counts)
                scene_durations = [
                    max(3.0, (wc / total_words) * audio_duration)
                    for wc in word_counts
                ]
                print(f"[Worker 2] Scene durations: {[f'{d:.1f}s' for d in scene_durations]}")

                # Fetch one clip per scene
                scene_clip_paths = []
                valid_durations  = []
                for i, (scene, target_dur) in enumerate(zip(scenes, scene_durations)):
                    kws = scene.get('keywords', keywords[:2])
                    if isinstance(kws, str):
                        kws = [k.strip() for k in kws.split(',')]
                    print(f"[Worker 2] Scene {i} keywords: {kws}")
                    clip_info = get_pexels_clip_for_scene(kws)
                    if clip_info:
                        path = os.path.join(tmpdir, f'scene_{i:02d}.mp4')
                        try:
                            download_file(clip_info['url'], path)
                            size = os.path.getsize(path)
                            print(f"[Worker 2] Scene {i} clip OK ({size // 1024}KB)")
                            scene_clip_paths.append(path)
                            valid_durations.append(target_dur)
                        except Exception as e:
                            print(f"[Worker 2] Scene {i} download failed: {e}")
                    else:
                        print(f"[Worker 2] Scene {i} — no Pexels clip found for {kws}")

                if scene_clip_paths:
                    print(f"[Worker 3] Assembling {len(scene_clip_paths)} scene clips...")
                    assemble_video_scenes(audio_path, scene_clip_paths, valid_durations, output_path)
                else:
                    print("[Worker] No scene clips downloaded — falling back to flat mode")
                    scenes = None  # trigger fallback below

            # ── 3. Flat fallback ──────────────────────────────────────────────
            if not scenes:
                print("[Worker 2] Flat mode: searching Pexels by keywords...")
                clips_data = get_pexels_clips(keywords)
                if not clips_data:
                    print("[Worker] ERROR: No Pexels clips found")
                    return

                print(f"[Worker 3] Downloading up to 8 clips...")
                clip_paths = []
                for i, clip in enumerate(clips_data[:8]):
                    path = os.path.join(tmpdir, f'clip_{i:02d}.mp4')
                    try:
                        download_file(clip['url'], path)
                        size = os.path.getsize(path)
                        print(f"[Worker 3] Clip {i} OK ({size // 1024}KB)")
                        clip_paths.append(path)
                    except Exception as e:
                        print(f"[Worker 3] Clip {i} failed: {e}")

                if not clip_paths:
                    print("[Worker] ERROR: All clip downloads failed")
                    return

                print("[Worker 4] Assembling video (flat mode)...")
                assemble_video(audio_path, clip_paths, output_path)

            # ── 4. Upload to YouTube ──────────────────────────────────────────
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
    content_type = request.content_type or ''
    audio_bytes  = None
    audio_url    = None
    audio_base64 = None
    scenes       = None

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
        raw_scenes   = request.form.get('scenes', '')

        # ── claude_json: raw Claude response string — parse everything from it ──
        # Make.com can't serialize arrays in form fields, so we pass the full
        # raw JSON string from Claude and extract title/scenes/tags server-side.
        claude_json_raw = request.form.get('claude_json', '')
        if claude_json_raw:
            try:
                # Strip any preamble/postamble text around the JSON object
                start = claude_json_raw.find('{')
                end   = claude_json_raw.rfind('}') + 1
                if start != -1 and end > start:
                    claude_data = json.loads(claude_json_raw[start:end])
                    title    = claude_data.get('title', title)
                    if claude_data.get('tags'):
                        raw_tags = json.dumps(claude_data['tags'])
                    if claude_data.get('scenes'):
                        raw_scenes = json.dumps(claude_data['scenes'])
                    print(f"[Route] claude_json parsed OK — title={title[:60]}, "
                          f"scenes={len(claude_data.get('scenes', []))}, "
                          f"tags={len(claude_data.get('tags', []))}")
                else:
                    print(f"[Route] claude_json: no JSON object found")
            except Exception as e:
                print(f"[Route] claude_json parse failed: {e}")

        try:
            keywords = json.loads(raw_kw) if raw_kw.startswith('[') else [k.strip() for k in raw_kw.split(',') if k.strip()]
        except Exception:
            keywords = [k.strip() for k in raw_kw.split(',') if k.strip()]
        try:
            tags = json.loads(raw_tags) if raw_tags.startswith('[') else [t.strip() for t in raw_tags.split(',') if t.strip()]
        except Exception:
            tags = [t.strip() for t in raw_tags.split(',') if t.strip()]
        try:
            if raw_scenes:
                scenes = json.loads(raw_scenes) if isinstance(raw_scenes, str) else raw_scenes
        except Exception as e:
            print(f"[Route] scenes parse failed: {e} — raw: {raw_scenes[:200]}")
            scenes = None
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
        raw_scenes   = data.get('scenes', None)
        # scenes may arrive as a list or a JSON string
        if isinstance(raw_scenes, list):
            scenes = raw_scenes
        elif isinstance(raw_scenes, str) and raw_scenes.strip():
            try:
                scenes = json.loads(raw_scenes)
            except Exception:
                scenes = None

    if not audio_bytes and not audio_url and not audio_base64:
        return jsonify({'error': 'Provide audio as multipart file, audio_url, or audio_base64'}), 400

    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',')]
    if not keywords:
        keywords = ['technology', 'artificial intelligence', 'innovation']
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',')]

    print(f"\n[Route] Request received — Title: {title}")
    print(f"[Route] Keywords: {keywords}")
    print(f"[Route] Scenes: {len(scenes) if scenes else 0}")

    threading.Thread(
        target=build_video_worker,
        args=(audio_bytes, audio_url, audio_base64, title, description, keywords, tags, scenes),
        daemon=True,
    ).start()

    return jsonify({
        'status':  'processing',
        'message': 'Video build started — uploading to YouTube in background',
        'title':   title,
    }), 200


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
