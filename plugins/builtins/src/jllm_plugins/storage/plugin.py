import json
import os
from pathlib import Path
from typing import Any


class LocalStorage:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)

    def path(self, *parts: str) -> Path:
        root = self.data_dir.resolve()
        path = root.joinpath(*parts).resolve()
        if not path.is_relative_to(root):
            raise ValueError("storage path escapes DATA_DIR")
        return path

    def plugin_path(self, plugin_id: str, *parts: str) -> Path:
        return self.path("plugins", plugin_id, *parts)

    def legacy_path(self, *parts: str) -> Path:
        return self.path(*parts)

    @staticmethod
    def write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)


class Plugin:
    async def setup(self, ctx):
        self.storage = LocalStorage(ctx.config.get("data_dir", "data"))
        return {"storage.local": self.storage}

    async def start(self):
        pass

    async def stop(self):
        pass
