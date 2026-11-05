#!/usr/bin/env python3
"""Convert old Office files (.xls, .doc, .ppt) with LibreOffice, without ever hanging a run.

- Each conversion gets a fresh LibreOffice profile on a short ASCII path, so one crash
  cannot poison the conversions after it. LibreOffice keeps writing to a profile for a
  moment after it exits; a profile that cannot be deleted yet is left, never an error.
- A conversion that takes too long is killed with its whole process tree.
- After a few failures in a row (a sandbox that forbids LibreOffice to write, a broken
  install), the rest are not tried: they are reported at once instead of each waiting.
- Conversions run one at a time; two LibreOffice instances started together can hand
  work to each other and both produce nothing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from kb_common import long_path


LOCK = threading.Lock()
TIMEOUT = 120
RETRY_WAIT = 2
GIVE_UP_AFTER = 3


class ConversionError(Exception):
    """LibreOffice ran but produced nothing; the message carries what it printed."""


class _State:
    failures = 0
    disabled: str | None = None


def find_converter() -> str | None:
    """LIBREOFFICE_PATH wins, then PATH, then the usual install folders."""
    named = os.environ.get("LIBREOFFICE_PATH") or os.environ.get("SOFFICE_PATH")
    if named and Path(named).is_file():
        return named
    found = shutil.which("soffice") or shutil.which("libreoffice")
    if found:
        return found
    roots = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), r"C:\Program Files", r"C:\Program Files (x86)"]
    for root in roots:
        candidate = Path(root or "") / "LibreOffice" / "program" / "soffice.exe"
        if root and candidate.is_file():
            return str(candidate)
    for candidate in ("/Applications/LibreOffice.app/Contents/MacOS/soffice", "/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice"):
        if Path(candidate).is_file():
            return candidate
    return None


def disable(reason: str) -> None:
    """Stop trying LibreOffice in this run; every later conversion reports the reason at once."""
    _State.disabled = reason


def disabled() -> str | None:
    return _State.disabled


def short_temp(fallback: Path) -> Path:
    """A writable folder on a short ASCII path: LibreOffice trips over long or non-ASCII ones."""
    for base in (os.environ.get("TEMP"), os.environ.get("TMP"), tempfile.gettempdir(), str(fallback)):
        if base and base.isascii():
            try:
                Path(base).mkdir(parents=True, exist_ok=True)
                return Path(tempfile.mkdtemp(prefix="cek-lo-", dir=base))
            except OSError:
                continue
    fallback.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="cek-lo-", dir=fallback))


def kill_tree(process: subprocess.Popen) -> None:
    """soffice.exe starts soffice.bin; killing only the first leaves the second running."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True)
    else:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def run_once(converter: str, path: Path, target: str, work: Path, timeout: int) -> tuple[Path | None, str]:
    profile, outdir = work / "profile", work / "out"
    outdir.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([converter, f"-env:UserInstallation={profile.resolve().as_uri()}", "--headless", "--norestore",
                                "--nologo", "--nodefault", "--convert-to", target, "--outdir", str(outdir), long_path(path)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(process)
        raise
    produced = sorted(outdir.glob(f"*.{target}"))
    message = (err or out or b"").decode("utf-8", errors="replace").strip()[-300:]
    return (produced[0] if produced else None), message


def convert_office(path: Path, target: str, out: Path, name: str, timeout: int = TIMEOUT) -> Path:
    """Write <out>/<name>.<target> and return it; an existing result is reused."""
    result = out / f"{name}.{target}"
    if result.is_file():
        return result
    if _State.disabled:
        raise LookupError("needs_libreoffice")
    converter = find_converter()
    if not converter:
        raise LookupError("needs_libreoffice")
    out.mkdir(parents=True, exist_ok=True)
    timeout = min(timeout, TIMEOUT)
    with LOCK:
        last = ""
        for attempt in range(2):
            work = short_temp(out)
            try:
                produced, last = run_once(converter, path, target, work, timeout)
                if produced:
                    shutil.move(str(produced), result)
                    _State.failures = 0
                    return result
            except subprocess.TimeoutExpired:
                last = f"超过 {timeout} 秒没有转完，已结束 LibreOffice"
                break  # A hang is not worth a second wait.
            finally:
                shutil.rmtree(work, ignore_errors=True)
            time.sleep(RETRY_WAIT)
        _State.failures += 1
        if _State.failures >= GIVE_UP_AFTER:
            disable(f"连续 {_State.failures} 个文件转换失败，最后一次：{last}")
    raise ConversionError(last or "LibreOffice 没有生成文件")


def self_test(work: Path) -> str | None:
    """Convert a tiny workbook; return why it failed, or None when LibreOffice works here."""
    from check_tools import WORKBOOK
    import zipfile
    work.mkdir(parents=True, exist_ok=True)
    source = work / "cek-self-test.xlsx"
    with zipfile.ZipFile(source, "w") as archive:
        for member, text in WORKBOOK.items():
            archive.writestr(member, text)
    target = work / "cek-self-test.ods"
    try:
        target.unlink(missing_ok=True)
        convert_office(source, "ods", work, "cek-self-test", 60)
        return None
    except (LookupError, ConversionError, OSError) as exc:
        return str(exc) or type(exc).__name__
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        _State.failures = 0
