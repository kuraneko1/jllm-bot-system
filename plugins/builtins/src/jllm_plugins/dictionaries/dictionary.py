import json
import math
import os
import random
import re
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioProcessingConfig:
    threshold_db: str = "-42dB"
    keep_start_seconds: float = 0.08
    keep_end_seconds: float = 0.12
    loudness_i: str = "-16"
    loudness_tp: str = "-1.5"
    loudness_lra: str = "11"


DEFAULT_AUDIO_CONFIG = AudioProcessingConfig()


SAMPLE_RATE = 48000

_KATAKANA_TO_HIRAGANA = {chr(code): chr(code - 0x60) for code in range(0x30A1, 0x30F7)}


def transcode_to_wav(
    data: bytes,
    suffix: str,
    target: Path,
    config: AudioProcessingConfig | None = None,
) -> bool:
    config = config or DEFAULT_AUDIO_CONFIG
    input_path = None
    trimmed_path = None

    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(data)
            input_path = handle.name

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
            trimmed_path = handle.name

        # 前後の無音だけを落とし、発話は1サンプルも削らない。
        # silenceremove の start_duration は「無音が終わったと判断するまでに
        # 必要な非無音の長さ」で、その長さぶんの音声を**捨ててしまう**。
        # そのため 0 にして、代わりに start_silence（切ったあとに残す無音）で
        # 頭と尻に余白を作る。
        trim_filter = (
            "silenceremove="
            "start_periods=1:"
            "start_duration=0:"
            f"start_threshold={config.threshold_db}:"
            f"start_silence={config.keep_start_seconds}:"
            "detection=rms,"
            "areverse,silenceremove="
            "start_periods=1:"
            "start_duration=0:"
            f"start_threshold={config.threshold_db}:"
            f"start_silence={config.keep_end_seconds}:"
            "detection=rms,"
            "areverse"
        )
        trim_result = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-y",
                "-v",
                "error",
                "-i",
                input_path,
                "-af",
                trim_filter,
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-sample_fmt",
                "s16",
                trimmed_path,
            ],
            capture_output=True,
        )

        loudnorm_filter = (
            "loudnorm="
            f"I={config.loudness_i}:"
            f"TP={config.loudness_tp}:"
            f"LRA={config.loudness_lra}:"
            "print_format=none"
        )

        normalize_input = (
            trimmed_path
            if trim_result.returncode == 0
            and Path(trimmed_path).is_file()
            and Path(trimmed_path).stat().st_size > 128
            else input_path
        )

        def normalize(source_path: str):
            target.unlink(missing_ok=True)
            return subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    source_path,
                    "-af",
                    loudnorm_filter,
                    "-ac",
                    "1",
                    "-ar",
                    str(SAMPLE_RATE),
                    "-sample_fmt",
                    "s16",
                    str(target),
                ],
                capture_output=True,
            )

        normalize_result = normalize(normalize_input)
        valid = (
            normalize_result.returncode == 0 and target.is_file() and target.stat().st_size > 128
        )

        if not valid and normalize_input != input_path:
            normalize_result = normalize(input_path)
            valid = (
                normalize_result.returncode == 0
                and target.is_file()
                and target.stat().st_size > 128
            )

        return valid

    except OSError:
        return False

    finally:
        for path in (input_path, trimmed_path):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _fold_char(char: str) -> str:
    out = []

    for folded in unicodedata.normalize("NFKC", char):
        for item in folded.casefold():
            out.append(_KATAKANA_TO_HIRAGANA.get(item, item))

    return "".join(out)


def normalize(text: str) -> str:
    return "".join(_fold_char(char) for char in text)


def _build_normalized(
    text: str,
) -> tuple[str, list[int], list[int]]:
    chars = []
    starts = []
    ends = []

    for index, char in enumerate(text):
        for folded in _fold_char(char):
            chars.append(folded)
            starts.append(index)
            ends.append(index + 1)

    return "".join(chars), starts, ends


@dataclass(frozen=True)
class Segment:
    kind: str
    value: str
    #: ``kind == "sound"`` のとき、一致した元の語句（wavを鳴らさない場合の読み上げ用）
    text: str = ""


def boundary_pattern(key: str) -> str:
    escaped = re.escape(key)

    if re.fullmatch(r"[a-z0-9_]+", key):
        return r"(?<![a-z0-9_])" + escaped + r"(?![a-z0-9_])"

    return escaped


