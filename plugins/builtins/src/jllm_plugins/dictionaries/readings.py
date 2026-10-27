import json
import re
from pathlib import Path

from .dictionary import (
    _build_normalized,
    boundary_pattern,
    normalize,
)


class ReadingDictionary:
    def __init__(self, data_path: str):
        self.data_path = Path(data_path)

        self.enabled = True
        self.entries: dict[str, str] = {}
        self.meta: dict[str, str] = {}

        self._pattern: re.Pattern | None = None
        self._index: list[tuple[str, str]] = []

        self.load()

    def load(self):
        self.entries = {}
        self.meta = {}

        if self.data_path.exists():
            try:
                data = json.loads(self.data_path.read_text("utf-8"))

                if isinstance(data, dict):
                    for key, value in data.items():
                        trigger = str(key)

                        if isinstance(value, dict):
                            reading = str(value.get("reading", ""))
                            author = value.get("by")

                            if author:
                                self.meta[trigger] = str(author)
                        else:
                            reading = str(value)

                        self.entries[trigger] = reading

            except OSError, ValueError:
                self.entries = {}
                self.meta = {}

        self._rebuild()

    def save(self):
        payload = {}

        for trigger, reading in self.entries.items():
            author = self.meta.get(trigger)

            if author:
                payload[trigger] = {
                    "reading": reading,
                    "by": author,
                }
            else:
                payload[trigger] = reading

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
            (normalize(trigger), reading) for trigger, reading in self.entries.items() if reading
        ]
        pairs = [(key, reading) for key, reading in pairs if key]
        pairs.sort(key=lambda item: len(item[0]), reverse=True)

        self._index = pairs

        alternatives = []

        for key, _ in pairs:
            alternatives.append(boundary_pattern(key))

        if alternatives:
            self._pattern = re.compile("|".join(alternatives))
        else:
            self._pattern = None

    def _find_key(self, trigger: str) -> str | None:
        target = normalize(trigger)

        for key in self.entries:
            if normalize(key) == target:
                return key

        return None

    def add(
        self,
        trigger: str,
        reading: str,
        author: str | None = None,
    ):
        existing = self._find_key(trigger)

        if existing is not None:
            del self.entries[existing]
            self.meta.pop(existing, None)

        self.entries[trigger] = reading

        if author:
            self.meta[trigger] = str(author)

        self._rebuild()
        self.save()

    def remove(self, trigger: str) -> bool:
        existing = self._find_key(trigger)

        if existing is None:
            return False

        del self.entries[existing]
        self.meta.pop(existing, None)
        self._rebuild()
        self.save()
        return True

    def items(self) -> list[tuple[str, str]]:
        return sorted(self.entries.items())

    def author_of(self, trigger: str) -> str | None:
        key = self._find_key(trigger)

        if key is None:
            return None

        return self.meta.get(key)

    def _lookup(self, matched: str) -> str | None:
        for key, reading in self._index:
            if key == matched:
                return reading

        return None

    def apply(self, text: str) -> str:
        if not self.enabled or self._pattern is None:
            return text

        normalized, starts, ends = _build_normalized(text)

        output = []
        position = 0

        for match in self._pattern.finditer(normalized):
            start = starts[match.start()]
            end = ends[match.end() - 1]

            output.append(text[position:start])

            reading = self._lookup(match.group(0))
            output.append(reading if reading is not None else text[start:end])

            position = end

        output.append(text[position:])

        return "".join(output)
