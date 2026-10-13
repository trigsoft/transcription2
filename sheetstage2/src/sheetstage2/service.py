from __future__ import annotations

import gc
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings

LOGGER = logging.getLogger(__name__)

EXPECTED_OUTPUTS = (
    "score.abc",
    "transcription.mid",
    "melody_vocal.mid",
    "melody_instrumental.mid",
    "chords.mid",
    "events.json",
)


@dataclass(frozen=True)
class TranscriptionResult:
    output_dir: Path
    vocal_melody: Path
    instrumental_melody: Path
    chords: Path
    combined_midi: Path
    score: Path
    events: Path

    def as_dict(self) -> dict[str, str]:
        return {
            "output_dir": str(self.output_dir),
            "vocal_melody": str(self.vocal_melody),
            "instrumental_melody": str(self.instrumental_melody),
            "chords": str(self.chords),
            "combined_midi": str(self.combined_midi),
            "score": str(self.score),
            "events": str(self.events),
        }


@dataclass(frozen=True)
class ModelOptions:
    model: str = "m-a-p/SheetSage2"
    revision: str | None = None
    device: str = "auto"
    local_files_only: bool = False


@dataclass(frozen=True)
class TranscriptionOptions:
    dtype: str = "bf16"
    preset: str = "default"
    max_seconds: float | None = None
    overlap_seconds: float | None = None
    lookahead_seconds: float | None = None
    prompts: tuple[str, ...] | None = None
    melody_only: bool = False
    export_logits: bool = False
    export_scores: bool = False
    export_embeddings: bool = False
    output_hidden_states: bool = False
    render_audio: bool = False
    render_score: str | bool = False
    render_parts: tuple[str, ...] = ("mix",)


class ModelLoadError(RuntimeError):
    """SheetSage2 模型无法加载。"""


class SheetSage2Service:
    def __init__(
        self,
        settings: Settings | None = None,
        model_loader: Callable[[Settings, ModelOptions], Any] | None = None,
        model_options: ModelOptions | None = None,
    ) -> None:
        self.settings = settings or Settings.from_environment()
        self._model_loader = model_loader or _load_model
        self.model_options = model_options or ModelOptions(
            model=self.settings.model_id
        )
        self._model: Any | None = None

    def transcribe(
        self,
        audio_path: str | Path,
        output_dir: str | Path | None = None,
        *,
        options: TranscriptionOptions | None = None,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> TranscriptionResult:
        options = options or TranscriptionOptions()
        source = Path(audio_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"找不到音频文件：{source}")

        self.settings.prepare()
        destination = (
            Path(output_dir).expanduser().resolve()
            if output_dir is not None
            else self._default_output_dir(source)
        )
        if destination.exists() and any(destination.iterdir()):
            raise FileExistsError(f"输出目录不是空目录：{destination}")
        destination.mkdir(parents=True, exist_ok=True)

        LOGGER.info("开始转录：%s", source.name)
        model = self._get_model()

        def report_progress(value: dict[str, Any]) -> None:
            _report_progress(value)
            if progress is not None:
                progress(value)

        try:
            raw_result = model.transcribe(
                str(source),
                output_dir=str(destination),
                **_transcription_kwargs(options, report_progress),
            )
            if options.melody_only and (
                raw_result.get("abc_error") or not raw_result.get("abc")
            ):
                reason = raw_result.get("abc_error") or "未生成 ABC 乐谱"
                raise RuntimeError(f"melody-only ABC 不可用：{reason}")
            _normalize_voice_labels(destination / "score.abc")
            result = _validate_outputs(
                destination,
                melody_only=options.melody_only,
                custom_prompts=options.prompts is not None,
            )
            _write_manifest(source, result, self.model_options, options)
        except Exception:
            LOGGER.exception("转录失败：%s", source.name)
            raise
        LOGGER.info("转录完成：%s", destination)
        return result

    def release_model(self) -> None:
        """Release cached weights before another model uses the same GPU."""
        if self._model is None:
            return
        self._model = None
        gc.collect()
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        LOGGER.info("已释放 SheetSage2 模型内存")

    def _get_model(self) -> Any:
        if self._model is None:
            LOGGER.info("正在加载模型：%s", self.model_options.model)
            try:
                self._model = self._model_loader(self.settings, self.model_options)
            except Exception as exc:
                raise ModelLoadError("SheetSage2 模型加载失败") from exc
        return self._model

    def _default_output_dir(self, source: Path) -> Path:
        digest = hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:10]
        return self.settings.results_dir / f"{source.stem}-{digest}"


