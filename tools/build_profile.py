#!/usr/bin/env python3
"""旧環境の設定から新しい profile を作る補助ツール。

値はコピーするが、秘密（トークン・API キー）は profile に書かず、環境変数名と
鍵ファイルのパスだけを残す。出力は自分だけが読める権限で書き、標準出力には
値ではなく対応件数だけを出す。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from dotenv import dotenv_values

KIND_PROVIDER = {
    "voicevox": "provider.tts.voicevox",
    "speechcpp": "provider.tts.speechcpp",
    "openai": "provider.tts.openai",
    "irodori": "provider.tts.irodori",
}
BACKENDS = {"local", "sarashina", "atmos"}
TRUTHY = {"1", "true", "yes", "on"}


def text(env, key, default=""):
    value = env.get(key)
    return default if value is None else str(value).strip()


def number(env, key, default):
    try:
        return float(text(env, key) or default)
    except ValueError:
        return default


def integer(env, key, default):
    raw = text(env, key)

    if not raw:
        return default

    try:
        # Discord の ID は 18 桁ある。float を経由すると末尾が変わる。
        return int(raw)
    except ValueError:
        try:
            return int(float(raw))
        except ValueError:
            return default


def flag(env, key, default=False):
    value = text(env, key)
    return default if not value else value.casefold() in TRUTHY


def items(env, key, cast=str):
    return [cast(part.strip()) for part in text(env, key).split(",") if part.strip()]


def safe_name(name):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")
    return cleaned or "backend"


def backend_instances(backends, env, data_dir):
    """旧 tts-backends.json を provider の instances と ID 対応へ移す。"""

    providers = {}
    ids = {}
    displays = {}
    order = []
    dropped = []

    for spec in backends.get("backends", []):
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("kind") or spec.get("backend") or "").strip().lower()
        ident = str(spec.get("id") or kind).strip()
        provider = KIND_PROVIDER.get(kind)
        if provider is None or not ident:
            dropped.append(ident or "(no id)")
            continue

        name = safe_name(ident)
        full = f"{provider}/{name}"
        options = {key: value for key, value in spec.items()
                   if key not in {"id", "kind", "backend", "display", "env"}}
        config = {}

        if kind == "voicevox":
            if options.get("url"):
                config["url"] = str(options["url"])
            if options.get("voice") not in (None, ""):
                config["default_speaker"] = int(options["voice"])
        elif kind == "speechcpp":
            for key in ("url", "voices_dir", "voice", "model"):
                if options.get(key) not in (None, ""):
                    config[key] = str(options[key])
            if options.get("timeout"):
                config["timeout"] = float(options["timeout"])
        elif kind == "irodori":
            config["url"] = str(options.get("url") or env.get("IRODORI_URL") or "http://127.0.0.1:8088")
            config["voice"] = str(options.get("voice") or env.get("IRODORI_VOICE") or "none")
            config["data_dir"] = str(data_dir)
            config["api_key_env"] = "IRODORI_API_KEY"
            if env.get("IRODORI_CAPTION"):
                config["caption"] = text(env, "IRODORI_CAPTION")
            if env.get("IRODORI_MODEL"):
                config["model"] = text(env, "IRODORI_MODEL")
            order_names = items(env, "IRODORI_VOICE_ORDER")
            if order_names:
                config["voice_order"] = order_names
        elif kind == "openai":
            for key in ("url", "voice", "model", "speed_mode"):
                if options.get(key) not in (None, ""):
                    config[key] = str(options[key])
            if options.get("default_speed") is not None:
                config["default_speed"] = float(options["default_speed"])
            if "ignore_speaker_speed" in options:
                config["ignore_speaker_speed"] = bool(options["ignore_speaker_speed"])
            if options.get("seed") is not None:
                config["seed"] = int(options["seed"])
            if options.get("timeout"):
                config["timeout"] = float(options["timeout"])

        providers.setdefault(provider, {})[name] = config
        ids[full] = ident
        display = str(spec.get("display") or "").strip()
        if display:
            displays[full] = display
        order.append(full)

    return providers, ids, displays, order, dropped


def build(env, backends, data_dir):
    def path(name):
        return str(data_dir / name)

    def effects_ir():
        """残響 IR は旧実装ではコード側にあり、data の外にあることが多い。"""

        for candidate in (data_dir / "ir" / "room.wav",
                          data_dir.parent / "services" / "ir" / "room.wav"):
            if candidate.is_file():
                return str(candidate)
        return path("ir/room.wav")

    whitelist = items(env, "MESSAGE_CHANNEL_WHITELIST", int)
    owner = integer(env, "OWNER_ID", 0)
    approver = integer(env, "MEDIA_APPROVER_ID", 0)
    switch_users = items(env, "TTS_SWITCH_USER_IDS", int)
    urls = [u for u in items(env, "READINGS_DASHBOARD_URL") if u]
    public_url = ",".join(urls) if urls else "http://127.0.0.1:8765"
    if urls:
        parsed = urlsplit(urls[0])
        front_url = f"{parsed.scheme}://{parsed.netloc}"
    else:
        front_url = public_url
    talk_model = text(env, "TALK_MODEL") or text(env, "SARASHINA_MODEL")
    kitasan_chars = max(20, integer(env, "KITASAN_ITEM_CHARS", 300))
    kitasan_max = max(100, integer(env, "KITASAN_MAX_CHARS", 4000))

    providers, ids, displays, order, dropped = backend_instances(backends, env, data_dir)

    profile = {"profile": "production", "plugins": {}}
    plugins = profile["plugins"]

    plugins["storage.local"] = {"enabled": True, "config": {"data_dir": str(data_dir)}}
    plugins["policy.restrictions"] = {
        "enabled": True, "config": {"path": path("user_restrictions.json")}
    }
    plugins["adapter.discord"] = {
        "enabled": True,
        "critical": True,
        "config": {"token_env": "DISCORD_TOKEN"},
    }

    for provider, instances in providers.items():
        plugins[provider] = {"enabled": True, "instances": instances}

    plugins["tts.router"] = {
        "enabled": True,
        "critical": True,
        "config": {
            "state_file": path("tts_state.json"),
            "require_existing_state": True,
            "failover": flag(env, "TTS_FAILOVER", True),
            "health_seconds": number(env, "TTS_HEALTH_SECONDS", 30),
            "backend_ids": ids,
            "backend_displays": displays,
        },
    }

    playback = {
        "speaker_id": integer(env, "DEFAULT_SPEAKER", 1),
        "speed": number(env, "DEFAULT_SPEED", 1.2),
        "volume": number(env, "DEFAULT_VOLUME", 0.2),
        "sample_volume": number(env, "SAMPLE_VOLUME", 0.2),
        "allow_nonverbal_emojis": flag(env, "ALLOW_NONVERBAL_EMOJIS", False),
        "ir_path": effects_ir(),
        "duck_enabled": flag(env, "DUCK_ENABLED", False),
        "duck_curve_path": path("duck_curve.json"),
        "duck_level": number(env, "DUCK_LEVEL", 0.2),
        "duck_hold_seconds": number(env, "DUCK_HOLD_SECONDS", 0.8),
        "duck_tick_seconds": number(env, "DUCK_TICK_SECONDS", 0.04),
        "duck_trigger_seconds": number(env, "DUCK_TRIGGER_SECONDS", 1.0),
        "duck_debug": flag(env, "DUCK_DEBUG", False),
    }

    plugins["dictionary.sound"] = {
        "enabled": True,
        "config": {"data_file": path("sounds.json"), "sounds_dir": path("sounds")},
    }
    plugins["dictionary.reading"] = {
        "enabled": True, "config": {"data_file": path("readings.json")}
    }
    plugins["speech.effects"] = {"enabled": True, "config": {"ir_path": effects_ir()}}
    plugins["speech.playback"] = {"enabled": True, "config": playback}
    plugins["speech.text"] = {"enabled": True}
    plugins["voice.settings"] = {
        "enabled": True,
        "config": {
            "speeds_path": path("voice_speeds.json"),
            "volumes_path": path("voice_volumes.json"),
            "lengths_path": path("voice_lengths.json"),
            "user_voices_path": path("user_voices.json"),
            "forced_voices_path": path("forced_voices.json"),
            "announcement_history_path": path("vc_announcements.json"),
            "announcement_cache_dir": path("join_voice"),
            "announcement_voice": text(env, "VC_ANNOUNCEMENT_VOICE")
            or "clone こしあん",
            "announcement_cache_limit": max(10, integer(env, "VC_ANNOUNCEMENT_CACHE_LIMIT", 128)),
        },
    }
    plugins["feature.bookmarks"] = {
        "enabled": True, "config": {"data_file": path("bookmarks.json")}
    }
    plugins["dictionary.media"] = {
        "enabled": True,
        "config": {
            "data_file": path("media.json"),
            "media_dir": path("media"),
            "approver_id": approver,
            "owner_id": owner,
        },
    }
    plugins["feature.html-library"] = {
        "enabled": True,
        "config": {
            "html_dir": path("html"),
            "pending_dir": path("html-pending"),
            "catalog_file": path("html_catalog.json"),
            "viewer_base_url": text(env, "HTML_VIEWER_BASE_URL")
            or "http://127.0.0.1:8030",
            "dashboard_url": front_url,
            "max_bytes": integer(env, "HTML_MAX_BYTES", 52428800),
            "require_approval": flag(env, "HTML_REQUIRE_APPROVAL", True),
            "approver_id": approver,
        },
    }
    plugins["tts.lifecycle"] = {"enabled": True}

    if text(env, "TTS_LIFECYCLE_TARGETS"):
        plugins["tts.lifecycle"]["config"] = {
            "targets": json.loads(text(env, "TTS_LIFECYCLE_TARGETS")),
            "local_only": items(env, "TTS_LIFECYCLE_LOCAL_ONLY"),
            "unmanaged_hosts": items(env, "TTS_LIFECYCLE_UNMANAGED"),
        }
    plugins["ops.vram"] = {"enabled": True}
    plugins["feature.miq"] = {
        "enabled": True,
        "config": {
            "layout": text(env, "MIQ_LAYOUT") or "auto",
            "emoji_cache_limit": max(10, integer(env, "MIQ_EMOJI_CACHE_LIMIT", 500)),
        },
    }
    plugins["feature.umigame"] = {
        "enabled": True,
        "config": {
            "d1_api_url": text(env, "D1_API_URL") or "http://127.0.0.1:8083",
            "d1_timeout_seconds": number(env, "D1_TIMEOUT_SECONDS", 20),
            "idle_minutes": max(1, integer(env, "UMIGAME_IDLE_MINUTES", 30)),
        },
    }
    plugins["imagegen.anima"] = {
        "enabled": True,
        "config": {
            "base_url": text(env, "ANIMA_URL") or "http://127.0.0.1:8084",
            "model": text(env, "ANIMA_MODEL"),
            "negative": text(env, "ANIMA_NEGATIVE"),
        },
    }
    plugins["feature.reader"] = {
        "enabled": True,
        "critical": True,
        "config": {
            "allowed_channel_ids": whitelist,
            "global_reaction_words": items(env, "GLOBAL_REACTION_WORDS"),
            "default_speed": number(env, "DEFAULT_SPEED", 1.2),
            "default_speaker": integer(env, "DEFAULT_SPEAKER", 1),
            "max_message_length": integer(env, "MAX_MESSAGE_LENGTH", 200),
            "vc_announcement_enabled": flag(env, "VC_ANNOUNCEMENT_ENABLED", True),
            "vc_announcement_cooldown_seconds": max(
                0, integer(env, "VC_ANNOUNCEMENT_COOLDOWN_SECONDS", 3600)
            ),
            "vc_announcement_voice": text(env, "VC_ANNOUNCEMENT_VOICE") or "clone こしあん",
            "vc_announcement_cache_dir": path("join_voice"),
            "vc_announcement_cache_limit": max(1, integer(env, "VC_ANNOUNCEMENT_CACHE_LIMIT", 128)),
            "vc_announcement_history_path": path("vc_announcements.json"),
            "wav_enabled": True,
            "read_mode": "tts",
            "duck_enabled": flag(env, "DUCK_ENABLED", False),
            "owner_id": owner,
            "bookmark_reaction": text(env, "BOOKMARK_REACTION"),
            "bookmark_notify": flag(env, "BOOKMARK_NOTIFY", True),
            "tts_switch_user_ids": switch_users,
            "mytalk_hidden_voices": items(env, "MYTALK_HIDDEN_VOICES"),
            "toggles_path": path("toggles.json"),
            "require_existing_toggles": True,
            "dashboard_url": front_url,
            "forced_voices_path": path("forced_voices.json"),
            "user_voices_path": path("user_voices.json"),
        },
    }

    sarashina = {
        "api_url": text(env, "SARASHINA_API_URL") or "http://127.0.0.1:8082",
        "key_file": text(env, "SARASHINA_API_KEY_FILE"),
        "model": text(env, "SARASHINA_MODEL") or "sarashina3-mini",
        "requests_per_minute": max(0, integer(env, "SARASHINA_RPM", 0)),
        "max_wait": number(env, "SARASHINA_MAX_WAIT", 120),
        "translate_model": text(env, "SARASHINA_TRANSLATE_MODEL") or None,
        "translate_rpm": max(0, integer(env, "SARASHINA_TRANSLATE_RPM", 0)) or None,
    }
    atmos = {
        "api_url": text(env, "ATMOS_API_URL") or "http://127.0.0.1:8123",
        "model": text(env, "ATMOS_MODEL") or "gemma4-atmos-sft",
        "timeout": number(env, "ATMOS_TIMEOUT_SECONDS", 30),
        "max_tokens": integer(env, "ATMOS_MAX_TOKENS", 1024),
        "context": integer(env, "ATMOS_CONTEXT", 8192),
    }
    local = {
        "api_url": text(env, "TRANSLATION_API_URL") or "http://127.0.0.1:8092",
        "key_file": text(env, "TRANSLATION_API_KEY_FILE"),
    }
    backend = text(env, "TRANSLATION_BACKEND").lower()
    if backend not in BACKENDS:
        backend = "sarashina"

    plugins["provider.llm.sarashina"] = {
        "enabled": True,
        "instances": {"main": {k: v for k, v in sarashina.items() if v is not None}},
    }
    plugins["llm.router"] = {
        "enabled": True,
        "config": {
            "backend": backend,
            "state_file": path("llm_backend.json"),
            "local": local,
            "sarashina": {k: v for k, v in sarashina.items() if v is not None},
            "atmos": atmos,
        },
    }
    plugins["llm.controller"] = {"enabled": True}
    plugins["store.sessions"] = {"enabled": True, "config": {"directory": path("sessions")}}
    plugins["web.safe-fetch"] = {"enabled": True}
    plugins["feature.conversation"] = {"enabled": True}
    plugins["feature.kitasan"] = {
        "enabled": True,
        "config": {
            "enabled": flag(env, "KITASAN_ENABLED", True),
            "words": items(env, "KITASAN_WORDS") or ["今北産業"],
            "messages": max(1, integer(env, "KITASAN_MESSAGES", 100)),
            "item_chars": kitasan_chars,
            "max_chars": kitasan_max,
        },
    }
    plugins["feature.talk-log"] = {
        "enabled": True,
        "config": {
            "log_path": path("talk_log.jsonl"),
            "stats_path": path("talk_stats.json"),
        },
    }
    plugins["feature.translation"] = {
        "enabled": True,
        "config": {
            "allowed_channel_ids": whitelist,
            "owner_id": owner,
            "talk_call_suffix": text(env, "TALK_CALL_SUFFIX"),
            "talk_mention_gate": text(env, "TALK_MENTION_GATE") or "open",
            "talk_model": talk_model,
            "talk_open_channels": items(env, "TALK_OPEN_CHANNELS", int),
            "talk_session_seconds": number(env, "TALK_SESSION_SECONDS", 1800),
            "talk_system_prompt_file": text(env, "TALK_SYSTEM_PROMPT_FILE"),
            "tts_switch_user_ids": switch_users,
        },
    }
    plugins["feature.wavrank"] = {
        "enabled": True,
        "config": {
            "data_dir": str(data_dir),
            # 旧実装の既定は許可チャンネルの先頭と同じだった。
            "channel_id": text(env, "WAV_RANK_CHANNEL")
            or (str(whitelist[0]) if whitelist else ""),
        },
    }
    plugins["web.dashboard"] = {
        "enabled": True,
        "critical": True,
        "config": {
            "host": "127.0.0.1",
            "port": integer(env, "READINGS_DASHBOARD_PORT", 8765),
            "public_url": public_url,
            "auth_file": path(".dashboard-auth.json"),
            "webchat_dir": path("webchat"),
            "duck_curve_path": path("duck_curve.json"),
            "d1_api_url": text(env, "D1_API_URL") or "http://127.0.0.1:8083",
            "d1_timeout_seconds": number(env, "D1_TIMEOUT_SECONDS", 20),
            "talk_model": talk_model or "sarashina3-mini",
            "turtle_soup_max_questions": max(1, integer(env, "TURTLE_SOUP_MAX_QUESTIONS", 100)),
            "turtle_soup_per_ip": max(1, integer(env, "TURTLE_SOUP_PER_IP", 60)),
            "require_existing_passkey": True,
        },
    }

    profile["bindings"] = {
        "tts.router": {"tts.provider": order},
        "llm.router": {"llm.provider": ["provider.llm.sarashina/main"]},
    }
    return profile, {"dropped_backends": dropped, "tts_instances": len(order)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--policy-file", type=Path)
    parser.add_argument("--backends", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    # 優先順位: policy > 起動プロセスの環境 > env ファイル（旧実装と同じ）。
    env = dict(os.environ)
    if args.env_file is not None:
        env.update({k: v for k, v in dotenv_values(args.env_file).items() if v is not None})
    if args.policy_file is not None:
        env.update({k: v for k, v in dotenv_values(args.policy_file).items() if v is not None})

    backends = json.loads(args.backends.read_text(encoding="utf-8"))
    profile, summary = build(env, backends, args.data_dir.resolve())

    text_out = yaml.safe_dump(profile, allow_unicode=True, sort_keys=False)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text_out, encoding="utf-8")
    args.output.chmod(0o600)

    print(f"設定した項目: {len(profile['plugins'])} プラグイン")
    print(f"TTS: {summary['tts_instances']} 系統")
    if summary["dropped_backends"]:
        print("TTS 未対応のため除外: " + ", ".join(summary["dropped_backends"]))
    print("値は出力ファイルだけに書き、秘密は環境変数と鍵ファイル参照のままにした。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
