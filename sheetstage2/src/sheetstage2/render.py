from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from .abc_score import rendering_assets
from .numbered_notation import render_numbered_abc
from .service import normalize_voice_labels


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="渲染已有的 SheetSage2 转录结果。")
    parser.add_argument("--input", help="转录结果目录")
    parser.add_argument("--midi", help="保留原始音符时间的 MIDI 文件")
    parser.add_argument("--abc", help="ABC 乐谱文件")
    parser.add_argument("--output", help="输出目录；默认使用 --input 或 rendered/")
    parser.add_argument("--title", help="乐谱标题；默认根据输入结果推断")
    parser.add_argument("--audio", action="store_true", help="渲染钢琴 WAV")
    parser.add_argument("--score", default="", help="逗号分隔的 pdf,svg,png")
    parser.add_argument(
        "--parts",
        default="mix",
        help="逗号分隔的 mix,melody,vocal,instrumental,chords,all",
    )
    parser.add_argument("--duration", type=float, help="最短音频时长（秒）")
    parser.add_argument(
        "--vocal-notation",
        choices=("staff", "numbered"),
        default="staff",
        help="记谱方式；默认 staff，numbered 将人声和器乐都转为数字简谱",
    )
    parser.add_argument(
        "--show-vocal", action="store_true", help="在数字简谱中显示人声，默认隐藏"
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    renderer: Callable[..., dict[str, Any]] | None = None,
    numbered_renderer: Callable[..., dict[str, Any]] = render_numbered_abc,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    audio = args.audio
    score = args.score
    if not audio and not score:
        if args.input:
            audio, score = True, "pdf"
        else:
            audio, score = bool(args.midi), "pdf" if args.abc else ""
    if not args.input and not args.midi and not args.abc:
        parser.error("请提供 --input、--midi 或 --abc。")

    if args.vocal_notation == "numbered" and score and not (args.input or args.abc):
        parser.error("数字简谱需要通过 --input 或 --abc 提供 ABC 乐谱。")
    title = (
        " ".join(args.title.split())
        if args.title
        else _result_title(args.input, args.abc)
    )
    render_outputs = renderer
    if args.vocal_notation == "staff" or audio:
        render_outputs = render_outputs or _load_renderer()
    try:
        if args.vocal_notation == "numbered":
            result = {"audio": {}, "score": {}, "warnings": []}
            if audio:
                assert render_outputs is not None
                rendered_audio = render_outputs(
                    args.input,
                    midi=args.midi,
                    output_dir=args.output,
                    audio=True,
                    score="",
                    parts=args.parts,
                    duration=args.duration,
                )
                result.update(rendered_audio)
            if score:
                source_abc = args.abc or str(Path(args.input) / "score.abc")
                result["score"] = numbered_renderer(
                    source_abc,
                    output_dir=args.output,
                    formats=score,
                    title=title,
                    show_vocal=args.show_vocal,
                )
        else:
            assert render_outputs is not None
            with _normalized_abc(
                args.input, args.abc, enabled=bool(score), title=title
            ) as abc:
                result = render_outputs(
                    args.input,
                    midi=args.midi,
                    abc=abc,
                    output_dir=args.output,
                    audio=audio,
                    score=score,
                    parts=args.parts,
                    duration=args.duration,
                )
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"渲染失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


@contextmanager
def _normalized_abc(
    input_dir: str | None, abc: str | None, *, enabled: bool, title: str
):
    source = Path(abc) if abc else Path(input_dir) / "score.abc" if input_dir else None
    if not enabled or source is None or not source.is_file():
        yield abc
        return
    original = source.read_text(encoding="utf-8")
    normalized = normalize_voice_labels(original)
    normalized = _set_abc_title(normalized, title)
    if "%%barsperstaff" not in normalized:
        normalized = normalized.replace("X:1\n", "X:1\n%%barsperstaff 4\n", 1)
    if normalized == original:
        yield abc
        return
    with TemporaryDirectory(prefix="sheetstage2-render-") as directory:
        temporary = Path(directory) / "score.abc"
        temporary.write_text(normalized, encoding="utf-8")
        yield str(temporary)


def _result_title(input_dir: str | None, abc: str | None) -> str:
    if input_dir:
        directory = Path(input_dir).expanduser()
        manifest = directory / "manifest.json"
        if manifest.is_file():
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            source = payload.get("source")
            if isinstance(source, str) and source:
                return Path(source).stem
        return directory.name.rsplit("-", 1)[0]
    return Path(abc).stem if abc else "SheetSage2"


def _set_abc_title(score: str, title: str) -> str:
    lines = score.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("T:"):
            lines[index] = f"T:{title}"
            break
    return "\n".join(lines) + ("\n" if score.endswith("\n") else "")


def _load_renderer() -> Callable[..., dict[str, Any]]:
    official_assets = rendering_assets()
    snapshot = official_assets.parent
    module_path = snapshot / "rendering_sheetsage2.py"
    spec = importlib.util.spec_from_file_location(
        "sheetstage2_official_rendering", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载官方渲染模块：{module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def render_outputs(*args: Any, **kwargs: Any) -> dict[str, Any]:
        with TemporaryDirectory(prefix="sheetstage2-assets-") as directory:
            assets = Path(directory)
            for source in official_assets.iterdir():
                if source.name in {"renderer.js", "manifest.json"}:
                    continue
                os.symlink(
                    source, assets / source.name, target_is_directory=source.is_dir()
                )
            renderer = _compact_renderer(
                (official_assets / "renderer.js").read_text(encoding="utf-8")
            )
            (assets / "renderer.js").write_text(renderer, encoding="utf-8")
            manifest = json.loads(
                (official_assets / "manifest.json").read_text(encoding="utf-8")
            )
            manifest["files"]["renderer.js"] = hashlib.sha256(
                renderer.encode("utf-8")
            ).hexdigest()
            (assets / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            return module.render_outputs(*args, assets_dir=assets, **kwargs)

    return render_outputs


def _compact_renderer(script: str) -> str:
    replacements = {
        'source.style.cssText = "position:absolute;left:-2000px;width:714px";': 'source.style.cssText = "position:absolute;left:-2000px;width:754px";',
        "staffwidth: 674, paddingtop: 12, paddingbottom: 12, paddingleft: 20, paddingright: 20,": "staffwidth: 734, paddingtop: 8, paddingbottom: 8, paddingleft: 10, paddingright: 10,",
        "wrap: {minSpacing: 1.8, maxSpacing: 2.7, preferredMeasuresPerLine: 4},": "wrap: undefined,",
        'content.setAttribute("transform", "translate(40 40)");': 'content.setAttribute("transform", "translate(20 24)");',
        "const scale = Math.min(1, 714 / box.width);": "const scale = Math.min(1, 754 / box.width);",
        "if (height > 1043)": "if (height > 1075)",
        "if (!page || used + height > 1043)": "if (!page || used + height > 1075)",
        "const tunes = ABCJS.renderAbc(source, expandRests(abc), {": "const tuneTexts = expandRests(abc).split(/\\n\\s*\\n(?=X:)/);\n"
        "  const tunes = [];\n"
        "  const renderOptions = {",
        "    afterParsing: fixSystemKeys,\n  });\n  if (!tunes.length": "    afterParsing: fixSystemKeys,\n  };\n"
        "  for (const text of tuneTexts) {\n"
        "    const holder = document.createElement('div');\n"
        "    source.append(holder);\n"
        "    tunes.push(...ABCJS.renderAbc(holder, text, renderOptions));\n"
        "  }\n"
        "  if (!tunes.length",
    }
    for original, replacement in replacements.items():
        if original not in script:
            raise RuntimeError("官方渲染器结构已变化，无法安全应用紧凑版式")
        script = script.replace(original, replacement)
    return script


if __name__ == "__main__":
    raise SystemExit(main())