class SoundDictionary:
    def __init__(
        self,
        data_path: str,
        sounds_dir: str,
        audio_config: AudioProcessingConfig | None = None,
    ):
        self.audio_config = audio_config or DEFAULT_AUDIO_CONFIG
        self.data_path = Path(data_path)
        self.sounds_dir = Path(sounds_dir)
        self.sounds_dir.mkdir(parents=True, exist_ok=True)

        self.enabled = True
        self.entries: dict[str, list[str]] = {}
        self.meta: dict[str, str] = {}
        self.volumes: dict[str, dict[str, float]] = {}
        self._volume_by_file: dict[str, float] = {}

        self._pattern: re.Pattern | None = None
        self._index: list[tuple[str, list[str]]] = []

        self.load()

    @staticmethod
    def _as_list(value: object) -> list[str]:
        if isinstance(value, str):
            return [value]

        if isinstance(value, list):
            return [str(item) for item in value]

        return []

    def load(self):
        self.entries = {}
        self.meta = {}
        self.volumes = {}

        if self.data_path.exists():
            try:
                data = json.loads(self.data_path.read_text("utf-8"))

                if isinstance(data, dict):
                    for key, value in data.items():
                        phrase = str(key)

                        if isinstance(value, dict):
                            files = self._as_list(value.get("files"))
                            author = value.get("by")

                            if author:
                                self.meta[phrase] = str(author)

                            raw_volumes = value.get("volumes")

                            if isinstance(raw_volumes, dict):
                                gains = {}

                                for name, gain in raw_volumes.items():
                                    if (
                                        isinstance(gain, (int, float))
                                        and not isinstance(gain, bool)
                                        and math.isfinite(float(gain))
                                        and 0.0 <= float(gain) <= 3.0
                                        and str(name) in files
                                    ):
                                        gains[str(name)] = float(gain)

                                if gains:
                                    self.volumes[phrase] = gains
                        else:
                            files = self._as_list(value)

                        self.entries[phrase] = files

            except OSError, ValueError:
                self.entries = {}
                self.meta = {}

        self._rebuild()

    def save(self):
        payload = {}

        for phrase, files in self.entries.items():
            author = self.meta.get(phrase)
            gains = {
                name: gain for name, gain in self.volumes.get(phrase, {}).items() if name in files
            }

            if author or gains:
                item: dict[str, object] = {
                    "files": list(files),
                }

                if author:
                    item["by"] = author

                if gains:
                    item["volumes"] = gains

                payload[phrase] = item
            else:
                payload[phrase] = list(files)

        self.data_path.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _rebuild(self):
        pairs = [
            (normalize(phrase), filenames)
            for phrase, filenames in self.entries.items()
            if filenames
        ]
        pairs = [(key, filenames) for key, filenames in pairs if key]
        pairs.sort(key=lambda item: len(item[0]), reverse=True)

        self._index = pairs

        flat: dict[str, float] = {}

        for phrase, filenames in self.entries.items():
            for name in filenames:
                gain = self.volumes.get(phrase, {}).get(name)

                if gain is not None:
                    flat.setdefault(name, gain)

        self._volume_by_file = flat

        alternatives = []

        for key, _ in pairs:
            alternatives.append(boundary_pattern(key))

        if alternatives:
            self._pattern = re.compile("|".join(alternatives))
        else:
            self._pattern = None

    def _find_key(self, phrase: str) -> str | None:
        target = normalize(phrase)

        for key in self.entries:
            if normalize(key) == target:
                return key

        return None

    def add(
        self,
        phrase: str,
        filenames: str | list[str],
        author: str | None = None,
    ):
        values = self._as_list(filenames)

        existing = self._find_key(phrase)

        if existing is not None:
            del self.entries[existing]
            self.meta.pop(existing, None)

        self.entries[phrase] = values

        if author:
            self.meta[phrase] = str(author)

        kept = {name: gain for name, gain in self.volumes.get(phrase, {}).items() if name in values}

        if kept:
            self.volumes[phrase] = kept
        else:
            self.volumes.pop(phrase, None)

        self._rebuild()
        self.save()

    def remove(self, phrase: str) -> bool:
        existing = self._find_key(phrase)

        if existing is None:
            return False

        del self.entries[existing]
        self.meta.pop(existing, None)
        self.volumes.pop(existing, None)
        self._rebuild()
        self.save()
        return True

    def items(self) -> list[tuple[str, list[str]]]:
        return sorted(self.entries.items())

    def author_of(self, phrase: str) -> str | None:
        key = self._find_key(phrase)

        if key is None:
            return None

        return self.meta.get(key)

    def volume_of_file(self, filename: str) -> float:
        """Discord再生時に使うファイル毎のゲイン（未設定は1.0）。"""
        return self._volume_by_file.get(str(filename), 1.0)

    def set_volumes(self, phrase: str, gains: dict[str, float]) -> dict[str, float]:
        """ファイル毎ゲインを検証・マージして保存する。"""
        files = self.entries.get(phrase, [])
        merged = {
            name: gain for name, gain in self.volumes.get(phrase, {}).items() if name in files
        }

        for name, gain in gains.items():
            if name not in files:
                continue

            if gain == 1.0:
                merged.pop(name, None)
            else:
                merged[name] = gain

        if merged:
            self.volumes[phrase] = merged
        else:
            self.volumes.pop(phrase, None)

        self._rebuild()
        self.save()

        return dict(merged)

    def _lookup(self, matched: str) -> list[str] | None:
        for key, filenames in self._index:
            if key == matched:
                return filenames

        return None

    def split(self, text: str) -> list[Segment]:
        if not self.enabled or self._pattern is None:
            return [Segment("text", text)]

        normalized, starts, ends = _build_normalized(text)

        segments: list[Segment] = []
        position = 0

        for match in self._pattern.finditer(normalized):
            start = starts[match.start()]
            end = ends[match.end() - 1]

            if start > position:
                segments.append(Segment("text", text[position:start]))

            filenames = self._lookup(match.group(0))

            if not filenames:
                segments.append(Segment("text", text[start:end]))
            else:
                filename = random.choice(filenames)
                segments.append(
                    Segment(
                        "sound",
                        str(self.sounds_dir / filename),
                        text[start:end],
                    )
                )

            position = end

        if position < len(text):
            segments.append(Segment("text", text[position:]))

        return segments
