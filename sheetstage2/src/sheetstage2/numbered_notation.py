from __future__ import annotations

import unicodedata
from dataclasses import dataclass, replace
from fractions import Fraction
from html import escape
from pathlib import Path

from .abc_score import AbcMeasure, AbcNote, AbcScore, parse_abc_score

MUSIC_START_X = -10
SYSTEM_WIDTH = 720
SYSTEM_GAP = 20


@dataclass(frozen=True)
class Glyph:
    offset: Fraction
    text: str
    underlines: int
    tie_in: bool = False
    tie_out: bool = False
    slur_starts: tuple[int, ...] = ()
    slur_ends: tuple[int, ...] = ()
    tuplet: int = 0
    tuplet_end: bool = False
    chords: tuple[str, ...] = ()
    key_change: str = ""
    duration: Fraction = Fraction(0)


def render_numbered_abc(
    abc: str | Path,
    *,
    output_dir: str | Path | None,
    formats: str,
    title: str,
    show_vocal: bool = False,
) -> dict[str, list[str]]:
    source = Path(abc).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"生成简谱需要文件：{source}")
    requested = tuple(
        dict.fromkeys(x.strip().lower() for x in formats.split(",") if x.strip())
    )
    unknown = set(requested) - {"pdf", "svg", "png"}
    if unknown:
        raise ValueError(f"未知简谱格式：{', '.join(sorted(unknown))}")
    score = parse_abc_score(source.read_text(encoding="utf-8"))
    destination = (
        Path(output_dir).expanduser().resolve() if output_dir else source.parent
    )
    destination.mkdir(parents=True, exist_ok=True)
    return _write_pages(
        _build_pages(score, title, show_vocal=show_vocal), destination, requested
    )


def _note_glyphs(note: AbcNote) -> list[Glyph]:
    # Exact ABC duration; long notes use dashes, irregular lengths use tied pieces.
    remaining = note.written_duration * 4
    if remaining.denominator & (remaining.denominator - 1):
        raise ValueError("ABC 非二分时值需通过连音组表达")
    offset = note.offset
    pieces = []
    while remaining:
        if remaining >= 1:
            beats = min(4, int(remaining))
            if remaining == Fraction(3, 2):
                beats = Fraction(3, 2)
            duration = Fraction(beats)
            underlines = 0
            dotted = duration == Fraction(3, 2)
        else:
            underlines = 1
            duration = Fraction(1, 2)
            while duration > remaining:
                duration /= 2
                underlines += 1
            dotted = remaining == duration * Fraction(3, 2)
            if dotted:
                duration *= Fraction(3, 2)
        text = note.text + ("·" if dotted and note.text else "")
        pieces.append((offset, text, underlines, duration))
        if duration >= 2:
            pieces.extend(
                (
                    offset + Fraction(index, 4) * note.duration / note.written_duration,
                    "–" if note.text else "",
                    0,
                    Fraction(0),
                )
                for index in range(1, int(duration))
            )
        offset += duration / 4 * note.duration / note.written_duration
        remaining -= duration
    heads = [index for index, piece in enumerate(pieces) if piece[3]]
    result = []
    for index, (offset, text, underlines, duration) in enumerate(pieces):
        first, last = index == heads[0], index == heads[-1]
        head = bool(duration)
        pitched = bool(note.text and note.text != "0")
        result.append(
            Glyph(
                offset,
                text,
                underlines,
                pitched and head and (note.tie_in if first else True),
                pitched and head and (note.tie_out if last else True),
                note.slur_starts if first else (),
                note.slur_ends if last else (),
                note.tuplet if first else 0,
                note.tuplet_end if last else False,
                note.chords if first else (),
                note.key_change if first else "",
                duration / 4 * note.duration / note.written_duration,
            )
        )
    return result


def _glyph_width(glyph: Glyph) -> float:
    count = sum(not unicodedata.combining(character) for character in glyph.text)
    return max(
        count * 6,
        12 if glyph.underlines else 6,
        max((len(chord) * 5.5 for chord in glyph.chords), default=0),
    )


def _measure_columns(
    voices: tuple[list[Glyph], list[Glyph]],
) -> tuple[list[Fraction], list[float]]:
    positions = sorted({glyph.offset for voice in voices for glyph in voice})
    widths = [
        max(
            _glyph_width(glyph)
            for voice in voices
            for glyph in voice
            if glyph.offset == offset
        )
        for offset in positions
    ]
    return positions, widths


