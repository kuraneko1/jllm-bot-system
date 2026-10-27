"""読み上げ音量のカーブ（ファンの回転数制御のように、区間ごとに音量点を持つ）。

- 点は「喋り始めてからの秒数」と「その区間の音量」。ステップ方式で、
  次の点の秒数まではその音量を保つ（``target()``）。
  例: ``[(0.0, 1.0), (1.0, 0.2)]`` = 1秒まではそのまま、1秒を過ぎたら20%まで下げる。
- 下げる速さ（``attack``）と戻す速さ（``release``）は、目標へ寄せる1tickあたりの割合。
- 喋り終わってからの保持は ``hold_seconds``。
- Web UI から編集でき、``data/duck_curve.json`` に保存する（走っている再生にも即反映）。
"""

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path

logger = logging.getLogger(__name__)

#: 音量として認める範囲
MIN_VOLUME = 0.0
MAX_VOLUME = 1.0
#: 点の秒数として認める範囲と、点の数
MAX_SECONDS = 60.0
MAX_POINTS = 8
#: 速さ（1tickあたりに目標へ寄せる割合）
MIN_SPEED = 0.02
MAX_SPEED = 1.0


@dataclass(frozen=True)
class Point:
    """区間の音量点。"""

    seconds: float
    volume: float

    def to_dict(self) -> dict:
        return {"seconds": round(self.seconds, 2), "volume": round(self.volume, 3)}


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass
class DuckCurve:
    """読み上げ音量のカーブ。"""

    points: list[Point]
    attack: float = 0.25
    release: float = 0.06
    hold_seconds: float = 0.8
    #: 保存先（読み込み元）
    path: Path | None = None

    def __post_init__(self):
        self.points = normalize_points(self.points)
        self.attack = clamp(float(self.attack), MIN_SPEED, MAX_SPEED)
        self.release = clamp(float(self.release), MIN_SPEED, MAX_SPEED)
        self.hold_seconds = clamp(float(self.hold_seconds), 0.0, 10.0)

    # ---------------------------------------------------------------- 計算

    @property
    def level(self) -> float:
        """いちばん低い音量（表示用）。"""

        return min((point.volume for point in self.points), default=1.0)

    @property
    def reaction_seconds(self) -> float:
        """下げ始めるまでの秒数（音量が最初に変わる点）。"""

        first = self.points[0].volume if self.points else 1.0

        for point in self.points[1:]:
            if point.volume < first - 0.005:
                return point.seconds

        return 0.0

    def target(self, seconds: float) -> float:
        """喋り始めて ``seconds`` 秒たったときの目標音量（区間ごとに一定）。"""

        if not self.points:
            return 1.0

        chosen = self.points[0].volume

        for point in self.points:
            if seconds >= point.seconds:
                chosen = point.volume
            else:
                break

        return chosen

    # ---------------------------------------------------------------- 入出力

    def to_dict(self) -> dict:
        return {
            "points": [point.to_dict() for point in self.points],
            "attack": round(self.attack, 3),
            "release": round(self.release, 3),
            "hold_seconds": round(self.hold_seconds, 2),
            "level": round(self.level, 3),
            "reaction_seconds": round(self.reaction_seconds, 2),
        }

    @classmethod
    def from_dict(cls, data: dict, *, path: Path | None = None) -> DuckCurve:
        points = []

        for item in (data or {}).get("points") or []:
            try:
                points.append(Point(float(item["seconds"]), float(item["volume"])))
            except KeyError, TypeError, ValueError:
                continue

        return cls(
            points or default_points(),
            attack=(data or {}).get("attack", 0.25),
            release=(data or {}).get("release", 0.06),
            hold_seconds=(data or {}).get("hold_seconds", 0.8),
            path=path,
        )

    @classmethod
    def default(cls) -> DuckCurve:
        """既定のカーブ（1秒まではそのまま、その後20%まで下げる）。"""

        return cls(default_points(), attack=0.25, release=0.06, hold_seconds=0.8)

    @classmethod
    def from_config(
        cls,
        *,
        trigger_seconds: float,
        level: float,
        attack: float,
        release: float,
        hold_seconds: float,
    ) -> DuckCurve:
        """設定（DUCK_*）から作る。区間は2つ（そのまま → 下げる）。"""

        return cls(
            [Point(0.0, 1.0), Point(max(0.0, float(trigger_seconds)), float(level))],
            attack=attack,
            release=release,
            hold_seconds=hold_seconds,
        )

    @classmethod
    def load(cls, path, fallback: DuckCurve | None = None) -> DuckCurve:
        """保存されたカーブを読む（無ければ ``fallback``、それも無ければ既定）。"""

        target = Path(path) if path else None

        if target is not None and target.is_file():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))

                return cls.from_dict(data, path=target)
            except OSError, ValueError:
                logger.warning("音量カーブを読めませんでした: %s", target, exc_info=True)

        if fallback is not None:
            fallback.path = target

            return fallback

        return cls(default_points(), path=target)

    def save(self, path=None) -> Path | None:
        """保存する（Web UI からの編集を残す）。"""

        target = Path(path) if path else self.path

        if target is None:
            return None

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            self.path = target
        except OSError:
            logger.warning("音量カーブを保存できませんでした: %s", target, exc_info=True)
            return None

        return target

    def copy(self) -> DuckCurve:
        return replace(self, points=list(self.points))


def normalize_points(points) -> list[Point]:
    """点を整える（範囲内・秒数順・同じ秒数は1つ・最大数まで）。"""

    cleaned: list[Point] = []

    for point in points or []:
        try:
            seconds = clamp(float(point.seconds), 0.0, MAX_SECONDS)
            volume = clamp(float(point.volume), MIN_VOLUME, MAX_VOLUME)
        except TypeError, ValueError:
            continue

        cleaned.append(Point(round(seconds, 2), round(volume, 3)))

    cleaned.sort(key=lambda item: item.seconds)

    unique: list[Point] = []

    for point in cleaned:
        if unique and abs(unique[-1].seconds - point.seconds) < 0.01:
            unique[-1] = point
            continue

        unique.append(point)

    if not unique:
        return default_points()

    return unique[:MAX_POINTS]


def default_points() -> list[Point]:
    """既定の2区間（そのまま → 下げる）。"""

    return [Point(0.0, 1.0), Point(1.0, 0.2)]
