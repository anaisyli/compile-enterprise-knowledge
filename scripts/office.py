#!/usr/bin/env python3
"""Convert old Office files (.xls, .doc, .ppt) with LibreOffice, reliably on Windows.

LibreOffice keeps writing to its profile for a moment after it exits, so deleting a
fresh profile right away fails ("目录不是空的") even though the file was converted.
Here one profile is kept and reused, conversions run one at a time (two instances on
one profile hand work to each other and produce nothing), the profile lives on a short
ASCII path, and an empty result is retried once.
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
RETRY_WAIT = 3


class ConversionError(Exception):
    """LibreOffice ran but produced nothing; the message carries what it printed."""


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


def profile_dir(fallback: Path) -> Path:
    """A short ASCII path: LibreOffice can trip over long or non-ASCII profile paths."""
    for base in (os.environ.get("TEMP"), os.environ.get("TMP"), tempfile.gettempdir(), str(fallback)):
        if not base:
            continue
        candidate = Path(base) / "cek-libreoffice-profile"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return fallback / "libreoffice-profile"


def convert_office(path: Path, target: str, out: Path, name: str, timeout: int) -> Path:
    """Write <out>/<name>.<target> and return it; an existing result is reused."""
    result = out / f"{name}.{target}"
    if result.is_file():
        return result
    converter = find_converter()
    if not converter:
        raise LookupError("needs_libreoffice")
    out.mkdir(parents=True, exist_ok=True)
    profile = profile_dir(out).resolve().as_uri()
    last = ""
    with LOCK:
        for attempt in range(2):
            folder = Path(tempfile.mkdtemp(prefix="lo-", dir=out))
            try:
                done = subprocess.run([converter, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                                       "--convert-to", target, "--outdir", str(folder), long_path(path)],
                                      capture_output=True, timeout=timeout)
                produced = sorted(folder.glob(f"*.{target}"))
                if produced:
                    shutil.move(str(produced[0]), result)
                    return result
                last = (done.stderr or done.stdout or b"").decode("utf-8", errors="replace").strip()[-300:]
            finally:
                # A leftover temp folder is harmless; a failed cleanup must not undo a good conversion.
                shutil.rmtree(folder, ignore_errors=True)
            time.sleep(RETRY_WAIT * (attempt + 1))
    raise ConversionError(last or "LibreOffice 没有生成文件")