def _measure_width(voices: tuple[list[Glyph], list[Glyph]]) -> float:
    _, widths = _measure_columns(voices)
    chord_width = max(
        (
            len(chord) * 5 + 8
            for voice in voices
            for glyph in voice
            for chord in glyph.chords
        ),
        default=0,
    )
    return max(36, sum(widths) + 5 * len(widths) + 8, chord_width)


def _column_positions(
    voices: tuple[list[Glyph], list[Glyph]], width: float
) -> dict[Fraction, float]:
    offsets, widths = _measure_columns(voices)
    extra = (width - 8 - sum(widths)) / len(offsets)
    cursor = 4.0
    positions = {}
    for offset, glyph_width in zip(offsets, widths, strict=True):
        positions[offset] = cursor + (glyph_width + extra) / 2
        cursor += glyph_width + extra
    return positions


def _arc(start: float, end: float, y: float, kind: str) -> str:
    return (
        f'<path class="{kind}" d="M {start:.2f} {y} '
        f'Q {(start + end) / 2:.2f} {y - 9} {end:.2f} {y}" '
        'fill="none" stroke="#111" stroke-width="0.8"/>'
    )


def _tuplet_svg(left: float, right: float, count: int) -> str:
    return (
        f'<path class="tuplet" d="M {left:.2f} 7 V 3 H {right:.2f} V 7" '
        'fill="none" stroke="#111"/>'
        f'<text class="meter" x="{(left + right) / 2:.2f}" '
        f'y="0" text-anchor="middle">{count}</text>'
    )


def _glyph_svg(glyph: Glyph, x: float) -> str:
    upper, lower = glyph.text.count("̇"), glyph.text.count("̣")
    text = glyph.text.replace("̇", "").replace("̣", "")
    if not (upper or lower):
        return f'<text x="{x:.2f}" y="30" text-anchor="middle">{escape(text)}</text>'
    digit = next(
        index for index, character in enumerate(text) if character in "1234567"
    )
    parts = [f'<text x="{x:.2f}" y="30" text-anchor="middle">{text[digit]}</text>']
    if text[:digit]:
        parts.append(
            f'<text x="{x - 4:.2f}" y="30" text-anchor="end">{escape(text[:digit])}</text>'
        )
    if text[digit + 1 :]:
        parts.append(
            f'<text x="{x + 4:.2f}" y="30" text-anchor="start">{escape(text[digit + 1 :])}</text>'
        )
    for count, y, step in ((upper, 17, -4), (lower, 33, 4)):
        for index in range(count):
            parts.append(
                f'<circle class="octave-dot" cx="{x:.2f}" cy="{y + index * step}" r="1" fill="#111"/>'
            )
    return "".join(parts)


def _underlines_svg(
    glyphs: list[Glyph],
    column_x: dict[Fraction, float],
    start: float,
    meter: tuple[int, int],
) -> list[str]:
    # Compound meters group three denominator units into one beat.
    numerator, denominator = meter
    beat = Fraction(3 if numerator > 3 and numerator % 3 == 0 else 1, denominator)
    items = []
    underline_y = max(38, 34 + 4 * max((g.text.count("̣") for g in glyphs), default=0))
    for level in range(max((glyph.underlines for glyph in glyphs), default=0)):
        left = right = None
        previous = None
        for glyph in glyphs:
            eligible = (
                glyph.underlines > level
                and bool(glyph.text)
                and not glyph.text.startswith("0")
            )
            connected = (
                eligible
                and previous is not None
                and previous.offset + previous.duration == glyph.offset
                and previous.offset // beat == glyph.offset // beat
                and not previous.tuplet_end
                and not glyph.tuplet
            )
            if left is not None and not connected:
                y = underline_y + level * 4
                items.append(
                    f'<line class="underline" x1="{left:.2f}" x2="{right:.2f}" '
                    f'y1="{y}" y2="{y}" stroke="#111"/>'
                )
                left = right = None
            if glyph.underlines > level:
                x = start + column_x[glyph.offset]
                if left is None:
                    left = x - 6
                right = x + 6
            previous = glyph if eligible else None
        if left is not None:
            y = underline_y + level * 4
            items.append(
                f'<line class="underline" x1="{left:.2f}" x2="{right:.2f}" '
                f'y1="{y}" y2="{y}" stroke="#111"/>'
            )
    return items


