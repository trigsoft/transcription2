from __future__ import annotations

import argparse
import json
import logging
import math
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from .auth import (
    DEFAULT_AUTH_DIR,
    SESSION_TTL_SECONDS,
    AuthError,
    AuthManager,
    User,
    build_mail_sender,
)
from .progress import StageProgress
from .config import Settings
from .service import ModelLoadError, ModelOptions, SheetSage2Service

LOGGER = logging.getLogger(__name__)
DEFAULT_LOG_PATH = Path.home() / "Library" / "Logs" / "SheetStage2" / "web.log"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_AUDIO_SECONDS = 360
ALLOWED_SUFFIXES = {".aac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav"}
JOB_ROUTE = re.compile(r"^/api/jobs/([0-9a-f]{32})$")
ADMIN_USER_ACTION_ROUTE = re.compile(r"^/api/admin/users/(\d+)/action$")
FILE_ROUTE = re.compile(
    r"^/api/jobs/([0-9a-f]{32})/files/(score\.pdf|drums\.pdf|bass\.pdf|score_\d{3}\.png)$"
)
PART_LABELS = {"drums": "鼓谱", "bass": "贝斯谱"}
GUEST_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")
STATUS_LABELS = {
    "queued": "等待处理",
    "transcribing": "正在识别旋律、节拍与和弦",
    "rendering": "正在排版简谱",
    "parts": "正在识别鼓和贝斯并生成分谱",
    "complete": "乐谱已生成",
    "failed": "生成失败",
}


class RenderError(RuntimeError):
    """乐谱渲染失败。"""


class AudioDurationError(ValueError):
    """音频时长不符合上传要求。"""


def validate_audio_duration(path: Path) -> None:
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "format=duration:stream=duration",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        data = json.loads(completed.stdout)
        streams = data.get("streams", [])
        if not streams:
            raise ValueError("没有音频流")
        durations = []
        for info in [data.get("format", {}), *streams]:
            value = info.get("duration")
            if value is not None and value != "N/A":
                duration = float(value)
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError("无效时长")
                durations.append(duration)
        if not durations:
            raise ValueError("缺少时长")
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
        TypeError,
        AttributeError,
    ) as exc:
        LOGGER.warning("无法确认音频时长：%s", type(exc).__name__)
        raise AudioDurationError("无法确认音频时长，请更换有效的音频文件。") from exc
    if max(durations) > MAX_AUDIO_SECONDS:
        raise AudioDurationError("音频超过 6 分钟，请选择不超过 6 分钟的歌曲。")


def job_error_message(exc: Exception) -> str:
    if isinstance(exc, ModelLoadError):
        return "模型加载失败，请联系管理员检查模型文件。"
    if isinstance(exc, RenderError):
        return str(exc)
    return "生成失败，请检查音频格式后重试。"


def render_error_message(stderr: str) -> str:
    for line in reversed(stderr.splitlines()):
        if line.startswith("渲染失败："):
            return line
    return "乐谱渲染失败，请联系管理员检查日志。"


@dataclass
class Job:
    id: str
    user_id: int | None
    guest_id: str | None
    filename: str
    upload_path: Path
    output_dir: Path
    artist: str = ""
    song_title: str = ""
    show_vocal: bool = False
    status: str = "queued"
    error: str | None = None
    pages: list[str] = field(default_factory=list)
    parts: list[str] = field(default_factory=list)
    parts_warning: str | None = None
    quota_remaining: int | None = None
    progress: tuple[int, int] | None = None
    progress_message: str | None = None
    recognition_progress: StageProgress = field(default_factory=StageProgress)

    @property
    def score_title(self) -> str:
        return f"{self.artist}-{self.song_title}"

    def payload(self, *, full_access: bool = True) -> dict[str, Any]:
        result = None
        if self.status == "complete":
            visible_pages = self.pages if full_access else self.pages[:1]
            result = {
                "pdf": f"/api/jobs/{self.id}/files/score.pdf" if full_access else None,
                "pages": [
                    f"/api/jobs/{self.id}/files/{name}" for name in visible_pages
                ],
                "lockedPageCount": 0 if full_access else max(0, len(self.pages) - 1),
                "fullAccess": full_access,
                "partPdfs": [
                    {
                        "label": PART_LABELS[name],
                        "url": f"/api/jobs/{self.id}/files/{name}.pdf",
                        "filename": pdf_filename(
                            f"{self.score_title}-{PART_LABELS[name]}"
                        ),
                    }
                    for name in self.parts
                    if full_access
                ],
                "partsWarning": self.parts_warning,
            }
        progress = self.recognition_progress.payload() if self.progress is not None else None
        message = self.progress_message or STATUS_LABELS[self.status]
        if self.status == "parts":
            try:
                report = json.loads((self.output_dir / "parts" / "progress.json").read_text())
                message = report["message"]
                progress = report.get("progress")
                started = report.get("startedMonotonic")
                if progress is not None and isinstance(started, (int, float)):
                    progress = StageProgress(
                        started=started,
                        current=progress["current"], total=progress["total"],
                    ).payload()
            except (OSError, ValueError, KeyError, TypeError):
                pass
        return {
            "id": self.id,
            "filename": self.filename,
            "artist": self.artist,
            "songTitle": self.song_title,
            "pdfFilename": pdf_filename(self.score_title),
            "status": self.status,
            "message": message,
            "progress": progress,
            "error": self.error,
            "result": result,
            "quotaRemaining": self.quota_remaining,
        }


