"""Inputs consumed by a completed build and its publishing receipt."""

import hashlib
import json
from pathlib import Path

from kb_common import MANIFEST_NAME, load_json, load_jsonl, long_path, sha256_file

PIPELINE_CHECKPOINT = "pipeline-checkpoint.json"
VERIFY_REPORT = "校验结果.json"
RECEIPT_PATH = "05_数据与审核/50_运行记录/compile-enterprise-knowledge-receipt.json"
ROUTING_KEYS = (
    "ask_client", "categories", "client_answer_confirmations", "client_answers", "client_answers_units",
    "d_drop", "d_keep", "doc_roles", "doc_titles", "drawing_items", "excluded_items", "header_map",
    "no_ocr_items", "not_drawing_items", "own_terms", "route_overrides", "sample_review",
    "separate_format_items", "sheet_columns", "websites",
)


def file_digest(path: Path) -> str | None:
    return sha256_file(path) if path.is_file() else None


def stable_json_digest(path: Path, *, jsonl: bool = False) -> str | None:
    if not path.is_file():
        return None
    value = load_jsonl(path) if jsonl else load_json(path)

    def normalize(item):
        if isinstance(item, list):
            return [normalize(child) for child in item]
        if isinstance(item, dict):
            return {key: (Path(child).name if key == "image" and isinstance(child, str) else normalize(child))
                    for key, child in item.items()}
        return item

    payload = json.dumps(normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def package_digests(packages: list[Path]) -> dict[str, str | None]:
    return {str(path.resolve()): file_digest(path / MANIFEST_NAME) for path in packages}


def package_input_digests(packages: list[Path]) -> dict[str, str | None]:
    """Hash copied source files and package sidecars consumed outside the manifest."""
    digests: dict[str, str | None] = {}
    for raw in packages:
        package = raw.resolve()
        manifest = load_json(package / MANIFEST_NAME)
        for asset in manifest.get("assets", []):
            if asset.get("copy_status") != "copied" or not asset.get("path"):
                continue
            item_id = asset.get("item_id")
            if not item_id:
                continue
            source = package / asset["path"]
            try:
                exists = Path(long_path(source)).is_file()
                digest = sha256_file(source) if exists else None
            except OSError:
                digest = None
            digests[f"{package}::asset:{item_id}"] = digest
        for relative in ("审计记录/triage-results.json", "C-知识点/knowledge-points.jsonl", "客户确认表.xlsx"):
            sidecar = package / relative
            try:
                digest = sha256_file(sidecar) if Path(long_path(sidecar)).is_file() else None
            except OSError:
                digest = None
            digests[f"{package}::file:{relative}"] = digest
    return digests


def extracted_inputs(build: Path, mineru_dir: Path | None = None) -> dict:
    directories = {
        "mineru": mineru_dir or build / "mineru-units",
        "reducto": build / "reducto-units",
    }
    return {
        "ocr": {engine: {p.stem: sha256_file(p) for p in folder.glob("*.json")}
                for engine, folder in directories.items()},
        "page_check": {
            "todo": stable_json_digest(build / "page-check/todo.jsonl", jsonl=True),
            "plan": stable_json_digest(build / "page-check/plan.json"),
            "results": stable_json_digest(build / "page-check/results.json"),
        },
    }


def routing_inputs(build: Path, config: dict) -> dict:
    return {
        "settings": {key: config[key] for key in ROUTING_KEYS if key in config},
        "samples": file_digest(build / "sample-decisions.json"),
        "sheet_columns": file_digest(build / "cell-review/sheet-columns.json"),
        "website_pages": file_digest(build / "website/pages.json"),
    }


def curation_inputs(build: Path) -> dict[str, str]:
    return {p.relative_to(build).as_posix(): sha256_file(p)
            for name in ("page.md", "ledger.jsonl") for p in (build / "curation").glob(f"*/{name}")}


def planned_outputs(run: Path, build_stage: str) -> dict[str, Path]:
    """Files a publish would write, excluding preserved curated pages in a source-only build."""
    from publish_vault import planned_files

    files = planned_files(run)
    if build_stage == "sources_only":
        formal = "03_正式知识/10_客户知识/"
        files = {key: path for key, path in files.items() if not key.startswith(formal)}
    return files


def output_digests(run: Path, build_stage: str) -> dict[str, str]:
    return {key: sha256_file(path) for key, path in planned_outputs(run, build_stage).items()}


def batch_id(config: dict) -> str | None:
    return (config.get("stage_authorization") or {}).get("batch_id")


def select_batch_ocr(entries: list[dict], scope: dict) -> tuple[list[dict], list[str]]:
    if not scope.get("batch_scoped"):
        return entries, []
    approved = set(scope.get("approved_item_ids") or [])
    return ([entry for entry in entries if entry["item_id"] in approved],
            sorted(entry["item_id"] for entry in entries if entry["item_id"] not in approved))