def _voice_svg(
    groups: list[list[Glyph]],
    positions: list[dict[Fraction, float]],
    starts: list[float],
    widths: list[float],
    measures: list[AbcMeasure],
    label: str,
    previous_meter: tuple[int, int],
    previous_key: str,
    open_slurs: set[int],
    open_tuplet: list[int],
    *,
    show_changes: bool = True,
) -> str:
    items = (
        [f'<text class="voice-label" x="-38" y="30">{label}</text>'] if label else []
    )
    pending_tie = None
    slurs = {slur: MUSIC_START_X for slur in open_slurs}
    tuplet = (MUSIC_START_X, open_tuplet[0]) if open_tuplet else None
    upper_dots = max((g.text.count("̇") for group in groups for g in group), default=0)
    tie_y = min(18, 14 - 4 * (upper_dots - 1))
    slur_y = min(12, tie_y - 6)
    for glyphs, column_x, start, width, measure in zip(
        groups, positions, starts, widths, measures, strict=True
    ):
        if show_changes and measure.meter != previous_meter:
            items.append(
                f'<text class="meter" x="{start + 4}" y="-6">{measure.meter[0]}/{measure.meter[1]}</text>'
            )
        previous_meter = measure.meter
        if show_changes and measure.key != previous_key:
            items.append(
                f'<text class="meter" x="{start + 4}" y="-18">{escape(_key_label(measure.key))}</text>'
            )
        previous_key = measure.key
        items.extend(_underlines_svg(glyphs, column_x, start, measure.meter))
        for glyph in glyphs:
            x = start + column_x[glyph.offset]
            items.append(_glyph_svg(glyph, x))
            for chord in glyph.chords:
                items.append(
                    f'<text class="chord" x="{x:.2f}" y="0" text-anchor="middle">{escape(chord)}</text>'
                )
            if glyph.key_change and glyph.key_change != previous_key and show_changes:
                items.append(
                    f'<text class="meter" x="{x:.2f}" y="-18">{escape(_key_label(glyph.key_change))}</text>'
                )
            if glyph.key_change:
                previous_key = glyph.key_change
            if glyph.tie_in:
                items.append(
                    _arc(
                        pending_tie if pending_tie is not None else MUSIC_START_X,
                        x,
                        tie_y,
                        "tie",
                    )
                )
            if glyph.tie_out:
                pending_tie = x
            elif glyph.tie_in:
                pending_tie = None
            for slur in glyph.slur_starts:
                slurs[slur] = x
            for slur in glyph.slur_ends:
                if slur not in slurs:
                    raise ValueError("ABC 连音线缺少起点")
                items.append(_arc(slurs.pop(slur), x, slur_y, "slur"))
            if glyph.tuplet:
                tuplet = (x, glyph.tuplet)
            if glyph.tuplet_end and tuplet:
                left, count = tuplet
                items.append(_tuplet_svg(left, x, count))
                tuplet = None
        bar_x = start + width
        items.append(
            f'<line class="bar" x1="{bar_x:.2f}" x2="{bar_x:.2f}" y1="5" y2="46" stroke="#222"/>'
        )
        if measure.bar in {"bar_thin_thin", "bar_thin_thick"}:
            weight = 2 if measure.bar == "bar_thin_thick" else 1
            items.append(
                f'<line class="bar" x1="{bar_x - 3:.2f}" x2="{bar_x - 3:.2f}" y1="5" y2="46" stroke="#222" stroke-width="{weight}"/>'
            )
    right = starts[-1] + widths[-1]
    if pending_tie is not None:
        items.append(_arc(pending_tie, right, tie_y, "tie"))
    open_tuplet.clear()
    if tuplet:
        items.append(_tuplet_svg(tuplet[0], right, tuplet[1]))
        open_tuplet.append(tuplet[1])
    for left in slurs.values():
        items.append(_arc(left, right, slur_y, "slur"))
    open_slurs.clear()
    open_slurs.update(slurs)
    return '<g class="vocal-system">' + "".join(items) + "</g>"


