from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

LOGGER = logging.getLogger(__name__)
CODE_TTL_SECONDS = 10 * 60
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
EMAIL_SEND_LIMIT_PER_HOUR = 5
GLOBAL_SEND_LIMIT_PER_HOUR = 100
MAX_CODE_ATTEMPTS = 5
ADMIN_USERNAME = "admin"
FREE_SONG_LIMIT = 10
DEFAULT_AUTH_DIR = Path.home() / "Library" / "Application Support" / "SheetStage2" / "auth"
EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


class AuthError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class MailDeliveryError(RuntimeError):
    pass


class MailSender(Protocol):
    name: str

    def send_verification_code(self, recipient: str, code: str) -> None: ...


@dataclass(frozen=True)
class UnavailableMailSender:
    name: str = "unavailable"

    def send_verification_code(self, recipient: str, code: str) -> None:
        raise MailDeliveryError("未配置邮件服务")


@dataclass(frozen=True)
class User:
    id: int
    email: str
    is_admin: bool = False


@dataclass(frozen=True)
class Login:
    user: User
    token: str


@dataclass(frozen=True)
class AliDirectMailSender:
    access_key_id: str
    access_key_secret: str
    account_name: str
    from_alias: str = "SheetStage2"
    region_id: str = "cn-hangzhou"
    endpoint: str = "https://dm.aliyuncs.com/"
    name: str = "aliyun"

    def send_verification_code(self, recipient: str, code: str) -> None:
        body = _verification_html(code)
        parameters = {
            "AccessKeyId": self.access_key_id,
            "AccountName": self.account_name,
            "Action": "SingleSendMail",
            "AddressType": "1",
            "Format": "JSON",
            "FromAlias": self.from_alias,
            "HtmlBody": body,
            "RegionId": self.region_id,
            "ReplyToAddress": "false",
            "SignatureMethod": "HMAC-SHA1",
            "SignatureNonce": uuid.uuid4().hex,
            "SignatureVersion": "1.0",
            "Subject": "SheetStage2 登录验证码",
            "Timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ToAddress": recipient,
            "Version": "2015-11-23",
        }
        parameters["Signature"] = _aliyun_signature(
            parameters, self.access_key_secret
        )
        response = _post_form(self.name, self.endpoint, parameters)
        if not response.get("EnvId"):
            code_value = response.get("Code", "unknown")
            raise MailDeliveryError(f"aliyun 返回失败状态：{code_value}")


@dataclass(frozen=True)
class ResendMailSender:
    api_key: str
    sender: str
    from_name: str = "SheetStage2"
    endpoint: str = "https://api.resend.com/emails"
    name: str = "resend"

    def send_verification_code(self, recipient: str, code: str) -> None:
        response = _post_json(
            self.name,
            self.endpoint,
            {
                "from": f"{self.from_name} <{self.sender}>",
                "to": [recipient],
                "subject": "SheetStage2 登录验证码",
                "html": _verification_html(code),
            },
            {"Authorization": f"Bearer {self.api_key}"},
        )
        if not response.get("id"):
            raise MailDeliveryError("resend 返回内容缺少邮件 ID")


@dataclass(frozen=True)
class GmailMailSender:
    email: str
    app_password: str
    from_name: str = "SheetStage2"
    host: str = "smtp.gmail.com"
    port: int = 465
    name: str = "gmail"

    def send_verification_code(self, recipient: str, code: str) -> None:
        message = EmailMessage()
        message["From"] = f"{self.from_name} <{self.email}>"
        message["To"] = recipient
        message["Subject"] = "SheetStage2 登录验证码"
        message.set_content(f"SheetStage2 登录验证码：{code}\n验证码 10 分钟内有效。")
        message.add_alternative(_verification_html(code), subtype="html")
        try:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=10) as smtp:
                smtp.login(self.email, self.app_password)
                smtp.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            LOGGER.warning("Gmail SMTP 发送失败：error_type=%s", type(exc).__name__)
            raise MailDeliveryError("gmail SMTP 发送失败") from exc


@dataclass(frozen=True)
class ChainedMailSender:
    senders: tuple[MailSender, ...]
    name: str = "mail-chain"

    def send_verification_code(self, recipient: str, code: str) -> None:
        last_error: Exception | None = None
        for sender in self.senders:
            try:
                sender.send_verification_code(recipient, code)
                LOGGER.info("验证码邮件已提交：provider=%s", sender.name)
                return
            except MailDeliveryError as exc:
                last_error = exc
                LOGGER.warning("验证码邮件通道失败：provider=%s error=%s", sender.name, exc)
        raise MailDeliveryError("所有邮件通道均发送失败") from last_error


