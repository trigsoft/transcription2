"""Read canonical SheetSage2 ABC with the same bundled parser as staff rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from huggingface_hub import snapshot_download

from .config import Settings


@dataclass(frozen=True)
class AbcNote:
    offset: Fraction
    duration: Fraction
    written_duration: Fraction
    text: str
    midi_pitch: int | None = None
    tie_in: bool = False
    tie_out: bool = False
    slur_starts: tuple[int, ...] = ()
    slur_ends: tuple[int, ...] = ()
    tuplet: int = 0
    tuplet_end: bool = False
    chords: tuple[str, ...] = ()
    key_change: str = ""


@dataclass
class AbcMeasure:
    meter: tuple[int, int]
    key: str
    notes: list[AbcNote] = field(default_factory=list)
    duration: Fraction = Fraction(0)
    bar: str = "bar_thin"
    section: str = ""


@dataclass
class AbcScore:
    vocal: list[AbcMeasure]
    instrumental: list[AbcMeasure]
    tempo: str


def rendering_assets() -> Path:
    settings = Settings.from_environment()
    settings.prepare()
    return (
        Path(
            snapshot_download(
                repo_id=settings.model_id,
                cache_dir=settings.model_cache_dir / "rendering",
                allow_patterns=("rendering_sheetsage2.py", "render_assets/**"),
            )
        )
        / "render_assets"
    )


def _voice_abc(abc: str, selected: str) -> str:
    """Keep each voice's own M:/K: changes; ABCJS otherwise shares staff state."""
    header, voice = True, None
    lines = []
    declared = set()
    for line in abc.splitlines():
        if line.startswith(("%%score", "%%staves")):
            raise ValueError("简谱仅支持 SheetSage2 的两个独立旋律声部")
        match = re.match(r"V:\s*(\S+)", line)
        if match:
            voice = match[1]
            if header:
                declared.add(voice)
            if voice == selected:
                lines.append(line)
            continue
        if header:
            lines.append(line)
            if line.startswith("K:"):
                header = False
        elif line.strip().lower() in {
            "% intro",
            "% prelude",
            "% verse",
            "% pre-chorus",
            "% chorus",
            "% post-chorus",
            "% interlude",
            "% bridge",
            "% solo",
            "% outro",
            "% ending",
            "% instrumental",
        }:
            # Official structural comments occur between aligned voice groups.
            lines.append("P:" + line.strip()[1:].strip())
        elif voice == selected or line.startswith("%"):
            lines.append(line)
    if selected not in declared:
        raise ValueError(f"ABC 缺少 {selected} 声部")
    return "\n".join(lines) + "\n"


def parse_abc_score(abc: str, *, assets: Path | None = None) -> AbcScore:
    from playwright.sync_api import Error as BrowserError
    from playwright.sync_api import sync_playwright

    assets = assets or rendering_assets()
    voices = [_voice_abc(abc, name) for name in ("Vocal", "Ins")]
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
            try:
                context = browser.new_context(offline=True)
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.add_script_tag(path=str(assets / "abcjs-basic-min.js"))
                page.add_script_tag(path=str(assets / "renderer.js"))
                parsed = page.evaluate(
                    """voices => voices.map(abc => {
                    const tunes = ABCJS.parseOnly(expandRests(abc));
                    if (tunes.length !== 1) throw new Error('仅支持单首 ABC 乐谱');
                    const tune = tunes[0];
                    // Jianpu splits composite rests itself; ABCJS still parses their
                    // exact duration even when a single staff glyph cannot show it.
                    const warnings = (tune.warnings || []).filter(warning =>
                        !/^Music Line:\\d+:\\d+: Duration not representable: z[0-9/]*:/.test(warning));
                    if (warnings.length) throw new Error(warnings.join('; '));
                    tune.setUpAudio(); // Resolve pitches with ABCJS; keep each notated duration.
                    return tune.lines.filter(line => line.staff).map(line => {
                        if (line.staff.length !== 1 || line.staff[0].voices.length !== 1)
                            throw new Error('旋律声部结构不受支持');
                        const staff = line.staff[0];
                        return {key: staff.key, meter: staff.meter, events: staff.voices[0]};
                    });
                })""",
                    voices,
                )
            finally:
                browser.close()
    except BrowserError as exc:
        raise RuntimeError(f"ABC 解析失败：{exc}") from exc
    score = AbcScore(*[_decode_voice(blocks) for blocks in parsed], tempo=_tempo(abc))
    if not score.vocal or len(score.vocal) != len(score.instrumental):
        raise ValueError("ABC 人声与器乐的小节数不一致或没有音乐")
    for index, (vocal, instrumental) in enumerate(
        zip(score.vocal, score.instrumental, strict=True), 1
    ):
        if (vocal.meter, vocal.duration) != (instrumental.meter, instrumental.duration):
            raise ValueError(f"ABC 第 {index} 小节的两声部拍号或时值不一致")
    return score


