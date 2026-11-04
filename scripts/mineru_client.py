#!/usr/bin/env python3
"""MinerU official API client: token checks, a harmless health check, and one-file extraction.

Ported from manage-article-knowledge so this skill works on its own. Both skills read the
same MINERU_API_TOKEN, so one setup serves either. The token is never written to a
project, a log, or a status file; only its short fingerprint is kept.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import os
import sys
import tempfile
import time
import zipfile
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


TOKEN_ENV = "MINERU_API_TOKEN"
TOKEN_ENV_COMPAT = "MINERU_TOKEN"
EXPIRES_ENV = "MINERU_API_EXPIRES_ON"
STATE_DIR_ENV = "MINERU_STATE_DIR"
DEFAULT_API_BASE = "https://mineru.net/api/v4"
SOURCE_HEADER = "compile-enterprise-knowledge"
TOKEN_PAGE = "https://mineru.net/apiManage/token"
FINAL_STATES = {"done", "failed"}
RETRY_DELAYS = (2.0, 5.0)

FAILURE_LABELS = {
    "credential-missing-or-expired": "Token 缺失或已过期",
    "credential-invalid": "Token 鉴权失败",
    "quota-or-rate-limit": "额度或频率限制",
    "sandbox-network-permission": "当前 AI 工具没有联网权限",
    "temporary-network-or-service": "临时网络或服务故障",
    "upload-signature-or-clock": "上传签名失败或电脑时间不准",
    "output-quality": "解析结果几乎没有文字",
    "api-or-file-processing": "MinerU 处理这个文件失败",
    "local-workflow": "本地输入或路径问题",
}
FAILURE_ACTIONS = {
    "credential-missing-or-expired": f"运行 scripts/setup_mineru.cmd 重新配置 Token（在 {TOKEN_PAGE} 创建），然后完全重启 AI 工具。",
    "credential-invalid": "Token 可能被撤销或复制不完整。重新复制后运行 setup_mineru.cmd，再完全重启 AI 工具。",
    "quota-or-rate-limit": "检查 MinerU 账号额度和频率限制；这不是 Token 坏了，不要重新配置。",
    "sandbox-network-permission": "在 AI 工具弹出的权限窗口里允许这条命令联网后重试；不要更换 Token。",
    "temporary-network-or-service": "稍后重试，最多 2 次；仍失败再检查网络和 MinerU 服务状态。",
    "upload-signature-or-clock": "检查电脑系统时间是否准确，然后重试。",
    "output-quality": "这个文件解析不出文字，改为人工处理，或在 MinerU 网页端处理后把 md 交给 AI。",
    "api-or-file-processing": "核对文件大小、页数和格式后重试；连续失败就改为人工处理。",
    "local-workflow": "修正输入路径或参数后重试。",
}


class MinerUError(RuntimeError):
    def __init__(self, message: str, stage: str, *, retryable: bool = False):
        super().__init__(message)
        self.stage = stage
        self.retryable = retryable


def resolve_token() -> tuple[str | None, str | None]:
    for name in (TOKEN_ENV, TOKEN_ENV_COMPAT):
        token = os.environ.get(name, "").strip()
        if token:
            return token, name
    return None, None


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def status_paths() -> list[Path]:
    candidates = []
    configured = os.environ.get(STATE_DIR_ENV, "").strip()
    if configured:
        candidates.append(Path(configured) / "mineru-api-status.json")
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidates.append(Path(local) / "compile-enterprise-knowledge" / "mineru-api-status.json")
    candidates.append(Path(tempfile.gettempdir()) / "compile-enterprise-knowledge" / "mineru-api-status.json")
    return list(dict.fromkeys(candidates))


def load_status() -> dict:
    for path in status_paths():
        try:
            if path.is_file():
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    return value
        except (OSError, json.JSONDecodeError):
            continue
    return {}


def save_status(status: dict) -> Path | None:
    clean = {key: status.get(key) for key in ("token_fingerprint", "expires_on", "reminders", "last_online_check", "last_online_failure")}
    for path in status_paths():
        temporary = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, path)
            return path
        except OSError:
            temporary.unlink(missing_ok=True)
    return None


def expiry_status(today: date | None = None) -> dict:
    today = today or date.today()
    raw = os.environ.get(EXPIRES_ENV, "").strip()
    result = {"expires_on": None, "days_remaining": None, "level": "unknown", "message": "没有登记 Token 到期日"}
    if not raw:
        return result
    try:
        expires_on = date.fromisoformat(raw)
    except ValueError:
        result["message"] = f"到期日格式不对：{raw}，应为 YYYY-MM-DD"
        return result
    days = (expires_on - today).days
    result.update(expires_on=expires_on.isoformat(), days_remaining=days)
    if days < 0:
        result.update(level="expired", message="Token 已过期，更换前不能解析")
    elif days <= 1:
        result.update(level="urgent", message="Token 1 天内到期，请立即更换，更换前不要大批量解析")
    elif days <= 7:
        result.update(level="warning", message="Token 7 天内到期，请安排更换")
    else:
        result.update(level="ok", message="Token 未临近到期")
    return result


def classify_failure(exc: MinerUError) -> str:
    text = str(exc).casefold()
    # The server can expire a token before the date someone wrote down at setup.
    if exc.stage == "token-check" or "token expired" in text or "过期" in text:
        return "credential-missing-or-expired"
    if "winerror 10013" in text or "访问套接字" in text:
        return "sandbox-network-permission"
    if any(word in text for word in ("401", "unauthorized", "token", "鉴权", "认证")):
        return "credential-invalid"
    if any(word in text for word in ("429", "quota", "rate", "额度")):
        return "quota-or-rate-limit"
    if exc.stage == "file-upload" and "403" in text:
        return "upload-signature-or-clock"
    if exc.retryable:
        return "temporary-network-or-service"
    if exc.stage in {"quality-check", "output-discovery"}:
        return "output-quality"
    if exc.stage in {"api-submit", "api-poll", "result-download", "file-upload"}:
        return "api-or-file-processing"
    return "local-workflow"


def failure_report(exc: MinerUError) -> dict:
    kind = classify_failure(exc)
    return {"ok": False, "failure_type": kind, "failure_label": FAILURE_LABELS[kind],
            "failure_stage": exc.stage, "error": str(exc), "next_action": FAILURE_ACTIONS[kind]}


class MinerUApi:
    def __init__(self, token: str, base_url: str = DEFAULT_API_BASE, request_timeout: int = 120, retry_submit: bool = False):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout
        self.retry_submit = retry_submit

    def _json(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "source": SOURCE_HEADER}
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        # Submitting twice could be billed twice, so only reads retry by default.
        delays = RETRY_DELAYS if method == "GET" or self.retry_submit else ()
        last: MinerUError | None = None
        for attempt in range(len(delays) + 1):
            try:
                with urlopen(Request(url, data=data, headers=headers, method=method), timeout=self.request_timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                try:
                    detail = json.loads(exc.read(4096).decode("utf-8", "replace")).get("msg")
                except Exception:
                    detail = None
                last = MinerUError(f"HTTP {exc.code}：{detail or ''}", "api-request", retryable=exc.code == 429 or exc.code >= 500)
                if not last.retryable:
                    raise last from exc
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = MinerUError(f"MinerU API 连接失败：{exc}", "api-request", retryable=True)
            else:
                if body.get("code", 0) == 0:
                    return body
                last = MinerUError(f"MinerU API 错误 {body.get('code')}：{body.get('msg')}", "api-request",
                                   retryable=str(body.get("code")) in {"-10005", "-60008"})
                if not last.retryable:
                    raise last
            if attempt < len(delays):
                time.sleep(delays[attempt])
        raise last  # type: ignore[misc]

    def submit(self, path: Path, options: dict) -> str:
        entry = {"name": path.name}
        if options.get("ocr") is not None:
            entry["is_ocr"] = options["ocr"]
        if options.get("pages"):
            entry["page_ranges"] = options["pages"]
        payload = {"files": [entry], "model_version": options.get("model", "vlm"),
                   "enable_formula": options.get("formula", False), "enable_table": options.get("table", True)}
        if options.get("language"):
            payload["language"] = options["language"]
        body = self._json("POST", "/file-urls/batch", payload)
        try:
            batch_id, upload_url = body["data"]["batch_id"], body["data"]["file_urls"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise MinerUError("MinerU 没有返回上传地址", "api-submit") from exc
        self.upload(upload_url, path)
        return str(batch_id)

    def upload(self, upload_url: str, path: Path) -> None:
        parsed = urlparse(upload_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise MinerUError("MinerU 返回的上传地址无效", "file-upload")
        connection_class = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
        data = path.read_bytes()
        last: MinerUError | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            connection = None
            try:
                connection = connection_class(parsed.hostname, parsed.port, timeout=600)
                connection.request("PUT", target, body=data, headers={"Content-Length": str(len(data))})
                response = connection.getresponse()
                response.read()
                if response.status < 300:
                    return
                last = MinerUError(f"文件上传失败：HTTP {response.status}", "file-upload",
                                   retryable=response.status == 429 or response.status >= 500)
                if not last.retryable:
                    raise last
            except MinerUError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                last = MinerUError(f"文件上传失败：{exc}", "file-upload", retryable=True)
            finally:
                if connection is not None:
                    connection.close()
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
        raise last  # type: ignore[misc]

    def wait(self, batch_id: str, timeout: int) -> dict:
        deadline, interval = time.monotonic() + timeout, 2.0
        while True:
            body = self._json("GET", f"/extract-results/batch/{batch_id}")
            results = (body.get("data") or {}).get("extract_result") or []
            if results:
                item = results[0]
                state = str(item.get("state"))
                if state in FINAL_STATES:
                    if state == "failed":
                        raise MinerUError(f"MinerU 解析失败：{item.get('err_msg') or item.get('err_code')}", "api-poll")
                    if not item.get("full_zip_url"):
                        raise MinerUError("MinerU 完成但没有返回结果地址", "result-download")
                    return item
            if time.monotonic() >= deadline:
                raise MinerUError(f"等待 MinerU 超过 {timeout} 秒", "api-poll", retryable=True)
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 1.7, 30.0)

    def download(self, url: str) -> bytes:
        last: MinerUError | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                with urlopen(Request(url), timeout=600) as response:
                    return response.read()
            except (URLError, TimeoutError, OSError) as exc:
                last = MinerUError(f"结果下载失败：{exc}", "result-download", retryable=True)
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
        raise last  # type: ignore[misc]


def safe_extract(data: bytes, destination: Path) -> list[str]:
    """Unpack the result zip, refusing any member that would land outside the folder."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            root = destination.resolve()
            for info in archive.infolist():
                target = (destination / PurePosixPath(info.filename)).resolve()
                if root not in target.parents and target != root:
                    raise MinerUError("结果压缩包里有不安全的路径", "result-download")
            archive.extractall(destination)
            return archive.namelist()
    except zipfile.BadZipFile as exc:
        raise MinerUError("MinerU 返回的结果不是有效 ZIP", "result-download") from exc


