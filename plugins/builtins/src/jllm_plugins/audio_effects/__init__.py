"""Chat suffix voice effects (ffmpeg filter builders).

Each effect has a 2-letter code and an intensity. ``rb`` means intensity 1,
``rb10`` means intensity 10. Intensities are clamped to 1..20.

``ec`` だけは離散タップではなく畳み込み（``afir``）で実装しているため、
``build_filter`` がフィルタグラフを組み立てる。IR は ``ir/room.wav``。
"""

import math
import wave

MIN_INTENSITY = 1.0
MAX_INTENSITY = 20.0

# 畳み込みは音源のサンプルレートに依存するため、グラフ内で 48kHz に揃える。
IR_RATE = 48000
_IR_PATH = "data/ir/room.wav"


def _ir_seconds(path: str | None = None) -> float:
    """IR の長さ（秒）。畳み込みで削られる尻尾を戻すのに使う。"""
    try:
        with wave.open(path or _IR_PATH) as handle:
            return handle.getnframes() / float(handle.getframerate())
    except OSError, wave.Error:
        return 0.0


IR_SECONDS = _ir_seconds()

# alimiter の level は既定 true で、出力を 0dB へ正規化してしまう。
# クリップ防止のつもりがフルスケールまで持ち上げるため level=false を必ず付ける。
_LIMITER = "alimiter=limit=0.95:level=false"


def clamp_intensity(value) -> float:
    try:
        number = float(value)
    except TypeError, ValueError:
        return 1.0
    if not math.isfinite(number):
        return 1.0
    return min(MAX_INTENSITY, max(MIN_INTENSITY, number))


def _num(value) -> str:
    text = f"{float(value):.4f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def _reverb(intensity):
    d1 = int(30 + 15 * intensity)
    d2 = int(80 + 30 * intensity)
    d3 = int(180 + 60 * intensity)
    g1 = min(0.85, 0.35 + 0.05 * intensity)
    g2 = min(0.80, 0.28 + 0.04 * intensity)
    g3 = min(0.75, 0.20 + 0.03 * intensity)
    return f"aecho=0.8:0.7:{d1}|{d2}|{d3}:{_num(g1)}|{_num(g2)}|{_num(g3)}", True


def _echo(intensity, ir_path: str | None = None):
    """畳み込みリバーブ（合成 IR・ステレオ無相関）。

    離散タップだと「ヤマビコ」で広がりが出ないため、指数減衰するノイズと
    初期反射から作った実インパルス応答を畳み込む。IR 自体が左右で無相関
    なので、掛けるだけで自然な広がりが出る。

    ``afir`` の dry/wet は ffmpeg のバージョンで挙動が違う。
    6.1.1 は dry を素通しするが、**bot が動く 7.1.3 は dry を素通しせず、
    小さい dry では出力が無音になる**（dry=0 も 0.001 も無音、1 以上で有効）。
    そのため dry=1 で残響を取り出し、元音声との混合は呼び出し側
    （build_filter）の asplit + amix で行う。音量は volume で作る。

    さらに ``afir`` は出力を入力と同じ長さに切り、残響の尻尾を捨てるので、
    畳み込む前に無音を足して長さを確保する。
    """
    length = min(1.0, 0.35 + 0.040 * intensity)
    # 7.1.3 での実測: volume 50→左右相関0.78 / 100→0.46 / 200→0.16。
    # 低強度でも聴こえるよう、強さ1でも 0.85 前後になる値から始める。
    wet = min(400.0, 38.0 * intensity**0.5)
    pad = _ir_seconds(ir_path) * length

    template = (
        "[0:a]aresample={rate},aformat=channel_layouts=stereo{PRE},"
        "asplit=2[dry][send];"
        "[send]apad=pad_dur={pad}[sendp];"
        "amovie={ir}[ir];[ir]aresample={rate}[ir48];"
        "[sendp][ir48]afir=dry=1:wet=1:gtype=none:length={length},"
        "volume={wet}[rev];"
        "[dry][rev]amix=inputs=2:duration=longest:normalize=0[room];"
        "[room]anull{LIMIT}"
    ).format(
        rate=IR_RATE,
        ir=ir_path or _IR_PATH,
        pad=_num(pad),
        length=_num(length),
        wet=_num(wet),
        PRE="{PRE}",
        LIMIT="{LIMIT}",
    )
    return ("graph", template), True


