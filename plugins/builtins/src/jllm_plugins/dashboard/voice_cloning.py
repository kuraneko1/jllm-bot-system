# ruff: noqa: E501
"""Normalize bounded voice references without changing the original upload."""
import json
import math
import subprocess
import tempfile
from pathlib import Path

MAX_VOICE_UPLOAD_BYTES = 20 * 1024 * 1024
VOICE_EXTENSIONS = {'.wav', '.mp3', '.webm', '.flac', '.ogg', '.m4a', '.opus'}


def normalize_voice(data: bytes, suffix: str) -> tuple[bytes, dict]:
    def run(args):
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired as exc:
            raise ValueError('音声の処理がタイムアウトしました。短い録音でお試しください。') from exc
        if result.returncode:
            raise ValueError('音声ファイルを読み込めません。別の録音を選択してください。')
        return result

    def measure(path):
        result = run(['ffmpeg', '-hide_banner', '-nostats', '-i', str(path), '-vn',
                      '-af', 'aformat=channel_layouts=mono,loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json',
                      '-f', 'null', '-'])
        # ffmpeg versions may append informational lines after loudnorm's JSON.
        return json.JSONDecoder().raw_decode(result.stderr[result.stderr.rfind('{'):])[0]

    with tempfile.TemporaryDirectory(prefix='voice-clone-') as directory:
        root = Path(directory)
        source, decoded, output = root/('upload'+suffix), root/'decoded.wav', root/'reference.wav'
        source.write_bytes(data)
        # Bound decoded size even for compressed formats with missing duration metadata.
        run(['ffmpeg', '-v', 'error', '-i', str(source), '-map', '0:a:0', '-vn',
             '-t', '121', '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(decoded)])
        import wave
        with wave.open(str(decoded)) as audio:
            duration = audio.getnframes()/audio.getframerate()
        if not 1 <= duration <= 120:
            raise ValueError('参照音声は1〜120秒にしてください。')
        gain = -16 - float(measure(decoded)['input_i'])
        if not math.isfinite(gain):
            raise ValueError('声を含む録音を選択してください（無音は登録できません）。')
        for _ in range(10):
            filters = (f'aresample=192000,volume={gain}dB,'
                       'alimiter=limit=0.82224265:level=false:latency=true:attack=5:release=50,aresample=48000')
            run(['ffmpeg', '-v', 'error', '-y', '-i', str(decoded), '-af', filters,
                 '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(output)])
            stats = measure(output)
            loudness, peak = float(stats['input_i']), float(stats['input_tp'])
            if abs(loudness+16) <= .1 and peak <= -1.5:
                return output.read_bytes(), {'lufs': loudness, 'peak_dbtp': peak, 'seconds': round(duration, 2)}
            gain += -16-loudness
        raise ValueError('音量を揃えられませんでした。別の録音を選択してください。')
