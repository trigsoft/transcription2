from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from .service import ModelOptions, SheetSage2Service, TranscriptionOptions

RENDER_PARTS = {"mix", "melody", "vocal", "instrumental", "chords", "all"}
RENDER_FORMATS = {"pdf", "svg", "png"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sheetstage2",
        description="使用 SheetSage2 从整首歌曲提取人声旋律、器乐旋律与和弦。",
    )
    parser.add_argument("audio", type=Path, help="输入音频文件")
    parser.add_argument("--output", type=Path, help="输出目录；默认写入外置数据盘")
    parser.add_argument("--model", default="m-a-p/SheetSage2", help="模型 ID 或本地目录")
    parser.add_argument("--revision", help="Hugging Face 模型及代码版本")
    parser.add_argument("--device", default="auto", help="auto、cpu、cuda 等 Torch 设备")
    parser.add_argument("--dtype", choices=("bf16", "fp32"), default="bf16")
    parser.add_argument(
        "--preset", choices=("default", "paper"), default="default"
    )
    parser.add_argument("--max-seconds", type=float, help="最多处理的音频秒数")
    parser.add_argument("--overlap", type=float, help="相邻窗口重叠秒数")
    parser.add_argument("--lookahead", type=float, help="窗口向右查看秒数")
    parser.add_argument("--prompts", nargs="+", help="指定一个或多个任务 prompt")
    parser.add_argument(
        "--melody-only",
        action="store_true",
        help="保留人声和器乐旋律，但不导出和弦伴奏",
    )
    parser.add_argument("--export-logits", action="store_true")
    parser.add_argument("--export-scores", action="store_true")
    parser.add_argument("--export-embeddings", action="store_true")
    parser.add_argument(
        "--all-layers", action="store_true", help="导出全部 24 层 MERT 特征"
    )
    parser.add_argument("--render-audio", action="store_true", help="渲染钢琴 WAV")
    parser.add_argument(
        "--render-score",
        nargs="?",
        const="pdf",
        default=False,
        help="渲染 pdf,svg,png；省略格式时默认 pdf",
    )
    parser.add_argument(
        "--render-parts",
        default="mix",
        help="mix,melody,vocal,instrumental,chords,all",
    )
    parser.add_argument("--local-files-only", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    render_parts = tuple(part.strip() for part in args.render_parts.split(",") if part.strip())
    unknown_parts = set(render_parts) - RENDER_PARTS
    if unknown_parts:
        parser.error(f"未知 render part：{', '.join(sorted(unknown_parts))}")
    if args.render_score:
        render_formats = {
            item.strip() for item in args.render_score.split(",") if item.strip()
        }
        unknown_formats = render_formats - RENDER_FORMATS
        if unknown_formats:
            parser.error(
                f"未知 render score 格式：{', '.join(sorted(unknown_formats))}"
            )
    model_options = ModelOptions(
        model=args.model,
        revision=args.revision,
        device=args.device,
        local_files_only=args.local_files_only,
    )
    options = TranscriptionOptions(
        dtype=args.dtype,
        preset=args.preset,
        max_seconds=args.max_seconds,
        overlap_seconds=args.overlap,
        lookahead_seconds=args.lookahead,
        prompts=tuple(args.prompts) if args.prompts else None,
        melody_only=args.melody_only,
        export_logits=args.export_logits,
        export_scores=args.export_scores,
        export_embeddings=args.export_embeddings,
        output_hidden_states=args.all_layers,
        render_audio=args.render_audio,
        render_score=args.render_score,
        render_parts=render_parts,
    )
    result = SheetSage2Service(model_options=model_options).transcribe(
        args.audio,
        output_dir=args.output,
        options=options,
    )
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