def _tempo(abc: str) -> str:
    value = next(
        (line[2:].strip() for line in abc.splitlines() if line.startswith("Q:")), ""
    )
    return value.rsplit("=", 1)[-1]


def _meter(value: dict) -> tuple[int, int]:
    if value["type"] == "common_time":
        return 4, 4
    if value["type"] == "cut_time":
        return 2, 2
    parts = value.get("value", [])
    if len(parts) != 1 or not str(parts[0]["num"]).isdigit():
        raise ValueError("ABC 拍号不受支持")
    return int(parts[0]["num"]), int(parts[0]["den"])


def _key_name(key: dict) -> str:
    mode = key.get("mode", "")
    if mode not in {"", "m"}:
        raise ValueError(f"ABC 调式不受支持：{mode}")
    return key["root"] + key.get("acc", "") + (":minor" if mode == "m" else ":major")


ACCIDENTALS = {"dblflat": -2, "flat": -1, "natural": 0, "sharp": 1, "dblsharp": 2}
LETTERS = "CDEFGAB"
MAJOR = (0, 2, 4, 5, 7, 9, 11)


def _numbered_pitch(pitch: dict, key: dict, midi_pitch: int) -> str:
    written = pitch["pitch"]
    letter = written % 7
    signature = {
        LETTERS.index(a["note"].upper()): ACCIDENTALS[a["acc"]]
        for a in key["accidentals"]
    }
    tonic_letter = LETTERS.index(key["root"])
    tonic_accidental = {"": 0, "#": 1, "b": -1}[key.get("acc", "")]
    # Standard minor jianpu uses the relative major tonic (A minor: 6=A, 1=C).
    if key.get("mode") == "m":
        tonic_letter = (tonic_letter + 2) % 7
        tonic_accidental = signature.get(tonic_letter, 0)
    degree = (letter - tonic_letter) % 7
    octave = (written - tonic_letter) // 7
    actual = midi_pitch - 60
    expected = MAJOR[tonic_letter] + tonic_accidental + MAJOR[degree] + octave * 12
    difference = actual - expected
    prefix = "♯" * difference if difference > 0 else "♭" * -difference
    dots = "̇" * octave if octave > 0 else "̣" * -octave
    return f"{prefix}{degree + 1}{dots}"


def _numbered_chord(name: str, key: dict) -> str:
    if name in {"N", "NC", "N.C."}:
        return name
    match = re.fullmatch(r"([A-G][#b♯♭]*)(.*)", name)
    if not match:
        raise ValueError(f"ABC 和弦名称不受支持：{name}")

    def degree(note: str) -> str:
        letter, accidentals = note[0], note[1:]
        alteration = sum(1 if sign in "#♯" else -1 for sign in accidentals)
        index = LETTERS.index(letter)
        label = _numbered_pitch({"pitch": index}, key, 60 + MAJOR[index] + alteration)
        # Chord roots and inversion bass are pitch classes, without octave dots.
        return label.replace("̇", "").replace("̣", "")

    root, quality = match.groups()
    suffix, slash, bass = quality.rpartition("/")
    if slash and re.fullmatch(r"[A-G][#b♯♭]*", bass):
        quality = suffix + "/" + degree(bass)
    return degree(root) + quality