class AuthManager:
    def __init__(
        self, data_dir: Path, sender: MailSender, *, auth_dir: Path | None = None
    ) -> None:
        auth_dir = auth_dir or data_dir / "web" / "auth"
        auth_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database_path = auth_dir / "users.sqlite3"
        self._secret = _load_or_create_secret(auth_dir / "secret.key")
        self._sender = sender
        # ponytail: 小型单机服务用全局锁串行化验证码状态；高并发时再改为数据库级预留。
        self._lock = threading.Lock()
        self._initialize_database()
        self.admin_credentials_path = auth_dir / "admin_credentials.txt"
        self._admin_password = _load_or_create_admin_password(
            self.admin_credentials_path
        )
        self._ensure_admin_user()

    def send_code(self, email: str) -> None:
        normalized = normalize_email(email)
        now = int(time.time())
        code = f"{secrets.randbelow(1_000_000):06d}"
        code_hash = self._code_hash(normalized, code)
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM verification_sends WHERE sent_at < ?",
                    (now - 24 * 60 * 60,),
                )
                latest = connection.execute(
                    "SELECT MAX(sent_at) FROM verification_sends WHERE email = ?",
                    (normalized,),
                ).fetchone()[0]
                if latest is not None and now - latest < 60:
                    raise AuthError("验证码发送过于频繁，请 60 秒后再试。", 429)
                email_count = connection.execute(
                    "SELECT COUNT(*) FROM verification_sends "
                    "WHERE email = ? AND sent_at >= ?",
                    (normalized, now - 60 * 60),
                ).fetchone()[0]
                global_count = connection.execute(
                    "SELECT COUNT(*) FROM verification_sends WHERE sent_at >= ?",
                    (now - 60 * 60,),
                ).fetchone()[0]
                if email_count >= EMAIL_SEND_LIMIT_PER_HOUR:
                    raise AuthError("验证码发送次数过多，请稍后再试。", 429)
                if global_count >= GLOBAL_SEND_LIMIT_PER_HOUR:
                    raise AuthError("验证码服务繁忙，请稍后再试。", 429)

            try:
                self._sender.send_verification_code(normalized, code)
            except MailDeliveryError as exc:
                LOGGER.error("验证码邮件发送失败：error=%s", exc)
                raise AuthError("验证码发送失败，请稍后重试。", 503) from exc

            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO verification_sends(email, sent_at) VALUES (?, ?)",
                    (normalized, now),
                )
                connection.execute(
                    "INSERT INTO verification_codes(email, code_hash, expires_at, attempts) "
                    "VALUES (?, ?, ?, 0) "
                    "ON CONFLICT(email) DO UPDATE SET "
                    "code_hash = excluded.code_hash, "
                    "expires_at = excluded.expires_at, attempts = 0",
                    (normalized, code_hash, now + CODE_TTL_SECONDS),
                )
        LOGGER.info("验证码已发送")

    def verify(self, email: str, code: str) -> Login:
        normalized = normalize_email(email)
        if not re.fullmatch(r"\d{6}", code):
            raise AuthError("请输入 6 位数字验证码。")
        now = int(time.time())
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT code_hash, expires_at, attempts FROM verification_codes "
                "WHERE email = ?",
                (normalized,),
            ).fetchone()
            if row is None or row["expires_at"] < now:
                connection.execute(
                    "DELETE FROM verification_codes WHERE email = ?", (normalized,)
                )
                raise AuthError("验证码无效或已过期，请重新获取。")
            if row["attempts"] >= MAX_CODE_ATTEMPTS:
                raise AuthError("验证码尝试次数过多，请重新获取。", 429)
            if not hmac.compare_digest(
                row["code_hash"], self._code_hash(normalized, code)
            ):
                connection.execute(
                    "UPDATE verification_codes SET attempts = attempts + 1 "
                    "WHERE email = ?",
                    (normalized,),
                )
                raise AuthError("验证码错误。")

            connection.execute(
                "INSERT INTO users(email, created_at) VALUES (?, ?) "
                "ON CONFLICT(email) DO NOTHING",
                (normalized, now),
            )
            user_row = connection.execute(
                "SELECT id, email, disabled FROM users WHERE email = ?", (normalized,)
            ).fetchone()
            assert user_row is not None
            if user_row["disabled"]:
                connection.execute(
                    "DELETE FROM verification_codes WHERE email = ?", (normalized,)
                )
                raise AuthError("账号已被禁用，请联系管理员。", 403)
            connection.execute(
                "DELETE FROM verification_codes WHERE email = ?", (normalized,)
            )
            token = self._create_session(connection, user_row["id"], now)
        LOGGER.info("用户登录成功：user_id=%s", user_row["id"])
        return Login(User(user_row["id"], user_row["email"]), token)

    def login_admin(self, username: str, password: str) -> Login:
        valid_username = hmac.compare_digest(username.strip(), ADMIN_USERNAME)
        valid_password = hmac.compare_digest(password, self._admin_password)
        if not (valid_username & valid_password):
            raise AuthError("管理员账号或密码错误。", 401)
        now = int(time.time())
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT id, email FROM users WHERE email = ?", (ADMIN_USERNAME,)
            ).fetchone()
            if row is None:
                raise RuntimeError("管理员账户不存在")
            token = self._create_session(connection, row["id"], now)
        LOGGER.info("管理员登录成功：user_id=%s", row["id"])
        return Login(User(row["id"], row["email"], is_admin=True), token)

    def user_for_session(self, token: str | None) -> User | None:
        if not token:
            return None
        now = int(time.time())
        with self._connect() as connection:
            row = connection.execute(
                "SELECT users.id, users.email FROM sessions "
                "JOIN users ON users.id = sessions.user_id "
                "WHERE sessions.token_hash = ? AND sessions.expires_at >= ? "
                "AND users.disabled = 0 AND users.deleted = 0",
                (_token_hash(token), now),
            ).fetchone()
        return (
            User(row["id"], row["email"], is_admin=row["email"] == ADMIN_USERNAME)
            if row
            else None
        )

    def logout(self, token: str | None) -> None:
        if not token:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),)
            )

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, email, created_at, disabled, "
                "(SELECT COUNT(*) FROM song_uses WHERE user_id = users.id) songs_used "
                "FROM users "
                "WHERE deleted = 0 "
                "ORDER BY email = ? DESC, created_at DESC",
                (ADMIN_USERNAME,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "email": row["email"],
                "isAdmin": row["email"] == ADMIN_USERNAME,
                "disabled": bool(row["disabled"]),
                "createdAt": row["created_at"],
                "songsUsed": row["songs_used"],
                "songsLimit": None
                if row["email"] == ADMIN_USERNAME
                else FREE_SONG_LIMIT,
            }
            for row in rows
        ]

    def usage_for_user(self, user: User) -> dict[str, int | None]:
        if user.is_admin:
            return {"songsUsed": 0, "songsLimit": None, "songsRemaining": None}
        with self._connect() as connection:
            used = connection.execute(
                "SELECT COUNT(*) FROM song_uses WHERE user_id = ?", (user.id,)
            ).fetchone()[0]
        return {
            "songsUsed": used,
            "songsLimit": FREE_SONG_LIMIT,
            "songsRemaining": max(0, FREE_SONG_LIMIT - used),
        }

    def reserve_song(self, user: User, job_id: str) -> int | None:
        if user.is_admin:
            return None
        with self._lock, self._connect() as connection:
            active = connection.execute(
                "SELECT 1 FROM users WHERE id = ? AND disabled = 0 AND deleted = 0",
                (user.id,),
            ).fetchone()
            if active is None:
                raise AuthError("账号不可用，请重新登录。", 403)
            used = connection.execute(
                "SELECT COUNT(*) FROM song_uses WHERE user_id = ?", (user.id,)
            ).fetchone()[0]
            if used >= FREE_SONG_LIMIT:
                raise AuthError("10 首免费额度已用完。", 403)
            connection.execute(
                "INSERT INTO song_uses(job_id, user_id, status, created_at) "
                "VALUES (?, ?, 'reserved', ?)",
                (job_id, user.id, int(time.time())),
            )
        return FREE_SONG_LIMIT - used - 1

    def complete_song(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE song_uses SET status = 'complete' WHERE job_id = ?", (job_id,)
            )

    def release_song(self, job_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM song_uses WHERE job_id = ? AND status = 'reserved'",
                (job_id,),
            )

    def manage_user(self, user_id: int, action: str) -> None:
        if action not in {"enable", "disable", "logout", "delete"}:
            raise AuthError("不支持的用户操作。")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT email FROM users WHERE id = ? AND deleted = 0", (user_id,)
            ).fetchone()
            if row is None:
                raise AuthError("用户不存在。", 404)
            if row["email"] == ADMIN_USERNAME:
                raise AuthError("不能操作管理员账户。")
            if action == "enable":
                connection.execute(
                    "UPDATE users SET disabled = 0 WHERE id = ?", (user_id,)
                )
            elif action == "disable":
                connection.execute(
                    "UPDATE users SET disabled = 1 WHERE id = ?", (user_id,)
                )
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            elif action == "logout":
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
            else:
                connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                connection.execute(
                    "DELETE FROM verification_codes WHERE email = ?", (row["email"],)
                )
                connection.execute(
                    "DELETE FROM verification_sends WHERE email = ?", (row["email"],)
                )
                connection.execute("DELETE FROM song_uses WHERE user_id = ?", (user_id,))
                connection.execute(
                    "UPDATE users SET email = ?, disabled = 1, deleted = 1 "
                    "WHERE id = ?",
                    (f"deleted-{user_id}@invalid", user_id),
                )
        LOGGER.info("管理员操作用户：user_id=%s action=%s", user_id, action)

    def _code_hash(self, email: str, code: str) -> str:
        return hmac.new(
            self._secret, f"{email}\0{code}".encode(), hashlib.sha256
        ).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS verification_codes (
                    email TEXT PRIMARY KEY,
                    code_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    attempts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verification_sends (
                    id INTEGER PRIMARY KEY,
                    email TEXT NOT NULL,
                    sent_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS verification_sends_lookup
                    ON verification_sends(email, sent_at);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS song_uses (
                    job_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('reserved', 'complete')),
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS song_uses_user
                    ON song_uses(user_id);
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "disabled" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0"
                )
            if "deleted" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0"
                )

    def _ensure_admin_user(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO users(email, created_at) VALUES (?, ?) "
                "ON CONFLICT(email) DO UPDATE SET disabled = 0, deleted = 0",
                (ADMIN_USERNAME, int(time.time())),
            )

    def _create_session(
        self, connection: sqlite3.Connection, user_id: int, now: int
    ) -> str:
        token = secrets.token_urlsafe(32)
        connection.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        connection.execute(
            "INSERT INTO sessions(token_hash, user_id, expires_at) VALUES (?, ?, ?)",
            (_token_hash(token), user_id, now + SESSION_TTL_SECONDS),
        )
        return token


