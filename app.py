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

PEXELS_API_KEY        = os.environ.get('PEXELS_API_KEY')
YOUTUBE_CLIENT_ID     = os.environ.get('YOUTUBE_CLIENT_ID')
YOUTUBE_CLIENT_SECRET = os.environ.get('YOUTUBE_CLIENT_SECRET')
YOUTUBE_REFRESH_TOKEN = os.environ.get('YOUTUBE_REFRESH_TOKEN')


def download_file(url, output_path):
    response = requests.get(url, stream=True, timeout=120)
    response.raise_for_status()
    with open(output_path, 'wb') as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)


def get_pexels_clips(keywords):
    headers = {'Authorization': PEXELS_API_KEY}
    all_clips = []
    for keyword in keywords[:6]:
        keyword = keyword.strip()
        if not keyword:
            continue
        url = f'https://api.pexels.com/videos/search?query={keyword}&per_page=8&orientation=landscape&size=medium'
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            data = resp.json()
            for video in data.get('videos', []):
                files = [f for f in video['video_files'] if f.get('width', 0) >= 1280]
                if not files:
                    files = video['video_files']
                if files:
                    best = sorted(files, key=lambda x: x.get('width', 0), reverse=True)[0]
                    all_clips.append({'url': best['link'], 'duration': video['duration']})
        except Exception as e:
            print(f"[Pexels] Error for '{keyword}': {e}")
    return all_clips


def assemble_video(audio_path, clip_paths, output_path):
    audio = AudioFileClip(audio_path)
    total_dur = audio.duration
    segments = []
    filled = 0.0
    idx = 0
    while filled < total_dur and clip_paths:
        path = clip_paths[idx % len(clip_paths)]
        idx += 1
        try:
            clip = VideoFileClip(path)
            remaining = total_dur - filled
            if clip.duration > remaining:
                clip = clip.subclip(0, remaining)
            if clip.size != [1920, 1080]:
                clip = clip.resize((1920, 1080))
            segments.append(clip)
            filled += clip.duration
        except Exception as e:
            print(f"[MoviePy] Skipping clip {path}: {e}")
    if not segments:
        raise RuntimeError("Could not load any video segments.")
    final = concatenate_videoclips(segments, method='compose')
    final = final.set_audio(audio)
    final.write_videofile(output_path, codec='libx264', audio_codec='aac', fps=24, preset='fast',
        temp_audiofile=output_path.replace('.mp4', '_tmp.m4a'), remove_temp=True, verbose=False, logger=None)
    for seg in segments:
        seg.close()
    audio.close()
    final.close()


def get_youtube_service():
    creds = Credentials(token=None, refresh_token=YOUTUBE_REFRESH_TOKEN,
        client_id=YOUTUBE_CLIENT_ID, client_secret=YOUTUBE_CLIENT_SECRET,
        token_uri='https://oauth2.googleapis.com/token',
        scopes=['https://www.googleapis.com/auth/youtube.upload'])
    creds.refresh(Request())
    return build('youtube', 'v3', credentials=creds)


def upload_to_youtube(video_path, title, description, tags):
    youtube = get_youtube_service()
    body = {
        'snippet': {'title': title[:100], 'description': description,
            'tags': tags if isinstance(tags, list) else tags.split(','),
            'categoryId': '28', 'defaultLanguage': 'en'},
        'status': {'privacyStatus': 'public', 'selfDeclaredMadeForKids': False, 'madeForKids': False},
    }
    media = MediaFileUpload(video_path, mimetype='video/mp4', resumable=True, chunksize=10*1024*1024)
    insert_request = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
    response = None
    while response is None:
        status, response = insert_request.next_chunk()
        if status:
            print(f"[YouTube] Upload progress: {int(status.progress() * 100)}%")
    return response['id']


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'message': 'Video builder is running'})


@app.route('/build-video', methods=['POST'])
def build_video():
    import base64
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({'error': 'No JSON body received'}), 400

    audio_url    = data.get('audio_url')
    audio_base64 = data.get('audio_base64')
    keywords     = data.get('keywords', [])
    title        = data.get('title', 'AI Tech Explained')
    description  = data.get('description', '')
    tags         = data.get('tags', [])

    if not audio_url and not audio_base64:
        return jsonify({'error': 'audio_url or audio_base64 is required'}), 400

    if isinstance(keywords, str):
        keywords = [k.strip() for k in keywords.split(',')]
    if not keywords:
        keywords = ['technology', 'artificial intelligence', 'innovation']

    print(f"\n[Build] Title: {title}")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            audio_path = os.path.join(tmpdir, 'voiceover.mp3')
            if audio_base64:
                print("[Step 1] Decoding base64 voiceover...")
                b64 = audio_base64
                if ',' in b64:
                    b64 = b64.split(',', 1)[1]
                audio_bytes = base64.b64decode(b64)
                with open(audio_path, 'wb') as f:
                    f.write(audio_bytes)
            else:
                print("[Step 1] Downloading voiceover...")
                download_file(audio_url, audio_path)

            tmp_audio = AudioFileClip(audio_path)
            duration = tmp_audio.duration
            tmp_audio.close()

            clips_data = get_pexels_clips(keywords)
            if not clips_data:
                return jsonify({'error': 'No Pexels clips found'}), 500

            clip_paths = []
            for i, clip in enumerate(clips_data[:12]):
                path = os.path.join(tmpdir, f'clip_{i:02d}.mp4')
                try:
                    download_file(clip['url'], path)
                    clip_paths.append(path)
                except Exception as e:
                    print(f"[Step 3] Clip {i} failed: {e}")

            if not clip_paths:
                return jsonify({'error': 'All clip downloads failed'}), 500

            output_path = os.path.join(tmpdir, 'final_video.mp4')
            assemble_video(audio_path, clip_paths, output_path)

            video_id = upload_to_youtube(output_path, title, description, tags)
            youtube_url = f'https://www.youtube.com/watch?v={video_id}'
            print(f"[Done] {youtube_url}")

            return jsonify({'status': 'success', 'video_id': video_id,
                'youtube_url': youtube_url, 'title': title, 'duration': round(duration, 1)})

        except Exception as e:
            print(f"[Error] {str(e)}")
            return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