def _decode_voice(blocks: list[dict]) -> list[AbcMeasure]:
    measures = []
    current = None
    multiplier = Fraction(1)
    key = None
    meter = None
    key_change = ""
    tied_pitch = None
    section = ""
    for block in blocks:
        if block["key"] and (key is None or _key_name(key) != _key_name(block["key"])):
            if key is not None:
                key_change = _key_name(block["key"])
            key = block["key"]
        if block["meter"] is not None:
            meter = _meter(block["meter"])
        if key is None or meter is None:
            raise ValueError("ABC 缺少初始调号或拍号")
        for event in block["events"]:
            kind = event["el_type"]
            if kind == "part":
                if current is not None and current.notes:
                    raise ValueError("ABC 段落标记必须位于小节起点")
                section = event["title"]
                continue
            if kind in {"key", "keySignature"}:
                key = event
                key_change = _key_name(key)
                continue
            if kind == "meter":
                meter = _meter(event)
                continue
            if kind == "bar":
                if (
                    "repeat" in event["type"]
                    or event.get("startEnding")
                    or event.get("endEnding")
                ):
                    raise ValueError("简谱暂不支持带反复或不同结尾的 ABC")
                if current is not None and current.notes:
                    current.bar = event["type"]
                    measures.append(current)
                    current = None
                continue
            if kind != "note":
                if kind in {"clef", "tempo", "stem", "style", "scale"}:
                    continue
                raise ValueError(f"ABC 事件不受支持：{kind}")
            if event.get("gracenotes"):
                raise ValueError("简谱暂不支持装饰音")
            if current is None:
                current = AbcMeasure(meter, _key_name(key), section=section)
                section = ""
            if event.get("startTriplet"):
                multiplier = Fraction(
                    str(event["tripletMultiplier"])
                ).limit_denominator(1024)
            written = Fraction(str(event["duration"])).limit_denominator(4096)
            duration = written * multiplier
            if written <= 0:
                raise ValueError("ABC 音符时值必须大于零")
            pitches = event.get("pitches", [])
            if len(pitches) > 1:
                raise ValueError("简谱旋律声部暂不支持同时发声的多个音符")
            pitch = pitches[0] if pitches else {}
            rest = event.get("rest")
            if rest and rest["type"] not in {"rest", "whole", "invisible", "spacer"}:
                raise ValueError(f"ABC 休止类型不受支持：{rest['type']}")
            midi_pitch = None
            if pitch:
                resolved = event.get("midiPitches", [])
                if pitch.get("endTie"):
                    if tied_pitch is None:
                        raise ValueError("ABC 延音线缺少起点")
                    midi_pitch = tied_pitch
                    if resolved and resolved[0]["pitch"] != midi_pitch:
                        raise ValueError("ABC 延音线音高不一致")
                elif resolved:
                    if tied_pitch is not None:
                        raise ValueError("ABC 延音线缺少续音")
                    midi_pitch = resolved[0]["pitch"]
                else:
                    raise ValueError("ABCJS 没有解析出音符音高")
                text = _numbered_pitch(pitch, key, midi_pitch)
                tied_pitch = midi_pitch if "startTie" in pitch else None
            else:
                if tied_pitch is not None:
                    raise ValueError("ABC 延音线不能连接休止符")
                text = "0" if rest and rest["type"] in {"rest", "whole"} else ""
            current.notes.append(
                AbcNote(
                    offset=current.duration,
                    duration=duration,
                    written_duration=written,
                    text=text,
                    midi_pitch=midi_pitch,
                    tie_in=bool(pitch.get("endTie")),
                    tie_out="startTie" in pitch,
                    slur_starts=tuple(
                        item["label"] for item in pitch.get("startSlur", [])
                    ),
                    slur_ends=tuple(pitch.get("endSlur", [])),
                    tuplet=event.get("startTriplet", 0),
                    tuplet_end=bool(event.get("endTriplet")),
                    chords=tuple(
                        _numbered_chord(chord["name"], key)
                        for chord in event.get("chord", [])
                        if chord.get("position") == "default"
                    ),
                    key_change=key_change,
                )
            )
            current.duration += duration
            key_change = ""
            if event.get("endTriplet"):
                multiplier = Fraction(1)
    if current is not None and current.notes:
        measures.append(current)
    if tied_pitch is not None:
        raise ValueError("ABC 延音线缺少终点")
    return measures