def _key_label(name: str) -> str:
    root, mode = name.split(":")
    if mode == "minor":
        relative = {
            "A": "C",
            "E": "G",
            "B": "D",
            "F#": "A",
            "C#": "E",
            "G#": "B",
            "D#": "F#",
            "A#": "C#",
            "D": "F",
            "G": "Bb",
            "C": "Eb",
            "F": "Ab",
            "Bb": "Db",
            "Eb": "Gb",
            "Ab": "Cb",
        }
        return f"1={relative[root]}（{root} 小调）"
    return f"1={root} 大调"


def _build_pages(score: AbcScore, title: str, *, show_vocal: bool = True) -> list[str]:
    vocal = [
        [glyph for note in measure.notes for glyph in _note_glyphs(note)]
        for measure in score.vocal
    ]
    instrumental = [
        [glyph for note in measure.notes for glyph in _note_glyphs(note)]
        for measure in score.instrumental
    ]
    for vocal_glyphs, ins_glyphs in zip(vocal, instrumental, strict=True):
        for glyph in vocal_glyphs:
            if glyph.chords:
                existing = next(
                    (g for g in ins_glyphs if g.offset == glyph.offset), None
                )
                if existing is None:
                    ins_glyphs.append(Glyph(glyph.offset, "", 0, chords=glyph.chords))
                else:
                    index = ins_glyphs.index(existing)
                    ins_glyphs[index] = replace(
                        existing,
                        chords=tuple(dict.fromkeys(existing.chords + glyph.chords)),
                    )
        vocal_glyphs[:] = [replace(glyph, chords=()) for glyph in vocal_glyphs]
        ins_glyphs.sort(key=lambda glyph: glyph.offset)
    voices = list(
        zip(vocal if show_vocal else [[] for _ in vocal], instrumental, strict=True)
    )
    minimums = [_measure_width(pair) for pair in voices]
    systems = []
    start = 0
    previous_meter = score.vocal[0].meter
    previous_key = score.vocal[0].key
    slurs = (set(), set())
    tuplets = ([], [])
    while start < len(voices):
        # Prefer 8/6/4 per line; exceptionally dense ABC can use 2/1 without losing notes.
        boundary = next(
            (
                i
                for i in range(start + 1, len(voices))
                if score.vocal[i].key != score.vocal[start].key
                or score.vocal[i].section
                or score.instrumental[i].section
            ),
            len(voices),
        )
        count = next(
            (
                min(size, boundary - start)
                for size in (8, 6, 4, 2, 1)
                if sum(minimums[start : start + min(size, boundary - start)])
                <= SYSTEM_WIDTH
            ),
            0,
        )
        if not count:
            raise ValueError("ABC 单小节音符过密，简谱行无法容纳")
        end = start + count
        total = sum(minimums[start:end])
        # A short section or final row keeps its share of an eight-bar line.
        # Dense bars retain the space required by their notation.
        row_width = (
            max(total, SYSTEM_WIDTH * count / 8) if end == boundary else SYSTEM_WIDTH
        )
        widths = [width * row_width / total for width in minimums[start:end]]
        starts = [MUSIC_START_X]
        for width in widths[:-1]:
            starts.append(starts[-1] + width)
        positions = [
            _column_positions(pair, width)
            for pair, width in zip(voices[start:end], widths, strict=True)
        ]
        rows = [
            _voice_svg(
                groups[start:end],
                positions,
                starts,
                widths,
                measures[start:end],
                label if show_vocal else "",
                previous_meter,
                previous_key,
                open_slurs,
                open_tuplet,
                show_changes=label == "voc" or not show_vocal,
            )
            for groups, measures, label, open_slurs, open_tuplet in (
                (vocal, score.vocal, "voc", slurs[0], tuplets[0]),
                (instrumental, score.instrumental, "ins", slurs[1], tuplets[1]),
            )
            if show_vocal or label == "ins"
        ]
        music = (
            f'{rows[0]}<g transform="translate(0,64)">{rows[1]}</g>'
            if show_vocal
            else rows[0]
        )
        section_labels = []
        for index, x in zip(range(start, end), starts, strict=True):
            section = score.vocal[index].section or score.instrumental[index].section
            if section:
                section_labels.append(
                    f'<text class="section-label" x="{x:.2f}" y="10">{escape(section)}</text>'
                )
        section_height = 22 if section_labels else 0
        section_label = "".join(section_labels)
        systems.append(
            (
                (
                    f'<g class="combined-system">{section_label}'
                    f'<g transform="translate(0,{section_height})">{music}</g></g>'
                ),
                (116 if show_vocal else 52) + section_height,
            )
        )
        previous_meter = score.vocal[end - 1].meter
        previous_key = next(
            (
                glyph.key_change
                for glyph in reversed(vocal[end - 1])
                if glyph.key_change
            ),
            score.vocal[end - 1].key,
        )
        start = end
    if any(tuplets):
        raise ValueError("ABC 连音组缺少终点")
    if any(slurs):
        raise ValueError("ABC 连音线缺少终点")
    meter = score.vocal[0].meter
    return _paginate(
        systems, score.vocal[0].key, f"{meter[0]}/{meter[1]}", title, score.tempo
    )


