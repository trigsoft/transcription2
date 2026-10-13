"""Run with MuScriptor's own Python environment, independent of SheetSage2."""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    from .progress import StageProgress
except ImportError:  # Direct script entry from MuScriptor's Python environment.
    from progress import StageProgress

LOGGER = logging.getLogger(__name__)
PARTS = {
    "drums": ("鼓谱", {"drums"}),
    "bass": ("贝斯谱", {"acoustic_bass", "electric_bass"}),
}


def select_events(events: list, instruments: set[str]) -> list:
    from muscriptor.events import NoteEndEvent, NoteStartEvent

    return [
        event
        for event in events
        if (isinstance(event, NoteStartEvent) and event.instrument in instruments)
        or (
            isinstance(event, NoteEndEvent)
            and event.start_event.instrument in instruments
        )
    ]


def render_part_pdf(
    midi_bytes: bytes, destination: Path, *, title: str, quantized: bool
) -> None:
    from muscriptor.utils import sheets

    midi = destination.with_suffix(".mid")
    midi.write_bytes(midi_bytes)
    options = destination.with_name(f"{destination.stem}-import.xml")
    options.write_text(sheets.import_options(quantized), encoding="utf-8")
    score = destination.with_suffix(".mscx")
    binary = sheets.find_musescore()
    completed = sheets._run(
        binary, ["-M", str(options), "-o", str(score), str(midi)]
    )
    if not score.is_file():
        sheets._fail("导入分谱 MIDI", completed)

    document = ET.parse(score)
    staff = document.find("Score/Staff")
    if staff is None:
        raise RuntimeError("分谱中没有可排版的声部")
    heading = ET.Element("VBox")
    text = ET.SubElement(heading, "Text")
    ET.SubElement(text, "style").text = "Title"
    ET.SubElement(text, "text").text = title
    staff.insert(0, heading)
    document.write(score, encoding="utf-8", xml_declaration=True)

    completed = sheets._run(binary, ["-o", str(destination), str(score)])
    if not destination.is_file():
        sheets._fail("生成分谱 PDF", completed)
    with destination.open("rb") as output:
        if output.read(5) != b"%PDF-":
            raise RuntimeError("分谱 PDF 无效")


def decode_audio(source: Path, destination: Path) -> None:
    # libsndfile cannot decode every format accepted by the web uploader.
    # Preserve the original channels/rate and let MuScriptor do its resampling.
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-map",
                "0:a:0",
                "-c:a",
                "pcm_f32le",
                str(destination),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        LOGGER.error("原始音频解码失败：%s", exc.stderr[-2000:])
        raise


def generate_parts(audio: Path, output: Path, title: str) -> None:
    from muscriptor.events import NoteStartEvent, ProgressEvent
    from muscriptor.transcription_model import TranscriptionModel
    from muscriptor.utils.sheets import find_musescore, prepare_output_dir

    prepare_output_dir(output)
    find_musescore()
    output.mkdir(parents=True, exist_ok=True)
    progress = StageProgress()

    def report(message, *, recognition=False):
        temporary = output / "progress.json.tmp"
        temporary.write_text(json.dumps({
            "message": message,
            "progress": progress.payload() if recognition else None,
            "startedMonotonic": progress.started if recognition else None,
        }, ensure_ascii=False), encoding="utf-8")
        temporary.replace(output / "progress.json")

    report("正在读取鼓和贝斯分谱音频")
    wav = output / "input.wav"
    LOGGER.info("正在解码原始音频")
    decode_audio(audio, wav)
    LOGGER.info("正在加载 MuScriptor Large")
    report("正在加载鼓和贝斯识别模型")
    model = TranscriptionModel.load_model(weights_path="large")
    LOGGER.info("正在识别鼓和贝斯")
    # Follow transcribe_and_postprocess: detect one grid and align it against
    # all detected onsets before selecting parts, so both scores stay aligned.
    report("正在分析鼓和贝斯节拍")
    grid = model.detect_beat_grid_for(wav)
    events = []
    report("正在识别鼓和贝斯")
    for event in model.transcribe(wav):
        if isinstance(event, ProgressEvent):
            progress.update(event.completed, event.total)
            report("正在识别鼓和贝斯", recognition=True)
        else:
            events.append(event)
    if grid is not None:
        grid = grid.with_onset_delay(
            [event.start_time for event in events if isinstance(event, NoteStartEvent)]
        )
    available = []
    missing = []
    for name, (label, instruments) in PARTS.items():
        selected = select_events(events, instruments)
        if not any(isinstance(event, NoteStartEvent) for event in selected):
            missing.append(label)
            LOGGER.info("未识别到%s，跳过分谱", label)
            continue
        report(f"正在排版{label}")
        LOGGER.info("正在排版%s", label)
        midi = model.events_to_midi_bytes(iter(selected), beat_grid=grid, quantize=True)
        render_part_pdf(
            midi,
            output / f"{name}.pdf",
            title=f"{title} - {label}",
            quantized=grid is not None and grid.beat_subdivision is not None,
        )
        available.append(name)
    report("正在保存鼓和贝斯分谱")
    (output / "manifest.json").write_text(
        json.dumps(
            {
                "model": "MuScriptor/muscriptor-large",
                "parts": available,
                "missing": missing,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 MuScriptor Large 鼓和贝斯分谱")
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s %(levelname)s job_id={args.job_id} %(message)s",
    )
    generate_parts(args.audio, args.output, args.title)


if __name__ == "__main__":
    main()
