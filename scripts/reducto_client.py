#!/usr/bin/env python3
"""Reducto API client: key checks, a free key check, and parsing chosen pages of one PDF.

Standard library only, like mineru_client.py. The key is read from REDUCTO_API_KEY and is never
written to a project, a log, or a status file. Uploads are deleted after parsing where the plan allows
it (Growth/Enterprise); on other plans Reducto expires them on its own schedule.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen


KEY_ENV = "REDUCTO_API_KEY"
DEFAULT_API_BASE = "https://platform.reducto.ai"
KEY_PAGE = "https://studio.reducto.ai"
PRICING_PAGE = "https://reducto.ai/pricing"
RETRY_DELAYS = (2.0, 5.0)
MODEL = "r-1"
# Blocks that carry the page's own words. Figures hold the model's description mixed with what it saw,
# so they are kept apart; page numbers are layout, not content.
TEXT_BLOCKS = {"Title", "Section Header", "Text", "List Item", "Key Value", "Table", "Header", "Footer", "Comment", "Signature"}
FIGURE_BLOCKS = {"Figure"}

FAILURE_LABELS = {
    "credential-missing": "没有读取到 Reducto API Key",
    "credential-invalid": "API Key 无效或已撤销",
    "quota-or-rate-limit": "额度不足或频率限制",
    "sandbox-network-permission": "当前 AI 工具没有联网权限",
    "temporary-network-or-service": "临时网络或服务故障",
    "api-or-file-processing": "Reducto 处理这个文件失败",
    "local-workflow": "本地输入或路径问题",
}
FAILURE_ACTIONS = {
    "credential-missing": f"运行 scripts/setup_reducto.cmd 配置 API Key（在 {KEY_PAGE} → API Keys 创建），然后完全重启 AI 工具。",
    "credential-invalid": "Key 可能被撤销或复制不完整。在 Studio 重新创建后运行 setup_reducto.cmd，再完全重启 AI 工具。",
    "quota-or-rate-limit": f"到 {KEY_PAGE} 查看额度和账单（价格见 {PRICING_PAGE}）；这不是 Key 坏了，不要重新配置。",
    "sandbox-network-permission": "在 AI 工具弹出的权限窗口里允许这条命令联网后重试；不要更换 Key。",
    "temporary-network-or-service": "稍后重试，最多 2 次；仍失败看 https://status.reducto.ai 。",
    "api-or-file-processing": "核对文件是否损坏、加密或过大（直传 100 MB，本脚本用预签名上传可到 5 GB）；连续失败就改用 MinerU 或 AI 看图。",
    "local-workflow": "修正输入路径或参数后重试。",
}


class ReductoError(RuntimeError):
    def __init__(self, message: str, stage: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.stage = stage
        self.status = status
        self.retryable = retryable


def resolve_key() -> str | None:
    return os.environ.get(KEY_ENV, "").strip() or None


def classify_failure(exc: ReductoError) -> str:
    text = str(exc).casefold()
    if exc.stage == "key-check":
        return "credential-missing"
    if "winerror 10013" in text or "访问套接字" in text:
        return "sandbox-network-permission"
    if exc.status == 401 or "invalid access token" in text:
        return "credential-invalid"
    if exc.status in {402, 429} or any(word in text for word in ("credit", "quota", "insufficient", "rate limit")):
        return "quota-or-rate-limit"
    if exc.retryable:
        return "temporary-network-or-service"
    if exc.stage in {"upload", "parse", "poll", "result"}:
        return "api-or-file-processing"
    return "local-workflow"


def failure_report(exc: ReductoError) -> dict:
    kind = classify_failure(exc)
    return {"ok": False, "failure_type": kind, "failure_label": FAILURE_LABELS[kind], "failure_stage": exc.stage,
            "error": str(exc), "next_action": FAILURE_ACTIONS[kind]}


class ReductoApi:
    def __init__(self, key: str, base_url: str = DEFAULT_API_BASE, request_timeout: int = 120):
        self.key = key
        self.base_url = base_url.rstrip("/")
        self.request_timeout = request_timeout

    def _call(self, method: str, path: str, payload: dict | None = None, stage: str = "api", retry: bool | None = None) -> dict:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.key}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        # Submitting twice could be billed twice, so only reads retry by default.
        delays = RETRY_DELAYS if (method in {"GET", "DELETE"} if retry is None else retry) else ()
        last: ReductoError | None = None
        for attempt in range(len(delays) + 1):
            try:
                request = Request(self.base_url + path, data=data, headers=headers, method=method)
                with urlopen(request, timeout=self.request_timeout) as response:
                    raw = response.read().decode("utf-8")
                    return json.loads(raw) if raw.strip() else {}
            except HTTPError as exc:
                detail = exc.read(4096).decode("utf-8", "replace")
                try:
                    body = json.loads(detail)
                    error = body.get("error") if isinstance(body.get("error"), dict) else {}
                    detail = error.get("message") or body.get("detail") or detail
                except (json.JSONDecodeError, AttributeError):
                    pass
                last = ReductoError(f"HTTP {exc.code}：{str(detail)[:300]}", stage, status=exc.code,
                                    retryable=exc.code in {408, 409, 429} or exc.code >= 502)
                if not last.retryable:
                    raise last from exc
            except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last = ReductoError(f"Reducto API 连接失败：{exc}", stage, retryable=True)
            if attempt < len(delays):
                time.sleep(delays[attempt])
        raise last  # type: ignore[misc]

    def check_key(self) -> None:
        """Free: lists at most one job. 200 means reachable and the key is valid."""
        self._call("GET", "/jobs?limit=1", stage="key-online")

    def upload(self, path: Path) -> str:
        """Presigned upload: no multipart needed, and files up to 5 GB. The extension tells Reducto the file type."""
        extension = path.suffix.lstrip(".").casefold()
        body = self._call("POST", "/upload" + (f"?extension={quote(extension)}" if extension else ""), stage="upload", retry=True)
        file_id, target = body.get("file_id"), body.get("presigned_url")
        if not file_id or not target:
            raise ReductoError("Reducto 没有返回上传地址", "upload")
        parsed = urlparse(target)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ReductoError("Reducto 返回的上传地址无效", "upload")
        data = path.read_bytes()
        last: ReductoError | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, timeout=600)
            try:
                connection.request("PUT", (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else ""), body=data,
                                   headers={"Content-Length": str(len(data))})
                response = connection.getresponse()
                response.read()
                if response.status < 300:
                    return str(file_id)
                last = ReductoError(f"文件上传失败：HTTP {response.status}", "upload", status=response.status,
                                    retryable=response.status == 429 or response.status >= 500)
                if not last.retryable:
                    raise last
            except (OSError, http.client.HTTPException) as exc:
                last = ReductoError(f"文件上传失败：{exc}", "upload", retryable=True)
            finally:
                connection.close()
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
        raise last  # type: ignore[misc]

    def submit(self, file_id: str, pages: list[int] | None) -> str:
        settings: dict = {"model": MODEL}
        if pages:
            settings["page_range"] = page_ranges(pages)
        body = self._call("POST", "/parse_async", {"input": file_id, "settings": settings,
                                                   "formatting": {"table_output_format": "html"}}, stage="parse", retry=False)
        if not body.get("job_id"):
            raise ReductoError("Reducto 没有返回任务编号", "parse")
        return str(body["job_id"])

    def wait(self, job_id: str, timeout: int) -> dict:
        deadline, interval = time.monotonic() + timeout, 2.0
        while True:
            job = self._call("GET", f"/job/{quote(job_id, safe='')}", stage="poll")
            status = str(job.get("status", ""))
            if status == "Completed":
                return job.get("result") or {}
            if status == "Failed":
                raise ReductoError(f"Reducto 解析失败：{job.get('reason') or job.get('error') or job}", "poll")
            if time.monotonic() >= deadline:
                raise ReductoError(f"等待 Reducto 超过 {timeout} 秒", "poll", retryable=True)
            time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
            interval = min(interval * 1.5, 15.0)

    def chunks(self, result: dict) -> list[dict]:
        inner = result.get("result") if isinstance(result.get("result"), dict) else result
        if inner.get("type") == "url":
            try:
                with urlopen(Request(str(inner["url"])), timeout=600) as response:
                    loaded = json.loads(response.read().decode("utf-8"))
            except (URLError, TimeoutError, OSError, json.JSONDecodeError, KeyError) as exc:
                raise ReductoError(f"结果下载失败：{exc}", "result", retryable=True) from exc
            return loaded if isinstance(loaded, list) else loaded.get("chunks", [])
        return inner.get("chunks") or []

    def delete_upload(self, file_id: str) -> str:
        """"deleted", "plan" (only Growth/Enterprise may delete; others expire on Reducto's schedule) or "failed"."""
        try:
            self._call("DELETE", f"/upload/{quote(file_id, safe='')}", stage="cleanup")
            return "deleted"
        except ReductoError as exc:
            return "plan" if exc.status == 422 and "not applicable" in str(exc).casefold() else "failed"


def page_ranges(pages: list[int]) -> list[dict]:
    ranges: list[dict] = []
    for page in sorted(set(pages)):
        if ranges and page == ranges[-1]["end"] + 1:
            ranges[-1]["end"] = page
        else:
            ranges.append({"start": page, "end": page})
    return ranges


def by_page(chunks: list[dict]) -> dict[int, dict]:
    """Group blocks per source page: the page's own words, and figure descriptions kept apart."""
    pages: dict[int, dict] = {}
    for chunk in chunks:
        for block in chunk.get("blocks") or []:
            content = (block.get("content") or "").strip()
            bbox = block.get("bbox") or {}
            # With page_range, "page" counts the parsed pages only; "original_page" is the page in the file.
            page = bbox.get("original_page", bbox.get("page"))
            if not content or not isinstance(page, int):
                continue
            entry = pages.setdefault(page, {"text": [], "figures": [], "low_confidence": 0})
            if block.get("type") in FIGURE_BLOCKS:
                entry["figures"].append(content)
            elif block.get("type") in TEXT_BLOCKS:
                entry["text"].append(content)
            if block.get("confidence") == "low":
                entry["low_confidence"] += 1
    return pages


def client_from_env(api_base: str | None = None) -> ReductoApi:
    key = resolve_key()
    if not key:
        raise ReductoError(f"没有读取到 {KEY_ENV}", "key-check")
    return ReductoApi(key, api_base or os.environ.get("REDUCTO_API_BASE", DEFAULT_API_BASE))


def parse_pages(api: ReductoApi, source: Path, pages: list[int] | None, timeout: int = 1800) -> dict:
    """Upload one PDF, parse the chosen pages (1-based, as in the file), delete the upload."""
    if not source.is_file():
        raise ReductoError(f"输入文件不存在：{source}", "input-check")
    file_id = api.upload(source)
    try:
        job_id = api.submit(file_id, pages)
        result = api.wait(job_id, timeout)
        chunks = api.chunks(result)
    finally:
        cleaned = api.delete_upload(file_id)
    usage = result.get("usage") or {}
    return {"job_id": job_id, "pages": by_page(chunks), "num_pages": usage.get("num_pages"),
            "credits": usage.get("credits"), "upload_cleanup": cleaned}


def make_test_pdf(path: Path, lines: list[str]) -> None:
    """A PDF with one line per page and no customer content, built without third-party packages."""
    count = len(lines)
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [" + b" ".join(f"{3 + 2 * i} 0 R".encode() for i in range(count)) + b"] /Count " + str(count).encode() + b" >>"]
    font = 3 + 2 * count
    for index, line in enumerate(lines):
        stream = f"BT /F1 18 Tf 72 760 Td ({line}) Tj ET".encode()
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * index} 0 R >>".encode())
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    data, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    data.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets))
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.write_bytes(bytes(data))