def _load_model(settings: Settings, options: ModelOptions) -> Any:
    try:
        import torch
        from transformers import AutoModel
    except ImportError as exc:
        raise RuntimeError("依赖未安装，请先运行：uv sync") from exc

    torch.set_num_threads(min(4, torch.get_num_threads()))
    device = (
        "cuda"
        if options.device == "auto" and torch.cuda.is_available()
        else "cpu"
        if options.device == "auto"
        else options.device
    )
    revision = options.revision or _local_model_revision(settings, options)
    model = AutoModel.from_pretrained(
        options.model,
        revision=revision,
        code_revision=revision,
        local_files_only=options.local_files_only,
        trust_remote_code=True,
        cache_dir=str(settings.model_cache_dir),
    )
    return model.eval().to(device)


def _local_model_revision(settings: Settings, options: ModelOptions) -> str | None:
    if not options.local_files_only or options.revision or "/" not in options.model:
        return None
    snapshots = (
        settings.model_cache_dir
        / f"models--{options.model.replace('/', '--')}"
        / "snapshots"
    )
    if not snapshots.is_dir():
        return None
    complete = [path for path in snapshots.iterdir() if (path / "config.json").is_file()]
    if not complete:
        return None
    return max(complete, key=lambda path: path.stat().st_mtime).name


def _transcription_kwargs(
    options: TranscriptionOptions,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "dtype": options.dtype,
        "preset": options.preset,
        "max_seconds": options.max_seconds,
        "overlap_seconds": options.overlap_seconds,
        "lookahead_seconds": options.lookahead_seconds,
        "export_logits": options.export_logits,
        "export_scores": options.export_scores,
        "export_embeddings": options.export_embeddings,
        "output_hidden_states": options.output_hidden_states,
        "render_audio": options.render_audio,
        "render_score": options.render_score,
        "render_parts": options.render_parts,
        "progress": progress or _report_progress,
    }
    if options.prompts is not None:
        kwargs["prompts"] = options.prompts
    if options.melody_only:
        kwargs["melody_only"] = True
    return kwargs


def _report_progress(value: dict[str, Any]) -> None:
    if value.get("stage") == "encoding":
        LOGGER.info("处理窗口 %s/%s", value.get("window"), value.get("windows"))


def _normalize_voice_labels(score_path: Path) -> None:
    if not score_path.is_file():
        return
    score = normalize_voice_labels(score_path.read_text(encoding="utf-8"))
    score_path.write_text(score, encoding="utf-8")


def normalize_voice_labels(score: str) -> str:
    score = score.replace(
        'V: Vocal clef=treble name="Vocal Melody" snm="Vocal"',
        'V: Vocal clef=treble name="Vocal" snm="Vocal"',
    )
    score = score.replace(
        'V: Ins clef=treble name="Ins Melody" snm="Inst."',
        'V: Ins clef=treble name="Instru." snm="Instru."',
    )
    return score


def _validate_outputs(
    output_dir: Path, *, melody_only: bool, custom_prompts: bool
) -> TranscriptionResult:
    required = set(EXPECTED_OUTPUTS)
    if melody_only:
        required.remove("chords.mid")
    if custom_prompts:
        required.clear()
    missing = sorted(name for name in required if not (output_dir / name).is_file())
    if missing:
        raise RuntimeError(f"SheetSage2 输出不完整，缺少：{', '.join(missing)}")
    return TranscriptionResult(
        output_dir=output_dir,
        vocal_melody=output_dir / "melody_vocal.mid",
        instrumental_melody=output_dir / "melody_instrumental.mid",
        chords=output_dir / "chords.mid",
        combined_midi=output_dir / "transcription.mid",
        score=output_dir / "score.abc",
        events=output_dir / "events.json",
    )


def _write_manifest(
    source: Path,
    result: TranscriptionResult,
    model: ModelOptions,
    options: TranscriptionOptions,
) -> None:
    payload = {
        "source": str(source),
        "model": model.model,
        "revision": model.revision,
        "device": model.device,
        "local_files_only": model.local_files_only,
        "options": {
            "dtype": options.dtype,
            "preset": options.preset,
            "max_seconds": options.max_seconds,
            "overlap_seconds": options.overlap_seconds,
            "lookahead_seconds": options.lookahead_seconds,
            "prompts": options.prompts,
            "melody_only": options.melody_only,
            "export_logits": options.export_logits,
            "export_scores": options.export_scores,
            "export_embeddings": options.export_embeddings,
            "output_hidden_states": options.output_hidden_states,
            "render_audio": options.render_audio,
            "render_score": options.render_score,
            "render_parts": options.render_parts,
        },
        "outputs": result.as_dict(),
    }
    (result.output_dir / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