def _sparkle(intensity):
    """キラキラ（シマー）。オクターブ上を重ねた残響で「シャラララ」を作る。

    本物のシマーは残響にオクターブ上を戻すフィードバックで作るが、ffmpeg の
    フィルタグラフではループを組めないため、ピッチ違いの枝を並列に重ねて
    同じ響きにしている。+7 / +12 / +19 半音の 3 層。
    """
    level = min(1.0, max(0.0, intensity / MAX_INTENSITY))
    mix1 = 0.14 + 0.26 * level  # +7半音（完全5度）
    mix2 = 0.12 + 0.24 * level  # +12半音（オクターブ）
    mix3 = 0.07 + 0.18 * level  # +19半音（オクターブ＋5度）
    step = int(80 + 10 * intensity)
    side = min(0.85, 0.45 + 0.02 * intensity)

    def taps(mult):
        first = max(30, int(step * mult))
        return f"{first}|{first * 2}|{int(first * 3.4)}"

    template = (
        "[0:a]aformat=channel_layouts=stereo{PRE},asplit=4[dry][a][b][c];"
        "[a]rubberband=pitch=1.4983,"
        "aecho=0.8:0.7:{t1}:0.45|0.30|0.20,volume={m1}[sa];"
        "[b]rubberband=pitch=2.0,"
        "aecho=0.8:0.7:{t2}:0.45|0.30|0.20,volume={m2}[sb];"
        "[c]rubberband=pitch=2.997,"
        "aecho=0.8:0.7:{t3}:0.40|0.28|0.18,volume={m3}[sc];"
        "[dry]anull[dd];"
        "[dd][sa][sb][sc]amix=inputs=4:duration=longest:normalize=0,"
        "aformat=channel_layouts=stereo,"
        "haas=side_gain={side}:middle_source=mid:left_delay=1.2:right_delay=9.5"
        "{LIMIT}"
    ).format(
        t1=taps(1.0),
        t2=taps(1.5),
        t3=taps(2.2),
        m1=_num(mix1),
        m2=_num(mix2),
        m3=_num(mix3),
        side=_num(side),
        PRE="{PRE}",
        LIMIT="{LIMIT}",
    )
    return ("graph", template), True


def _chorus(intensity):
    depth1 = 1.5 + 0.8 * intensity
    depth2 = 1.0 + 0.5 * intensity
    return (f"chorus=0.5:0.9:55|66:0.4|0.32:0.4|0.5:{_num(depth1)}|{_num(depth2)}"), False


def _vibrato(intensity):
    # レートは 11Hz で頭打ち。20 まで上げると 43Hz になり、ビブラートでは
    # なく「ブルブル」という別物になる。
    rate = min(11.0, 3.5 + 1.2 * intensity)
    depth = min(0.80, 0.20 + 0.10 * intensity)
    return f"vibrato=f={_num(rate)}:d={_num(depth)}", False


def _lofi(intensity):
    """潰れた音＋電話・ラジオ風の帯域制限（旧 lo と ph を統合）。

    ``aa=1`` で acrusher のアンチエイリアスを最大にし、ザリザリを抑える。
    """
    bits = max(4, int(10 - 2 * intensity))
    band = max(1800, int(3900 - 140 * intensity))
    low_cut = min(band - 300, int(560 + 45 * intensity))
    return (
        f"acrusher=level_in=1:level_out=1:bits={bits}:mix=0.7:aa=1,"
        f"highpass=f={low_cut},lowpass=f={band},"
        f"volume={_num(2.6)}"
    ), True


def _bass(intensity):
    # 20 まで素直に伸ばすと 43dB になり、音量が +9dB 暴騰する。12dB で頭打ち。
    gain = min(12.0, 2.0 + 0.8 * intensity)
    return f"bass=g={_num(gain)}:f=110", True


def _pitch(intensity):
    factor = 2.0 ** (intensity / 12.0)
    return f"rubberband=pitch={_num(factor)}", False


def _reverse(_intensity):
    return "areverse", False


def _loud(intensity):
    """音圧を上げる。volume + リミッターだと常にリミッターが全力で歪むため、
    コンプレッサーで持ち上げる。

    注意: acompressor の threshold は dB ではなくリニア値（0..1）。
    """
    threshold = max(0.02, 0.50 - 0.022 * intensity)
    ratio = min(12.0, 2.0 + 0.5 * intensity)
    makeup = min(6.0, 1.10 + 0.22 * intensity)
    return (
        f"acompressor=threshold={_num(threshold)}:ratio={_num(ratio)}"
        f":attack=5:release=180:makeup={_num(makeup)}:knee=2.8"
    ), True