def normalize_email(value: str) -> str:
    email = value.strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        raise AuthError("请输入有效的邮箱地址。")
    return email


def build_mail_sender() -> MailSender:
    configured = os.environ.get("SHEETSTAGE2_MAIL_PROVIDERS", "").strip()
    if configured:
        names = [item.strip().lower() for item in configured.split(",") if item.strip()]
    else:
        names = []
        if os.environ.get("SHEETSTAGE2_ALI_ACCESS_KEY_ID"):
            names.append("aliyun")
        if os.environ.get("SHEETSTAGE2_RESEND_API_KEY"):
            names.append("resend")
        if os.environ.get("SHEETSTAGE2_GMAIL_EMAIL"):
            names.append("gmail")
    if not names:
        LOGGER.warning(
            "未配置邮件服务：管理员登录可用，邮箱验证码暂不可用"
        )
        return UnavailableMailSender()

    senders: list[MailSender] = []
    for name in names:
        if name == "aliyun":
            senders.append(
                AliDirectMailSender(
                    access_key_id=_required_environment(
                        "SHEETSTAGE2_ALI_ACCESS_KEY_ID"
                    ),
                    access_key_secret=_required_environment(
                        "SHEETSTAGE2_ALI_ACCESS_KEY_SECRET"
                    ),
                    account_name=_required_environment(
                        "SHEETSTAGE2_ALI_ACCOUNT_NAME"
                    ),
                    from_alias=os.environ.get(
                        "SHEETSTAGE2_ALI_FROM_ALIAS", "SheetStage2"
                    ),
                    region_id=os.environ.get(
                        "SHEETSTAGE2_ALI_REGION_ID", "cn-hangzhou"
                    ),
                    endpoint=os.environ.get(
                        "SHEETSTAGE2_ALI_ENDPOINT", "https://dm.aliyuncs.com/"
                    ),
                )
            )
        elif name == "resend":
            senders.append(
                ResendMailSender(
                    api_key=_required_environment("SHEETSTAGE2_RESEND_API_KEY"),
                    sender=_required_environment("SHEETSTAGE2_RESEND_FROM"),
                    from_name=os.environ.get(
                        "SHEETSTAGE2_RESEND_FROM_NAME", "SheetStage2"
                    ),
                )
            )
        elif name == "gmail":
            senders.append(
                GmailMailSender(
                    email=_required_environment("SHEETSTAGE2_GMAIL_EMAIL"),
                    app_password=_required_secret_file(
                        "SHEETSTAGE2_GMAIL_APP_PASSWORD_FILE"
                    ),
                    from_name=os.environ.get(
                        "SHEETSTAGE2_GMAIL_FROM_NAME", "SheetStage2"
                    ),
                )
            )
        else:
            raise RuntimeError(f"不支持的邮件服务：{name}")
    return senders[0] if len(senders) == 1 else ChainedMailSender(tuple(senders))


