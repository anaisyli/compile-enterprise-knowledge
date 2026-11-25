"""Inputs consumed by a completed build and its publishing receipt."""

from pathlib import Path

from kb_common import MANIFEST_NAME, sha256_file

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


def package_digests(packages: list[Path]) -> dict[str, str | None]:
    return {str(path.resolve()): file_digest(path / MANIFEST_NAME) for path in packages}


def extracted_inputs(build: Path, mineru_dir: Path | None = None) -> dict:
    directories = {
        "mineru": mineru_dir or build / "mineru-units",
        "reducto": build / "reducto-units",
    }
    return {
        "ocr": {engine: {p.stem: sha256_file(p) for p in folder.glob("*.json")}
                for engine, folder in directories.items()},
        "page_check": file_digest(build / "page-check/results.json"),
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


def batch_id(config: dict) -> str | None:
    return (config.get("stage_authorization") or {}).get("batch_id")


def select_batch_ocr(entries: list[dict], scope: dict) -> tuple[list[dict], list[str]]:
    if not scope.get("batch_scoped"):
        return entries, []
    approved = set(scope.get("approved_item_ids") or [])
    return ([entry for entry in entries if entry["item_id"] in approved],
            sorted(entry["item_id"] for entry in entries if entry["item_id"] not in approved))