def _paginate(
    systems: list[tuple[str, float]],
    key_name: str,
    initial_meter: str,
    title: str,
    tempo: str,
) -> list[str]:
    pages: list[list[tuple[str, float]]] = []
    current: list[tuple[str, float]] = []
    used = 72.0
    for system in systems:
        if current and used + system[1] + SYSTEM_GAP > 1100:
            pages.append(current)
            current = []
            used = 72.0
        current.append(system)
        used += system[1] + SYSTEM_GAP
    if current:
        pages.append(current)
    return [
        _page_svg(page, key_name, initial_meter, title if index == 0 else "", tempo)
        for index, page in enumerate(pages)
    ]


def _page_svg(
    systems: list[tuple[str, float]],
    key_name: str,
    initial_meter: str,
    title: str,
    tempo: str,
) -> str:
    body = []
    y = 72.0
    for system, height in systems:
        body.append(f'<g transform="translate(44,{y:.2f})">{system}</g>')
        y += height + SYSTEM_GAP
    safe_key = escape(_key_label(key_name))
    tempo_label = f"　♩ = {escape(tempo)}" if tempo else ""
    title_text = (
        f'<text class="title" x="397" y="42" text-anchor="middle">{escape(title)}</text>'
        if title
        else ""
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="794" height="1123" viewBox="0 0 794 1123">'
        '<rect width="794" height="1123" fill="white"/>'
        '<style>text{font-family:"Noto Sans CJK SC","PingFang SC",sans-serif;font-size:18px}.title{font-size:24px;font-weight:600}.score-meta{font-size:13px}.section-label{font-size:12px;font-weight:700}.voice-label{font-size:11px;font-weight:700}.accidental,.meter{font-size:11px}.chord{font-size:10px;font-weight:600}.vocal-system text:not(.voice-label):not(.meter):not(.chord){font-size:11px}</style>'
        f"{title_text}"
        f'<text class="score-meta" x="20" y="58">{safe_key}　拍号：{initial_meter}{tempo_label}</text>'
        f"{''.join(body)}</svg>"
    )


def _write_pages(
    pages: list[str], destination: Path, formats: tuple[str, ...]
) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {item: [] for item in formats}
    svg_paths = []
    for index, content in enumerate(pages, 1):
        path = destination / f"score_{index:03d}.svg"
        path.write_text(content, encoding="utf-8")
        svg_paths.append(path)
    if "svg" in formats:
        result["svg"] = [str(path) for path in svg_paths]
    if set(formats) & {"pdf", "png"}:
        _convert_pages(pages, destination, formats, result)
    if "svg" not in formats:
        for path in svg_paths:
            path.unlink()
    return result


def _convert_pages(
    pages: list[str],
    destination: Path,
    formats: tuple[str, ...],
    result: dict[str, list[str]],
) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright，无法渲染简谱") from exc
    document = (
        "<html><style>@page{size:A4;margin:0}body{margin:0}.page{page-break-after:always;width:794px;height:1123px}.page:last-child{page-break-after:auto}</style><body>"
        + "".join(f'<div class="page">{page}</div>' for page in pages)
        + "</body></html>"
    )
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--disable-gpu"])
        try:
            page = browser.new_page(viewport={"width": 794, "height": 1123})
            page.set_content(document)
            if "pdf" in formats:
                path = destination / "score.pdf"
                page.pdf(
                    path=str(path),
                    width="794px",
                    height="1123px",
                    print_background=True,
                )
                result["pdf"] = [str(path)]
            if "png" in formats:
                paths = []
                for index, locator in enumerate(page.locator(".page").all(), 1):
                    path = destination / f"score_{index:03d}.png"
                    locator.screenshot(path=str(path))
                    paths.append(str(path))
                result["png"] = paths
        finally:
            browser.close()