def _verification_html(code: str) -> str:
    safe_code = html.escape(code)
    return (
        "<div style=\"font-family:Arial,sans-serif;line-height:1.7\">"
        "<h2>SheetStage2 登录验证码</h2>"
        f"<p style=\"font-size:28px;font-weight:bold;letter-spacing:6px\">{safe_code}</p>"
        "<p>验证码 10 分钟内有效。若非本人操作，请忽略本邮件。</p>"
        "</div>"
    )


def _aliyun_signature(parameters: dict[str, str], secret: str) -> str:
    canonical = "&".join(
        f"{_percent_encode(key)}={_percent_encode(value)}"
        for key, value in sorted(parameters.items())
    )
    string_to_sign = f"POST&%2F&{_percent_encode(canonical)}"
    digest = hmac.new(
        f"{secret}&".encode(), string_to_sign.encode(), hashlib.sha1
    ).digest()
    return base64.b64encode(digest).decode()


def _percent_encode(value: str) -> str:
    return quote(str(value), safe="~")


def _post_form(provider: str, url: str, parameters: dict[str, str]) -> dict[str, Any]:
    request = Request(
        url,
        data=urlencode(parameters).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = response.read(256 * 1024)
    except HTTPError as exc:
        error_code = "unknown"
        try:
            error_payload = json.loads(exc.read(64 * 1024))
            if isinstance(error_payload, dict):
                error_code = str(
                    error_payload.get("Code", error_payload.get("statusCode", "unknown"))
                )[:80]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass
        LOGGER.warning(
            "邮件接口请求失败：provider=%s status=%s url=%s error_code=%s",
            provider,
            exc.code,
            url,
            error_code,
        )
        raise MailDeliveryError(f"{provider} HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        LOGGER.warning("邮件接口网络失败：provider=%s url=%s", provider, url)
        raise MailDeliveryError(f"{provider} 网络请求失败") from exc
    try:
        result = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailDeliveryError(f"{provider} 返回内容无效") from exc
    if not isinstance(result, dict):
        raise MailDeliveryError(f"{provider} 返回内容无效")
    return result


def _post_json(
    provider: str,
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            response_payload = response.read(256 * 1024)
    except HTTPError as exc:
        LOGGER.warning(
            "邮件接口请求失败：provider=%s status=%s url=%s",
            provider,
            exc.code,
            url,
        )
        raise MailDeliveryError(f"{provider} HTTP {exc.code}") from exc
    except (URLError, TimeoutError) as exc:
        LOGGER.warning("邮件接口网络失败：provider=%s url=%s", provider, url)
        raise MailDeliveryError(f"{provider} 网络请求失败") from exc
    try:
        result = json.loads(response_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailDeliveryError(f"{provider} 返回内容无效") from exc
    if not isinstance(result, dict):
        raise MailDeliveryError(f"{provider} 返回内容无效")
    return result


def _load_or_create_secret(path: Path) -> bytes:
    try:
        secret = path.read_bytes()
    except FileNotFoundError:
        secret = secrets.token_bytes(32)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _load_or_create_secret(path)
        with os.fdopen(descriptor, "wb") as output:
            output.write(secret)
    if len(secret) < 32:
        raise RuntimeError(f"认证密钥无效：{path}")
    return secret


def _load_or_create_admin_password(path: Path) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        password = secrets.token_urlsafe(24)
        content = f"username={ADMIN_USERNAME}\npassword={password}\n"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return _load_or_create_admin_password(path)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
    values = dict(
        line.split("=", 1) for line in content.splitlines() if "=" in line
    )
    if values.get("username") != ADMIN_USERNAME or not values.get("password"):
        raise RuntimeError(f"管理员凭据文件无效：{path}")
    return values["password"]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少环境变量：{name}")
    return value


def _required_secret_file(name: str) -> str:
    path = Path(_required_environment(name)).expanduser()
    try:
        value = path.read_text(encoding="utf-8").strip().replace(" ", "")
    except OSError as exc:
        raise RuntimeError(f"无法读取密钥文件：{path}") from exc
    if not value:
        raise RuntimeError(f"密钥文件为空：{path}")
    return value