def _widen(intensity):
    """広がり。Haas 効果で左右に時間差を作り、モノラル音源を立体的にする。

    入力がモノラルでも効くよう、先にステレオへ揃える。
    """
    side = min(0.95, 0.35 + 0.03 * intensity)
    delay = min(28.0, 6.0 + 1.1 * intensity)
    return (
        "aformat=channel_layouts=stereo,"
        f"haas=side_gain={_num(side)}:middle_source=mid"
        f":left_delay={_num(delay)}:right_delay={_num(delay * 0.55)}"
    ), True


def _phaser(intensity):
    delay = min(5.0, 1.5 + 0.2 * intensity)
    decay = min(0.90, 0.25 + 0.03 * intensity)
    speed = min(2.0, 0.30 + 0.08 * intensity)
    return (
        f"aphaser=in_gain=0.6:out_gain=0.9:delay={_num(delay)}"
        f":decay={_num(decay)}:speed={_num(speed)}:type=sinusoidal"
    ), True


def _exciter(intensity):
    """倍音を足して声の抜けを良くする。

    全体の peak/mean はほとんど動かないが、6kHz 以上の倍音が乗る
    （実測: 強さ5 で 6kHz 以上が -50.5dB → -40dB 程度）。
    """
    amount = min(6.0, 0.6 + 0.30 * intensity)
    freq = min(9000, int(3500 + 250 * intensity))
    return f"aexciter=amount={_num(amount)}:drive=8.5:freq={freq}:blend=0", True


def _echo_legacy(intensity):
    """従来の ec（離散タップのヤマビコ）。

    2026-10-12 に ec を畳み込みへ置き換えた際の実装をそのまま ecl として残す。
    タップが離れた 2 点なので広がりは出ないが、はっきりした反復が欲しいとき用。
    """
    d1 = int(150 + 60 * intensity)
    d2 = int(300 + 140 * intensity)
    g = min(0.80, 0.40 + 0.03 * intensity)
    return f"aecho=0.8:0.55:{d1}|{d2}:{_num(g)}|{_num(g * 0.7)}", True


# ツリーチャイム: 金属棒のインハーモニック比（基音 : 2.76 : 5.40）と音程。
_CHIME_PITCHES = (1174.66, 1318.51, 1567.98, 1760.00, 2093.00, 2349.32)
_CHIME_PARTIALS = ((1.0, 1.7, 0.90), (2.76, 1.1, 0.42), (5.40, 0.7, 0.20))
_CHIME_STARTS = (0.15, 0.72, 1.30, 1.95, 2.60, 3.25, 3.90, 4.55, 5.20, 5.85)


def _tree_chime(intensity):
    """ツリーチャイム。金属棒の澄んだ音を合成して声に重ねる。

    純音では「ピー」にしかならないので、金属棒のインハーモニックな倍音
    （基音の 2.76 倍・5.40 倍）を指数減衰で重ねて鐘らしさを出している。
    音程は D6 E6 G6 A6 C7 D7 の 6 音を順に巡回。
    """
    level = min(1.0, max(0.0, intensity / MAX_INTENSITY))
    count = min(len(_CHIME_STARTS), 3 + int(round(7 * level)))
    gain = 0.10 + 0.28 * level
    decay = 0.6 + 1.2 * level

    nodes = ["[0:a]aformat=channel_layouts=stereo{PRE}[voice]"]
    labels = []
    for index in range(count):
        start = _CHIME_STARTS[index]
        pitch = _CHIME_PITCHES[index % len(_CHIME_PITCHES)]
        for sub, (mult, length, amp) in enumerate(_CHIME_PARTIALS):
            tag = f"ch{index}_{sub}"
            nodes.append(
                f"sine=frequency={pitch * mult:.2f}:duration={max(0.12, length * decay):.2f},"
                f"afade=t=out:st=0:d={max(0.12, length * decay):.2f}:curve=exp,"
                "aformat=channel_layouts=stereo,"
                f"adelay={int(start * 1000)}:all=1,volume={amp * gain:.4f}[{tag}]"
            )
            labels.append("[" + tag + "]")

    nodes.append(
        "[voice]"
        + "".join(labels)
        + f"amix=inputs={len(labels) + 1}:duration=longest:normalize=0[room]"
        + ";[room]anull{LIMIT}"
    )
    return ("graph", ";".join(nodes)), True


