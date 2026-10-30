"""管理画面からの LLM 接続設定。選択状態とは別ファイルに保存する。"""
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    "local": {"api_url": "http://127.0.0.1:8081", "model": "jllm-translation"},
    "sarashina": {"api_url": "http://127.0.0.1:8082", "model": "sarashina3-mini"},
    "atmos": {"api_url": "http://127.0.0.1:8123", "model": "gemma4-atmos-sft"},
    "openai": {"api_url": "https://api.openai.com", "model": "gpt-4o-mini"},
}
FIELDS = frozenset({"api_url", "model", "translate_model", "api_key"})


def validate_url(value):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 2048
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ValueError("API URL が不正です。")
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                or url.password is not None or url.query or url.fragment or url.port == 0
                or "\\" in value or "%" in url.netloc or url.path not in ("", "/", "/v1", "/v1/")):
            raise ValueError
        _ = url.port
    except ValueError:
        raise ValueError("API URL が不正です。") from None
    return value.rstrip("/")


def validate_fields(payload):
    if not isinstance(payload, dict) or set(payload) - (FIELDS | {"backend"}):
        raise ValueError("設定項目が不正です。")
    name = payload.get("backend")
    if not isinstance(name, str) or name not in DEFAULTS:
        raise ValueError("バックエンドが不正です。")
    result = {}
    for key in FIELDS & payload.keys():
        value = payload[key]
        if key == "api_url":
            result[key] = validate_url(value)
        else:
            if (not isinstance(value, str) or len(value) > 4096
                    or any(ord(c) < 32 or ord(c) == 127 for c in value)):
                raise ValueError("設定値が不正です。")
            if key == "api_key" and any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ValueError("API キーが不正です。")
            if key == "model" and not value.strip():
                raise ValueError("モデルが不正です。")
            if key != "api_key" and value != value.strip():
                raise ValueError("設定値が不正です。")
            result[key] = value
    return name, result


class AdminConfig:
    def __init__(self, path, configs):
        self.path = Path(path)
        self.base = {name: {**DEFAULTS[name], **configs.get(name, {})} for name in DEFAULTS}
        self.overrides = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if (
                    not isinstance(data, dict)
                    or data.get("version") != 1
                    or not isinstance(data.get("backends"), dict)
                ):
                    raise ValueError
                for name, fields in data["backends"].items():
                    if not isinstance(fields, dict):
                        raise ValueError
                    _, parsed = validate_fields({**fields, "backend": name})
                    base = self.base[name]
                    if (
                        "api_url" in parsed
                        and parsed["api_url"] != str(base.get("api_url", "")).rstrip("/")
                        and (base.get("api_key") or base.get("key_file"))
                        and "api_key" not in parsed
                    ):
                        raise ValueError
                    effective = {**base, **parsed}
                    if effective.get("api_key"):
                        endpoint = urlsplit(validate_url(effective["api_url"]))
                        if endpoint.scheme != "https" and endpoint.hostname not in (
                            "localhost", "127.0.0.1", "::1"
                        ):
                            raise ValueError
                    self.overrides[name] = parsed
            except (OSError, ValueError, TypeError, UnicodeError):
                raise ValueError("LLM 設定を読み込めません。") from None

    def config(self, name, overrides=None):
        config = dict(self.base[name])
        config.update((self.overrides if overrides is None else overrides).get(name, {}))
        if "api_key" in config:
            config["key_file"] = ""
        return config

    def status(self, selected):
        backends = {}
        for name in DEFAULTS:
            config = self.config(name)
            try:
                url = validate_url(config.get("api_url", ""))
            except ValueError:
                url = ""
            model = config.get("model") or ""
            backends[name] = {
                "api_url": url,
                "model": model,
                "translate_model": config.get("translate_model") or model,
                "key_configured": bool(config.get("api_key") or config.get("key_file")),
            }
        return {"backend": selected, "backends": backends}

    def prepare(self, payload):
        name, values = validate_fields(payload)
        old = self.config(name)
        if ("api_url" in values and values["api_url"] != old.get("api_url", "").rstrip("/")
                and (old.get("api_key") or old.get("key_file")) and "api_key" not in values):
            raise ValueError("URL の変更時は API キーを明示してください。")
        overrides = {key: dict(value) for key, value in self.overrides.items()}
        overrides.setdefault(name, {}).update(values)
        updated = self.config(name, overrides)
        if updated.get("api_key"):
            endpoint = urlsplit(validate_url(updated["api_url"]))
            if endpoint.scheme != "https" and endpoint.hostname not in (
                "localhost", "127.0.0.1", "::1"
            ):
                raise ValueError("API キーには HTTPS が必要です。")
        return name, overrides

    def save(self, overrides):
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(prefix=".llm-config-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump({"version": 1, "backends": overrides}, stream, ensure_ascii=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, ValueError, TypeError):
            raise ValueError("LLM 設定を保存できません。") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
