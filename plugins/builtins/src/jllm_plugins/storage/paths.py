import os
from pathlib import Path


def resolve_data_path(env_key, legacy_name, env=None):
    env = dict(os.environ if env is None else env)
    explicit = env.get(env_key)
    if explicit:
        return explicit
    data_dir = env.get("DATA_DIR", "data")
    data_path = Path(data_dir) / legacy_name
    if "DATA_DIR" not in env and Path(legacy_name).exists():
        return legacy_name
    if data_path.exists():
        return str(data_path)
    if "DATA_DIR" in env:
        return str(data_path)
    legacy_files = ("readings.json", "sounds.json", "media.json", "user_voices.json", "sounds")
    if any(Path(name).exists() for name in legacy_files):
        return legacy_name
    return str(data_path)