def client_from_env(api_base: str = DEFAULT_API_BASE) -> MinerUApi:
    token, _ = resolve_token()
    if not token:
        raise MinerUError(f"没有读取到 {TOKEN_ENV}", "token-check")
    if expiry_status()["level"] == "expired":
        raise MinerUError("Token 已过期", "token-check")
    return MinerUApi(token, api_base)


def extract(api: MinerUApi, source: Path, destination: Path, options: dict, timeout: int = 1800) -> list[str]:
    """Parse one file and unpack MinerU's full result into `destination`."""
    if not source.is_file():
        raise MinerUError(f"输入文件不存在：{source}", "input-check")
    destination.mkdir(parents=True, exist_ok=True)
    batch_id = api.submit(source, options)
    result = api.wait(batch_id, timeout)
    return safe_extract(api.download(str(result["full_zip_url"])), destination)


def make_test_pdf(path: Path) -> None:
    """A one-page PDF with no customer content, built without third-party packages."""
    stream = b"BT /F1 14 Tf 72 760 Td (MinerU API health check) Tj 0 -24 Td /F1 10 Tf (No customer data.) Tj ET"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    data, offsets = bytearray(b"%PDF-1.4\n"), []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    data.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(bytes(data))


def probe() -> dict:
    token, source = resolve_token()
    status = load_status()
    check = status.get("last_online_check") or {}
    this_month = date.today().strftime("%Y-%m")
    passed = bool(token) and status.get("token_fingerprint") == token_fingerprint(token) and \
        check.get("ok") is True and str(check.get("checked_at", "")).startswith(this_month)
    return {"ok": bool(token), "credential_source": source, "expiry": expiry_status(),
            "health_checked_this_month": passed, "last_online_check": check or None,
            "message": "已读取 MinerU Token" if token else f"没有读取到 {TOKEN_ENV}，请先运行 scripts/setup_mineru.cmd"}