def probe() -> dict:
    key = resolve_key()
    return {"ok": bool(key), "message": "已读取 Reducto API Key" if key else f"没有读取到 {KEY_ENV}，请先运行 scripts/setup_reducto.cmd"}


def health_check(parse: bool = False, api: ReductoApi | None = None) -> dict:
    """Without --parse: free key check. With it: parse page 2 of a harmless 3-page PDF end to end,
    which also proves page numbers come back as they are in the file."""
    try:
        api = api or client_from_env()
        api.check_key()
        if not parse:
            return {"ok": True, "message": "Key 有效，API 可以连上（未消耗额度）"}
        marker = f"REDUCTO CHECK {uuid.uuid4().hex[:6].upper()}"
        with tempfile.TemporaryDirectory(prefix="reducto-health-") as temp:
            pdf = Path(temp) / "reducto_health_check.pdf"
            make_test_pdf(pdf, ["Page one, no customer data.", marker, "Page three, no customer data."])
            result = parse_pages(api, pdf, [2], timeout=600)
        text = "\n".join(result["pages"].get(2, {}).get("text", []))
        if marker not in text.upper():
            raise ReductoError(f"在线测试返回的内容和测试文件对不上（拿到的页：{sorted(result['pages'])}）", "result")
        cleanup = {"deleted": "测试文件已从 Reducto 删除", "plan": "当前套餐不能主动删除上传，Reducto 会按其保留期自动清除",
                   "failed": "测试文件删除失败，Reducto 会按其保留期自动清除"}[result["upload_cleanup"]]
        return {"ok": True, "message": f"Key、上传、解析、按页取结果均正常；{cleanup}", "pages_billed": result["num_pages"]}
    except ReductoError as exc:
        return failure_report(exc)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reducto API：检查 Key、在线自检、解析一个 PDF 的指定页")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("probe", help="只在本机检查 Key，不联网")
    health = sub.add_parser("health-check", help="联网检查 Key（免费）；加 --parse 再解析一页测试 PDF（消耗约 1 页额度）")
    health.add_argument("--parse", action="store_true")
    one = sub.add_parser("parse", help="解析一个 PDF，把按页结果写成 JSON")
    one.add_argument("--input", type=Path, required=True)
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--pages", help="页码，例如 1,3-5；不写就是整份")
    args = parser.parse_args()
    if args.command == "probe":
        report = probe()
    elif args.command == "health-check":
        report = health_check(args.parse)
    else:
        pages = None
        if args.pages:
            pages = []
            for part in args.pages.split(","):
                first, _, last = part.strip().partition("-")
                pages.extend(range(int(first), int(last or first) + 1))
        try:
            result = parse_pages(client_from_env(), args.input.resolve(), pages)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            report = {"ok": True, "out": str(args.out.resolve()), "pages": sorted(result["pages"]), "credits": result["credits"]}
        except ReductoError as exc:
            report = failure_report(exc)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else (3 if report.get("failure_type") in {"temporary-network-or-service", "sandbox-network-permission"} else 2)


if __name__ == "__main__":
    sys.exit(main())
