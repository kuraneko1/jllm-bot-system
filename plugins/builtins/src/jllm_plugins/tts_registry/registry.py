# ruff: noqa: E501
import os

from jllm_tts import TTSBackendError

from jllm_plugins.tts_irodori.backend import IrodoriBackend
from jllm_plugins.tts_openai.backend import OpenAITTSBackend
from jllm_plugins.tts_speechcpp.backend import SpeechCppBackend
from jllm_plugins.tts_voicevox.backend import VoicevoxBackend

BACKENDS = {
    "voicevox": VoicevoxBackend,
    "irodori": IrodoriBackend,
    "speechcpp": SpeechCppBackend,
    "openai": OpenAITTSBackend,
}
DEFAULT_BACKEND = "voicevox"


def register(name):
    def decorator(cls):
        cls.name = name
        BACKENDS[name] = cls
        return cls

    return decorator


def available():
    return sorted(BACKENDS)


def first_env(env, *keys, default=""):
    for key in keys:
        value = env.get(key)
        if value not in (None, ""):
            return value
    return default


def env_bool(value, default=False):
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_float(value, default):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed == parsed and abs(parsed) != float("inf") else default


def build_backend(name=None, *, env=None):
    env = os.environ if env is None else env
    key = (name or env.get("TTS_BACKEND", DEFAULT_BACKEND)).strip().lower()
    try:
        backend_cls = BACKENDS[key]
    except KeyError:
        raise TTSBackendError(
            f"未知のTTSバックエンド: {key}（利用可能: {', '.join(available())}）"
        ) from None
    if key == "voicevox":
        speaker = first_env(env, "TTS_VOICEVOX_SPEAKER", "VOICEVOX_SPEAKER", default=None)
        return VoicevoxBackend(
            first_env(env, "TTS_VOICEVOX_URL", "VOICEVOX_URL", default="http://127.0.0.1:50021"),
            timeout=env_float(first_env(env, "TTS_VOICEVOX_TIMEOUT", "VOICEVOX_TIMEOUT", default=90), 90),
            default_speaker=int(speaker) if speaker is not None else None,
        )
    if key == "irodori":
        return IrodoriBackend(
            first_env(env, "TTS_IRODORI_URL", "IRODORI_URL", default="http://127.0.0.1:8088"),
            default_voice=first_env(env, "TTS_IRODORI_DEFAULT_VOICE", "IRODORI_DEFAULT_VOICE", default="none"),
            api_key=first_env(env, "TTS_IRODORI_API_KEY", "IRODORI_API_KEY"),
            model_name=first_env(env, "TTS_IRODORI_MODEL", "IRODORI_MODEL", default="irodori-tts"),
            voices_cache=first_env(env, "TTS_IRODORI_VOICES_CACHE", "IRODORI_VOICES_CACHE"),
            disabled_voices_path=first_env(env, "TTS_DISABLED_VOICES_PATH", "DISABLED_VOICES_PATH"),
            streaming=env_bool(first_env(env, "TTS_IRODORI_STREAMING", "IRODORI_STREAMING", default=True), True),
            timeout=env_float(first_env(env, "TTS_IRODORI_TIMEOUT", "IRODORI_TIMEOUT", default=120), 120),
        )
    if key == "speechcpp":
        return SpeechCppBackend(
            first_env(env, "TTS_SPEECHCPP_URL", "SPEECHCPP_URL", default="http://127.0.0.1:8089"),
            voices_dir=first_env(env, "TTS_SPEECHCPP_VOICES_DIR", "SPEECHCPP_VOICES_DIR"),
            default_voice=first_env(env, "TTS_SPEECHCPP_VOICE", "SPEECHCPP_VOICE", default="none"),
            model_name=first_env(env, "SPEECHCPP_MODEL"),
            timeout=env_float(first_env(env, "SPEECHCPP_TIMEOUT", default=120), 120),
        )
    if key == "openai":
        seed = first_env(env, "TTS_OPENAI_SEED", default=None)
        return OpenAITTSBackend(
            first_env(env, "TTS_OPENAI_URL", default="http://127.0.0.1:8090"),
            default_voice=first_env(env, "TTS_OPENAI_DEFAULT_VOICE", "TTS_OPENAI_VOICE"),
            voices=first_env(env, "TTS_OPENAI_VOICES"),
            voices_path=first_env(env, "TTS_OPENAI_VOICES_PATH", default="/v1/audio/voices"),
            health_path=first_env(env, "TTS_OPENAI_HEALTH_PATH", default="/health"),
            model_name=first_env(env, "TTS_OPENAI_MODEL"),
            speed_mode=first_env(env, "TTS_OPENAI_SPEED_MODE", default="atempo"),
            default_speed=env_float(first_env(env, "TTS_OPENAI_DEFAULT_SPEED", default=1.0), 1.0),
            ignore_speaker_speed=env_bool(first_env(env, "TTS_OPENAI_IGNORE_SPEAKER_SPEED", default=False)),
            seed=int(seed) if seed is not None else None,
            silence_retries=int(env_float(first_env(env, "TTS_OPENAI_SILENCE_RETRIES", default=1), 1)),
            silence_rms_dbfs=env_float(first_env(env, "TTS_OPENAI_SILENCE_RMS_DBFS", default=-45), -45),
            timeout=env_float(first_env(env, "TTS_OPENAI_TIMEOUT", default=120), 120),
            streaming=env_bool(first_env(env, "TTS_OPENAI_STREAMING", default=True), True),
        )
    return backend_cls.from_env(env)