def health_check(force: bool = False, list_output: bool = False, api: MinerUApi | None = None) -> dict:
    """Upload a harmless one-page PDF end to end. Reuses this month's pass to save quota."""
    state = probe()
    if not state["ok"] and api is None:
        return {**state, "ok": False, "failure_type": "credential-missing-or-expired",
                "next_action": FAILURE_ACTIONS["credential-missing-or-expired"]}
    if state["health_checked_this_month"] and not force and api is None:
        return {"ok": True, "reused_this_month": True, "expiry": state["expiry"], "message": "本月已通过在线检查，不重复消耗额度"}
    token, _ = resolve_token()
    api = api or MinerUApi(token or "", os.environ.get("MINERU_API_BASE", DEFAULT_API_BASE), retry_submit=True)
    status = load_status()
    try:
        with tempfile.TemporaryDirectory(prefix="mineru-health-") as temp:
            pdf = Path(temp) / "mineru_health_check.pdf"
            make_test_pdf(pdf)
            members = extract(api, pdf, Path(temp) / "out", {"model": "pipeline", "ocr": False, "table": False, "language": "en"}, timeout=600)
            text = "".join(p.read_text(encoding="utf-8", errors="replace") for p in (Path(temp) / "out").rglob("*.md"))
            if "MinerU API health check" not in text:
                raise MinerUError("在线测试返回的内容和测试文件对不上", "quality-check")
        status.update(token_fingerprint=token_fingerprint(token or "test"), expires_on=state["expiry"]["expires_on"],
                      last_online_check={"checked_at": datetime.now().astimezone().isoformat(timespec="seconds"), "ok": True},
                      last_online_failure=None)
        save_status(status)
        report = {"ok": True, "reused_this_month": False, "expiry": state["expiry"], "message": "Token、上传、解析、下载均正常，测试文件已删除"}
        if list_output:
            report["result_members"] = members
        return report
    except MinerUError as exc:
        status.update(last_online_failure={"checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                                           "failure_type": classify_failure(exc), "failure_stage": exc.stage})
        save_status(status)
        return failure_report(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="MinerU API：检查 Token、在线自检、解析单个文件")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="只在本机检查 Token 和到期日，不联网")
    health = sub.add_parser("health-check", help="上传一个不含客户信息的单页 PDF，验证整条链路")
    health.add_argument("--force", action="store_true", help="本月已通过也重新测试")
    health.add_argument("--list-output", action="store_true", help="列出结果压缩包里的文件名")
    one = sub.add_parser("extract", help="解析一个文件，把完整结果解压到指定目录")
    one.add_argument("--input", type=Path, required=True)
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--model", choices=("vlm", "pipeline"), default="vlm")
    one.add_argument("--ocr", action=argparse.BooleanOptionalAction, default=True)
    one.add_argument("--language", default="ch")
    one.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()

    if args.command == "probe":
        report = probe()
    elif args.command == "health-check":
        report = health_check(args.force, args.list_output)
    else:
        try:
            members = extract(client_from_env(), args.input.resolve(), args.out.resolve(),
                              {"model": args.model, "ocr": args.ocr, "table": True, "language": args.language}, args.timeout)
            report = {"ok": True, "out": str(args.out.resolve()), "members": len(members)}
        except MinerUError as exc:
            report = failure_report(exc)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else (3 if report.get("failure_type") in {"temporary-network-or-service", "sandbox-network-permission"} else 2)


if __name__ == "__main__":
    sys.exit(main())
