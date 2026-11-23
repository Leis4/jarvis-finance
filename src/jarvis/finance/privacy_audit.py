from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path

SENSITIVE_FILE_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".pdf", ".pkl", ".pt", ".cbm", ".pyd"}
# These folders are expected in a developer working tree or contain user-local data.
# They must be excluded from clean release archives, but privacy-audit should not
# fail merely because a developer created .venv/ or build artifacts next to the
# source tree. Files in these folders are skipped; sensitive files outside them
# are still reported.
IGNORED_DIR_NAMES = {
    "datasets",
    "models",
    "sber_9_months",
    "sber_inbox",
    "sber_archive",
    "telegram_staging",
    "downloads",
    "exports",
    "backups",
    ".venv",
    "venv",
    "env",
    "build",
    "dist",
    "node_modules",
    "coverage",
    "htmlcov",
    "release_artifacts",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
IGNORED_DIR_SUFFIXES = (".egg-info", ".dist-info")
PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone_ru": re.compile(r"(?:\+7|8)[\s\-()]?\d{3}[\s\-()]?\d{3}[\s\-()]?\d{2}[\s\-()]?\d{2}"),
    "long_account_or_card_like_number": re.compile(r"\b\d{12,20}\b"),
}


@dataclass(frozen=True)
class PrivacyFinding:
    path: str
    kind: str
    detail: str


@dataclass(frozen=True)
class PrivacyAuditReport:
    ok: bool
    root: str
    scanned_files: int
    findings: list[PrivacyFinding]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        return payload


def audit_privacy(root: str | Path, *, max_text_bytes: int = 200_000) -> PrivacyAuditReport:
    base = Path(root).resolve()
    findings: list[PrivacyFinding] = []
    scanned = 0
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        rel = path.relative_to(base).as_posix()
        relative_parts = path.relative_to(base).parts
        parts = set(relative_parts)
        if parts & IGNORED_DIR_NAMES:
            continue
        if any(part.endswith(IGNORED_DIR_SUFFIXES) for part in relative_parts):
            continue
        if path.suffix.lower() in SENSITIVE_FILE_SUFFIXES:
            findings.append(PrivacyFinding(rel, "sensitive_file_type", f"Файл с расширением {path.suffix} не должен попадать в source-release."))
            continue
        if path.suffix.lower() not in {".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml", ".csv", ".ts", ".tsx", ".js", ".css", ".html"}:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:max_text_bytes]
        except Exception:  # noqa: BLE001, S112
            continue
        for kind, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(PrivacyFinding(rel, kind, "Найден потенциальный персональный идентификатор; проверь вручную."))
    return PrivacyAuditReport(ok=not findings, root=str(base), scanned_files=scanned, findings=findings)


def render_privacy_audit(report: PrivacyAuditReport) -> str:
    lines = ["Privacy audit", f"ok: {report.ok}", f"root: {report.root}", f"scanned_files: {report.scanned_files}", f"findings: {len(report.findings)}"]
    for finding in report.findings[:100]:
        lines.append(f"- {finding.kind}: {finding.path} — {finding.detail}")
    if len(report.findings) > 100:
        lines.append(f"... and {len(report.findings) - 100} more")
    return "\n".join(lines)