class JobManager:
    def __init__(self, settings: Settings, auth: AuthManager) -> None:
        self.upload_dir = settings.data_dir / "web" / "uploads"
        self.results_dir = settings.data_dir / "web" / "results"
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[Job] = queue.Queue()
        self._auth = auth
        self._service = SheetSage2Service(
            settings=settings,
            model_options=ModelOptions(local_files_only=True),
        )
        threading.Thread(target=self._worker, daemon=True).start()

    def new_upload(
        self, filename: str, *, user_id: int | None, guest_id: str | None
    ) -> tuple[Job, Path]:
        clean_name, suffix = safe_upload_name(filename)
        job_id = uuid.uuid4().hex
        upload_path = self.upload_dir / f"{job_id}{suffix}"
        job = Job(
            id=job_id,
            user_id=user_id,
            guest_id=guest_id,
            filename=clean_name,
            upload_path=upload_path,
            output_dir=self.results_dir / job_id,
        )
        return job, upload_path.with_suffix(upload_path.suffix + ".upload")

    def enqueue(self, job: Job) -> None:
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        LOGGER.info("任务已入队：job_id=%s file=%s", job.id, job.filename)

    def get(
        self, job_id: str, *, user_id: int | None, guest_id: str | None
    ) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if user_id is not None and job.user_id == user_id:
                return job
            if guest_id is not None and job.guest_id == guest_id:
                return job
            return None

    def guest_has_job(self, guest_id: str) -> bool:
        with self._lock:
            return any(job.guest_id == guest_id for job in self._jobs.values())

    def update(self, job: Job, status: str, *, error: str | None = None) -> None:
        with self._lock:
            previous = job.status
            job.status = status
            job.error = error
            job.progress_message = None
            job.progress = None
            job.recognition_progress = StageProgress()
        LOGGER.info("任务状态更新：job_id=%s status=%s->%s", job.id, previous, status)

    def update_transcription_progress(self, job: Job, value: dict[str, Any]) -> None:
        stage = value.get("stage")
        window = value.get("window")
        windows = value.get("windows")
        progress: tuple[int, int] | None = None
        message = {
            "audio": "正在读取音频",
            "notation": "正在生成乐谱数据",
            "complete": "转录完成，正在准备排版",
        }.get(stage)
        if (
            stage in {"encoding", "decoding", "window_complete"}
            and isinstance(window, int)
            and isinstance(windows, int)
            and 1 <= window <= windows
        ):
            total = windows
            if stage == "encoding":
                progress = (window - 1, total)
                message = "正在分析音频"
            elif stage == "decoding":
                progress = (window - 1, total)
                message = "正在识别旋律、节拍与和弦"
            else:
                progress = (window, total)
                message = "正在识别旋律、节拍与和弦"
        with self._lock:
            if progress is not None:
                job.progress = progress
                job.recognition_progress.update(*progress)
            elif stage in {"audio", "notation", "complete"}:
                job.progress = None
            if message is not None:
                job.progress_message = message

    def _worker(self) -> None:
        while True:
            job = self._queue.get()
            started_at = time.monotonic()
            LOGGER.info("任务开始：job_id=%s file=%s", job.id, job.filename)
            try:
                self._process(job)
            except Exception as exc:
                LOGGER.exception(
                    "乐谱任务失败：job_id=%s duration=%.1fs",
                    job.id,
                    time.monotonic() - started_at,
                )
                self.update(job, "failed", error=job_error_message(exc))
                try:
                    if job.user_id is not None:
                        self._auth.release_song(job.id)
                except Exception:
                    LOGGER.exception("退回歌曲额度失败：job_id=%s", job.id)
            else:
                try:
                    if job.user_id is not None:
                        self._auth.complete_song(job.id)
                except Exception:
                    LOGGER.exception("确认歌曲额度失败：job_id=%s", job.id)
                LOGGER.info(
                    "乐谱任务完成：job_id=%s duration=%.1fs",
                    job.id,
                    time.monotonic() - started_at,
                )
            finally:
                job.upload_path.unlink(missing_ok=True)
                self._queue.task_done()

    def _process(self, job: Job) -> None:
        transcription_dir = job.output_dir / "transcription"
        render_dir = job.output_dir / "rendered"
        title = job.score_title
        self.update(job, "transcribing")
        LOGGER.info("开始转录：job_id=%s file=%s", job.id, job.filename)
        try:
            self._service.transcribe(
                job.upload_path,
                output_dir=transcription_dir,
                progress=partial(self.update_transcription_progress, job),
            )
        finally:
            if os.environ.get("SHEETSTAGE2_RELEASE_MODEL_AFTER_TRANSCRIPTION") == "1":
                self._service.release_model()

        self.update(job, "rendering")
        LOGGER.info("开始渲染：job_id=%s", job.id)
        render_dir.mkdir(parents=True, exist_ok=True)
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "sheetstage2.render",
                "--input",
                str(transcription_dir),
                "--output",
                str(render_dir),
                "--title",
                title,
                "--score",
                "pdf,png",
                "--vocal-notation",
                "numbered",
                *(["--show-vocal"] if job.show_vocal else []),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            LOGGER.error(
                "乐谱渲染失败：job_id=%s stderr=%s",
                job.id,
                completed.stderr[-4000:],
            )
            raise RenderError(render_error_message(completed.stderr))
        pages = sorted(path.name for path in render_dir.glob("score_*.png"))
        if not (render_dir / "score.pdf").is_file() or not pages:
            raise RuntimeError("乐谱输出不完整")
        with self._lock:
            job.pages = pages
        self.update(job, "parts")
        try:
            self._generate_parts(job)
        except Exception:
            LOGGER.exception("鼓和贝斯分谱生成失败：job_id=%s", job.id)
            with self._lock:
                job.parts_warning = "鼓和贝斯分谱生成失败，已保留人声和器乐乐谱。"
        self.update(job, "complete")

    def _generate_parts(self, job: Job) -> None:
        muscriptor_dir = Path(__file__).resolve().parents[3] / "muscriptor"
        python = os.environ.get(
            "SHEETSTAGE2_MUSCRIPTOR_PYTHON",
            str(muscriptor_dir / ".venv" / "bin" / "python"),
        )
        parts_dir = job.output_dir / "parts"
        LOGGER.info("开始生成鼓和贝斯分谱：job_id=%s model=large", job.id)
        completed = subprocess.run(
            [
                str(python),
                str(Path(__file__).with_name("muscriptor_parts.py")),
                "--audio",
                str(job.upload_path),
                "--output",
                str(parts_dir),
                "--title",
                job.score_title,
                "--job-id",
                job.id,
            ],
            cwd=muscriptor_dir,
            env={**os.environ, "HF_HUB_OFFLINE": "1"},
            timeout=7200,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            LOGGER.error(
                "MuScriptor 分谱失败：job_id=%s stderr=%s",
                job.id,
                completed.stderr[-4000:],
            )
            raise RuntimeError("MuScriptor 分谱失败")
        manifest = json.loads((parts_dir / "manifest.json").read_text(encoding="utf-8"))
        available = manifest["parts"]
        if any(name not in PART_LABELS for name in available):
            raise RuntimeError("MuScriptor 返回了未知分谱")
        for name in available:
            with (parts_dir / f"{name}.pdf").open("rb") as output:
                if output.read(5) != b"%PDF-":
                    raise RuntimeError("MuScriptor 分谱 PDF 无效")
        with self._lock:
            job.parts = available
            if manifest["missing"]:
                job.parts_warning = "未识别到：" + "、".join(manifest["missing"]) + "。"
        LOGGER.info("鼓和贝斯分谱完成：job_id=%s parts=%s", job.id, available)


def safe_upload_name(filename: str) -> tuple[str, str]:
    name = Path(unquote(filename)).name.strip()
    suffix = Path(name).suffix.lower()
    if not name or suffix not in ALLOWED_SUFFIXES:
        raise ValueError("请选择 MP3、WAV、FLAC、M4A、AAC、OGG 或 OPUS 音频文件。")
    return name[:180], suffix


def audio_metadata(path: Path) -> dict[str, str]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format_tags=title,artist",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if completed.returncode:
        return {"artist": "", "songTitle": ""}
    try:
        tags = json.loads(completed.stdout).get("format", {}).get("tags", {})
        tags = {key.lower(): value for key, value in tags.items()}
    except (AttributeError, TypeError, json.JSONDecodeError):
        return {"artist": "", "songTitle": ""}
    return {
        field: " ".join(tags[key].split())[:180]
        if isinstance(tags.get(key), str)
        else ""
        for field, key in (("artist", "artist"), ("songTitle", "title"))
    }


def audio_title(path: Path, filename: str) -> str:
    return audio_metadata(path)["songTitle"] or Path(filename).stem


def pdf_filename(title: str) -> str:
    return re.sub(r'[\\/:*?"<>|\x00-\x1f\x7f]', "_", title).strip(" .") + ".pdf"


def can_access_result_file(user: User | None, filename: str) -> bool:
    return user is not None or filename == "score_001.png"


class WebHandler(BaseHTTPRequestHandler):
    server_version = "SheetStage2Web/1.0"

    def __init__(
        self,
        *args: Any,
        manager: JobManager,
        auth: AuthManager,
        static_dir: Path,
        secure_cookies: bool,
        **kwargs: Any,
    ) -> None:
        self.manager = manager
        self.auth = auth
        self.static_dir = static_dir
        self.secure_cookies = secure_cookies
        super().__init__(*args, **kwargs)

    def do_HEAD(self) -> None:
        self._route(send_body=False)

    def do_GET(self) -> None:
        self._route(send_body=True)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/auth/code":
            self._send_auth_code()
            return
        if path == "/api/auth/verify":
            self._verify_auth_code()
            return
        if path == "/api/auth/admin":
            self._login_admin()
            return
        if path == "/api/auth/logout":
            self.auth.logout(self._session_token())
            self._send_json(
                200,
                {"ok": True},
                headers={"Set-Cookie": self._session_cookie("", max_age=0)},
            )
            return
        admin_action = ADMIN_USER_ACTION_ROUTE.fullmatch(path)
        if admin_action:
            self._manage_user(int(admin_action.group(1)))
            return
        if path != "/api/jobs":
            self._send_json(404, {"error": "Not found"})
            return
        user = self._current_user()
        guest_id = self._guest_token()
        new_guest_token = None
        if user is None and guest_id is None:
            new_guest_token = secrets.token_urlsafe(32)
            guest_id = new_guest_token
        assert user is not None or guest_id is not None
        if user is None and self.manager.guest_has_job(guest_id):
            self._send_json(403, {"error": "注册后可以继续生成更多乐谱。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            LOGGER.warning("拒绝上传：reason=size bytes=%s", length)
            self._send_json(413, {"error": "音频文件为空或超过 50 MB。"})
            return
        try:
            job, temporary = self.manager.new_upload(
                self.headers.get("X-Filename", ""),
                user_id=user.id if user is not None else None,
                guest_id=guest_id if user is None else None,
            )
        except ValueError as exc:
            LOGGER.warning("拒绝上传：reason=file_type")
            self._send_json(400, {"error": str(exc)})
            return
        job.artist = " ".join(unquote(self.headers.get("X-Artist", "")).split())
        job.song_title = " ".join(unquote(self.headers.get("X-Song-Title", "")).split())
        if (
            not job.artist
            or not job.song_title
            or max(len(job.artist), len(job.song_title)) > 180
        ):
            self._send_json(400, {"error": "请填写艺术家和歌曲名，每项不超过 180 字。"})
            return
        job.show_vocal = self.headers.get("X-Show-Vocal", "false") == "true"
        reserved = False
        try:
            with temporary.open("xb") as output:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("上传中断")
                    output.write(chunk)
                    remaining -= len(chunk)
            validate_audio_duration(temporary)
            if user is not None:
                job.quota_remaining = self.auth.reserve_song(user, job.id)
                reserved = True
            temporary.replace(job.upload_path)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            if reserved:
                try:
                    self.auth.release_song(job.id)
                except Exception:
                    LOGGER.exception("退回上传额度失败：job_id=%s", job.id)
            if isinstance(exc, (AudioDurationError, AuthError)):
                LOGGER.warning("拒绝上传：job_id=%s reason=%s", job.id, str(exc))
                self._send_json(
                    exc.status if isinstance(exc, AuthError) else 422,
                    {"error": str(exc)},
                )
            else:
                LOGGER.exception("音频上传失败：job_id=%s", job.id)
                self._send_json(400, {"error": "音频上传中断，请重试。"})
            return
        LOGGER.info(
            "音频上传完成：job_id=%s file=%s bytes=%s",
            job.id,
            job.filename,
            length,
        )
        self.manager.enqueue(job)
        headers = (
            {"Set-Cookie": self._guest_cookie(new_guest_token)}
            if new_guest_token is not None
            else None
        )
        self._send_json(202, job.payload(full_access=user is not None), headers=headers)

    def _route(self, *, send_body: bool) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "ok"}, send_body=send_body)
            return
        if path in {"/", "/index.html"}:
            self._send_file(self.static_dir / "index.html", send_body=send_body)
            return
        if path == "/vendor/jsmediatags-3.9.7.min.js":
            self._send_file(
                self.static_dir / "vendor" / "jsmediatags-3.9.7.min.js",
                send_body=send_body,
            )
            return
        if path == "/api/auth/me":
            user = self._current_user()
            self._send_json(
                200,
                {
                    "user": {
                        "email": user.email,
                        "isAdmin": user.is_admin,
                        **self.auth.usage_for_user(user),
                    }
                    if user is not None
                    else None
                },
                send_body=send_body,
            )
            return
        user = self._current_user()
        if path == "/api/admin/users":
            if user is None:
                self._send_json(401, {"error": "请先登录。"}, send_body=send_body)
            elif not user.is_admin:
                self._send_json(403, {"error": "需要管理员权限。"}, send_body=send_body)
            else:
                self._send_json(
                    200, {"users": self.auth.list_users()}, send_body=send_body
                )
            return
        job_match = JOB_ROUTE.fullmatch(path)
        if job_match:
            job = self.manager.get(
                job_match.group(1),
                user_id=user.id if user is not None else None,
                guest_id=self._guest_token(),
            )
            if job is None:
                self._send_json(404, {"error": "任务不存在"}, send_body=send_body)
            else:
                self._send_json(
                    200,
                    job.payload(full_access=user is not None),
                    send_body=send_body,
                )
            return
        file_match = FILE_ROUTE.fullmatch(path)
        if file_match:
            job = self.manager.get(
                file_match.group(1),
                user_id=user.id if user is not None else None,
                guest_id=self._guest_token(),
            )
            if job is None or job.status != "complete":
                self._send_json(404, {"error": "文件不存在"}, send_body=send_body)
                return
            requested_file = file_match.group(2)
            if not can_access_result_file(user, requested_file):
                self._send_json(
                    401, {"error": "注册后可查看完整乐谱。"}, send_body=send_body
                )
                return
            part_name = Path(requested_file).stem
            is_part = part_name in PART_LABELS
            if is_part and part_name not in job.parts:
                self._send_json(404, {"error": "分谱不存在"}, send_body=send_body)
                return
            self._send_file(
                job.output_dir / ("parts" if is_part else "rendered") / requested_file,
                send_body=send_body,
                download_name=pdf_filename(
                    f"{job.score_title}-{PART_LABELS[part_name]}"
                    if is_part
                    else job.score_title
                )
                if requested_file.endswith(".pdf")
                else None,
            )
            return
        self._send_json(404, {"error": "Not found"}, send_body=send_body)

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        send_body: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _send_file(
        self, path: Path, *, send_body: bool, download_name: str | None = None
    ) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "文件不存在"}, send_body=send_body)
            return
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        if download_name:
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + quote(download_name),
            )
        self.send_header("Content-Length", str(path.stat().st_size))
        self.send_header(
            "Cache-Control",
            "no-store" if path.suffix == ".html" else "private, max-age=3600",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if send_body:
            with path.open("rb") as source:
                shutil.copyfileobj(source, self.wfile)

    def _send_auth_code(self) -> None:
        try:
            payload = self._read_json()
            self.auth.send_code(str(payload.get("email", "")))
        except AuthError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        except Exception:
            LOGGER.exception("验证码请求处理失败")
            self._send_json(500, {"error": "验证码发送失败，请稍后重试。"})
            return
        self._send_json(200, {"message": "验证码已发送，请检查邮箱。"})

    def _verify_auth_code(self) -> None:
        try:
            payload = self._read_json()
            login = self.auth.verify(
                str(payload.get("email", "")), str(payload.get("code", ""))
            )
        except AuthError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        except Exception:
            LOGGER.exception("登录验证处理失败")
            self._send_json(500, {"error": "登录失败，请稍后重试。"})
            return
        self._send_json(
            200,
            {"user": self._user_payload(login.user)},
            headers={"Set-Cookie": self._session_cookie(login.token)},
        )

    def _login_admin(self) -> None:
        try:
            payload = self._read_json()
            login = self.auth.login_admin(
                str(payload.get("username", "")),
                str(payload.get("password", "")),
            )
        except AuthError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        except Exception:
            LOGGER.exception("管理员登录处理失败")
            self._send_json(500, {"error": "管理员登录失败，请稍后重试。"})
            return
        self._send_json(
            200,
            {"user": self._user_payload(login.user)},
            headers={"Set-Cookie": self._session_cookie(login.token)},
        )

    def _manage_user(self, user_id: int) -> None:
        user = self._require_user()
        if user is None:
            return
        if not user.is_admin:
            self._send_json(403, {"error": "需要管理员权限。"})
            return
        try:
            payload = self._read_json()
            self.auth.manage_user(user_id, str(payload.get("action", "")))
        except AuthError as exc:
            self._send_json(exc.status, {"error": str(exc)})
            return
        except Exception:
            LOGGER.exception("用户管理操作失败：user_id=%s", user_id)
            self._send_json(500, {"error": "用户操作失败，请稍后重试。"})
            return
        self._send_json(200, {"ok": True})

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise AuthError("请求内容无效。") from exc
        if length <= 0 or length > 4096:
            raise AuthError("请求内容无效。")
        try:
            payload = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AuthError("请求内容无效。") from exc
        if not isinstance(payload, dict):
            raise AuthError("请求内容无效。")
        return payload

    def _current_user(self) -> User | None:
        return self.auth.user_for_session(self._session_token())

    def _user_payload(self, user: User) -> dict[str, Any]:
        return {
            "email": user.email,
            "isAdmin": user.is_admin,
            **self.auth.usage_for_user(user),
        }

    def _require_user(self, *, send_body: bool = True) -> User | None:
        user = self._current_user()
        if user is None:
            self._send_json(401, {"error": "请先登录。"}, send_body=send_body)
        return user

    def _session_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except (CookieError, ValueError):
            return None
        session = cookie.get("sheetstage2_session")
        return session.value if session else None

    def _guest_token(self) -> str | None:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except (CookieError, ValueError):
            return None
        guest = cookie.get("sheetstage2_guest")
        if guest is None or GUEST_TOKEN_PATTERN.fullmatch(guest.value) is None:
            return None
        return guest.value

    def _session_cookie(self, token: str, *, max_age: int = SESSION_TTL_SECONDS) -> str:
        parts = [
            f"sheetstage2_session={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if self.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def _guest_cookie(self, token: str) -> str:
        parts = [
            f"sheetstage2_guest={token}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={SESSION_TTL_SECONDS}",
        ]
        if self.secure_cookies:
            parts.append("Secure")
        return "; ".join(parts)

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("HTTP：" + format, *args)


def configure_logging(log_path: Path = DEFAULT_LOG_PATH) -> Path:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
    ]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=handlers,
        force=True,
    )
    return log_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="SheetSage2 乐谱生成网页")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8791)
    parser.add_argument("--static-dir", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_path = configure_logging()
    settings = Settings.from_environment()
    static_dir = (
        args.static_dir.expanduser().resolve()
        if args.static_dir
        else Path(__file__).resolve().parents[2] / "pdf-web" / "dist"
    )
    if not (static_dir / "index.html").is_file():
        raise FileNotFoundError(f"网页目录不可用：{static_dir}")
    auth = AuthManager(
        settings.data_dir, build_mail_sender(), auth_dir=DEFAULT_AUTH_DIR
    )
    manager = JobManager(settings, auth)
    secure_cookies = os.environ.get("SHEETSTAGE2_COOKIE_SECURE", "").lower() in {
        "1",
        "true",
        "yes",
    }
    handler = partial(
        WebHandler,
        manager=manager,
        auth=auth,
        static_dir=static_dir,
        secure_cookies=secure_cookies,
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    LOGGER.info(
        "SheetSage2 网页已启动：http://%s:%s data_dir=%s log=%s",
        args.host,
        args.port,
        settings.data_dir,
        log_path,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        LOGGER.info("SheetSage2 网页已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