EFFECTS = {
    # code: (label_ja, builder, description)
    "rb": ("リバーブ", _reverb, "部屋の響き。数字で部屋の大きさ"),
    "ec": ("エコー", _echo, "自然な残響の広がり。数字で深さと長さ"),
    "ecl": ("エコー(従来)", _echo_legacy, "従来のヤマビコ。数字で間隔と反復"),
    "ch": ("コーラス", _chorus, "声の重なり。数字で深さ"),
    "vi": ("ビブラート", _vibrato, "声の揺れ。数字で速さと深さ"),
    "lo": ("ローファイ", _lofi, "潰れた音＋電話・ラジオ風。数字で潰し具合と帯域"),
    "bs": ("重低音", _bass, "低音増強。数字でdB相当"),
    "pi": ("ピッチ", _pitch, "声の高さ。数字は半音（pi4で+4半音）"),
    "rv": ("逆再生", _reverse, "逆向き再生。数字は無視"),
    "lm": ("ラウド", _loud, "音圧上げ。数字で圧縮の強さ"),
    "wd": ("広がり", _widen, "左右に広げる。数字で広がりの強さ"),
    "ph": ("フェイザー", _phaser, "シュワシュワした揺れ。数字で深さと速さ"),
    "ex": ("エキサイター", _exciter, "声の抜け・存在感。数字で強さ"),
    "kk": ("キラキラ", _sparkle, "シャラララしたシマー。数字で広がりと深さ"),
    "tc": ("ツリーチャイム", _tree_chime, "澄んだチャイムを重ねる。数字で数と深さ"),
}

EFFECT_CODES = tuple(EFFECTS)


def build_filter(effects, ir_path: str | None = None) -> str | None:
    """効果列から ffmpeg のフィルタ文字列を作る。空なら None。

    枝分かれする効果（ec の畳み込み、kk のシマー）はフィルタグラフを返す。
    その場合は最後の段に置き、他の効果を掛けた結果を入力として渡す。
    テンプレート中の ``{PRE}`` に先行効果、``{LIMIT}`` に末尾の効果とリミッターが入る。
    """
    parts: list[tuple[bool, str]] = []  # (畳み込みの後ろへ回すか, フィルタ)
    limiter = False
    graph: str | None = None

    for code, intensity in effects or ():
        lowered = str(code).lower()
        entry = EFFECTS.get(lowered)
        if entry is None:
            continue
        _, builder, _ = entry
        try:
            if lowered == "ec":
                piece, wants_limiter = builder(clamp_intensity(intensity), ir_path=ir_path)
            else:
                piece, wants_limiter = builder(clamp_intensity(intensity))
        except ValueError, TypeError, ArithmeticError:
            continue
        limiter = limiter or wants_limiter
        if isinstance(piece, tuple):
            graph = piece[1]
            continue
        parts.append((lowered == "vi", piece))

    if graph is not None:
        # vi は畳み込み（afir）の後ろ・リミッターの直前に回す。afir の直前に
        # vibrato を置くと、ffmpeg 6.1.1 は afir の内部バッファを初期化しない
        # まま出力し、無音を入力しても先頭 0.85 秒前後が -inf になる（実測。
        # IR が長いほど範囲が伸びる）。その -inf はアミックス後のリミッターで
        # 0.95 に張り付いたまま残り、サンプルの 6 割前後が天井に張り付く＝激しい
        # 音割れになる。vibrato は入力の線形補間なので、後ろに置いてもピークは
        # 増えない（実測 0.35 → 0.247）。リミッターの手前に置くのは、vibrato
        # 自身が先頭 5ms に出す非有限サンプルをリミッターに無害化させるため。
        late = "afir=" in graph
        pre_parts = [piece for is_vibrato, piece in parts if not (late and is_vibrato)]
        tail_parts = [piece for is_vibrato, piece in parts if late and is_vibrato]
        pre = "," + ",".join(pre_parts) if pre_parts else ""
        last_parts = tail_parts + ([_LIMITER] if limiter else [])
        limit = "," + ",".join(last_parts) if last_parts else ""
        return graph.replace("{PRE}", pre).replace("{LIMIT}", limit)

    if not parts:
        return None
    plain = [piece for _, piece in parts]
    if limiter and not any("alimiter" in part for part in plain):
        plain.append(_LIMITER)
    return ",".join(plain)


def describe_effects(effects) -> str:
    """適用内容の短い説明（ログ・エラー文用）。"""
    names = []
    for code, intensity in effects or ():
        entry = EFFECTS.get(str(code).lower())
        if entry is None:
            continue
        names.append(f"{entry[0]}x{clamp_intensity(intensity):g}")
    return "＋".join(names)
