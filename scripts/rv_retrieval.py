#!/usr/bin/env python3
"""Research Vault Retrieval - read-only, NOTE-FIRST literature retrieval.

Subcommands
  find     Search analytical notes first. Fulltext is scanned only when
           --include-fulltext is passed explicitly (supplementary recall).
  resolve  Resolve one paper to its note, fulltext and PDF; report conflicts.
  verify   Verify a quotation really exists in a Markdown fulltext or a PDF page.
  stats    Read-only vault diagnostics: coverage and pairing issues.

Hard rules
  * This script never writes to the Vault (no cache, no on-disk index).
  * Identities are never guessed; ambiguous cases are reported, not resolved.
  * Standard library only; Python 3.9+.

Exit codes
  0  success (verify: every quote verified)
  1  unresolved / ambiguous / quote not found
  2  environment or usage error (missing vault, missing PDF tool, bad JSON)
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SKILL_NAME = "research-vault-retrieval"
TOOL_VERSION = "0.1.0"

DEFAULT_MAX_FILE_MB = 8.0

EXCLUDED_DIR_NAMES = {
    ".git", ".obsidian", ".trash", ".meta", ".agents", ".claude", ".codex",
    ".vscode", "node_modules", "_staging", "_validation", "state", "cache",
    "logs", "templates", "dist", "target", "_index",
}
FULLTEXT_DIR_NAMES = {"03fulltext", "fulltext"}
NOTE_TYPES = {"literature-note", "paper-note"}
FULLTEXT_TYPES = {"literature-fulltext"}
NOTE_MARKER_FILENAMES = {"notes.md"}

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
QUERY_SPLIT_RE = re.compile(r"[\s,，。;；、/\\|()（）【】\[\]{}<>\"'“”‘’:：!?！？—–\-]+")

NOTE_FIELDS: Tuple[Tuple[str, float], ...] = (
    ("title", 8.0),
    ("aliases", 6.0),
    ("tags", 5.0),
    ("key_finding", 5.0),
    ("theme", 4.0),
    ("methodology", 3.0),
    ("core_variable", 3.0),
    ("study_area", 2.0),
    ("relevance", 2.0),
    ("body", 1.0),
)

SCALAR_PAGE_MARKERS = (
    re.compile(r"^\s*<!--\s*page[:\s]*(\d+)\s*-->\s*$", re.IGNORECASE),
    re.compile(r"^\s*\{\{\s*page[:\s]*(\d+)\s*\}\}\s*$", re.IGNORECASE),
    re.compile(r"^\s*\[\[\s*page[:\s]*(\d+)\s*\]\]\s*$", re.IGNORECASE),
    re.compile(r"^\s*\[\s*page[:\s]*(\d+)\s*\]\s*$", re.IGNORECASE),
    re.compile(r"^\s*<\s*page[:\s]*(\d+)\s*>\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{3,}\s*page\s+(\d+)\s*-{3,}\s*$", re.IGNORECASE),
)


class VaultError(RuntimeError):
    """Environment / usage problem (exit code 2)."""


# --------------------------------------------------------------------------
# Tiny frontmatter reader (top-level scalar keys + inline/block lists)
# --------------------------------------------------------------------------

def _split_inline_list(inner: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    quote: Optional[str] = None
    for char in inner:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
        elif char in ("'", '"'):
            quote = char
            current.append(char)
        elif char == ",":
            parts.append("".join(current).strip())
            current = []
        else:
            current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str, int]:
    """Return (fields, body, body start line). Tolerant; ignores nested maps."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text, 1
    end: Optional[int] = None
    for index in range(1, min(len(lines), 4000)):
        if lines[index].strip() in ("---", "..."):
            end = index
            break
    if end is None:
        return {}, text, 1

    fields: Dict[str, Any] = {}
    current_key: Optional[str] = None
    for raw_line in lines[1:end]:
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace():
            stripped = line.strip()
            if current_key and stripped.startswith("- "):
                bucket = fields.get(current_key)
                if isinstance(bucket, list):
                    bucket.append(_scalar(stripped[2:]))
            continue
        if ":" not in line:
            current_key = None
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][\w-]*", key):
            current_key = None
            continue
        value = value.strip()
        if not value:
            fields[key] = []
            current_key = key
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            fields[key] = [_scalar(item) for item in _split_inline_list(inner)] if inner else []
            current_key = None
        else:
            fields[key] = _scalar(value)
            current_key = None
    body = "\n".join(lines[end + 1:])
    return fields, body, end + 2


def field_text(fields: Dict[str, Any], key: str, limit: int = 40000) -> str:
    value = fields.get(key)
    if value is None:
        return ""
    if isinstance(value, list):
        text = " ".join(str(item) for item in value)
    else:
        text = str(value)
    return text[:limit]


def field_list(fields: Dict[str, Any], key: str) -> List[str]:
    value = fields.get(key)
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


# --------------------------------------------------------------------------
# Records and vault indexing
# --------------------------------------------------------------------------

@dataclass
class Record:
    rel_path: str
    abs_path: Path
    kind: str  # "note" | "fulltext"
    title: str
    aliases: List[str]
    tags: List[str]
    body: str
    fields: Dict[str, Any]
    identity: str
    zotero_key: Optional[str] = None
    doi: Optional[str] = None
    fulltext_path: Optional[str] = None
    note_path: Optional[str] = None
    pdf_path: Optional[str] = None


def _first_heading(body: str) -> str:
    for line in body.splitlines():
        match = re.match(r"^#\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return ""


def _paper_metadata_title(absolute: Path) -> str:
    """Canonical title from an Agentero paper unit's metadata.json (best effort)."""
    if absolute.name.lower() != "notes.md":
        return ""
    metadata = absolute.parent / "metadata.json"
    if not metadata.is_file() or metadata.is_symlink():
        return ""
    try:
        if metadata.stat().st_size > 512 * 1024:
            return ""
        payload = json.loads(metadata.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    title = payload.get("title") if isinstance(payload, dict) else None
    return title.strip() if isinstance(title, str) else ""


def _readable_alias(value: str) -> bool:
    text = value.strip()
    if len(text) < 3 or text.isdigit():
        return False
    if re.fullmatch(r"[0-9a-fA-F][0-9a-fA-F\-_.]{7,}", text) and " " not in text:
        return False
    return bool(re.search(r"[A-Za-z\u4e00-\u9fff]", text))


def _record_from_file(path: Path, rel_path: str, kind: str,
                      max_bytes: int) -> Optional[Record]:
    try:
        if path.stat().st_size > max_bytes:
            return None
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    fields, body, _ = parse_frontmatter(text)
    aliases = field_list(fields, "aliases") or field_list(fields, "alias")
    tags = field_list(fields, "tags")
    title = field_text(fields, "title").strip()
    if not title:
        title = _paper_metadata_title(path)
    if not title:
        title = _first_heading(body)
    if not title:
        readable = [alias for alias in aliases if _readable_alias(alias)]
        title = readable[0] if readable else (aliases[0].strip() if aliases else "")
    if not title:
        title = "Notes" if path.stem.lower() == "notes" else path.stem
    zotero_key = field_text(fields, "zotero_key").strip() or None
    doi = field_text(fields, "doi").strip() or None
    identity = zotero_key or doi or ("path:" + rel_path)
    return Record(
        rel_path=rel_path,
        abs_path=path,
        kind=kind,
        title=title,
        aliases=aliases,
        tags=tags,
        body=body,
        fields=fields,
        identity=identity,
        zotero_key=zotero_key,
        doi=doi,
        fulltext_path=field_text(fields, "fulltext_path").strip() or None,
        note_path=field_text(fields, "note_path").strip() or None,
        pdf_path=field_text(fields, "pdf_path").strip() or None,
    )


def _looks_like_paper_folder(absolute: Path) -> bool:
    parent = absolute.parent
    if (parent / "metadata.json").is_file() or (parent / "PAPER.md").is_file():
        return True
    if any(item.suffix.lower() == ".pdf" for item in parent.glob("*")):
        return True
    if (parent / "attachments").is_dir() and any(
            item.suffix.lower() == ".pdf" for item in (parent / "attachments").glob("*")):
        return True
    return False


def _classify(absolute: Path, rel_parts: Sequence[str],
              fields: Dict[str, Any]) -> Optional[str]:
    type_value = str(fields.get("type") or "").strip().lower()
    tags = {str(item).strip().lower() for item in field_list(fields, "tags")}
    name = rel_parts[-1].lower() if rel_parts else ""
    dirs = {part.lower() for part in rel_parts[:-1]}
    if type_value in FULLTEXT_TYPES or (dirs & FULLTEXT_DIR_NAMES):
        return "fulltext"
    if type_value in NOTE_TYPES or "literature-note" in tags:
        return "note"
    if name in NOTE_MARKER_FILENAMES and _looks_like_paper_folder(absolute):
        return "note"
    return None


class VaultIndex:
    def __init__(self, root: Path, max_file_mb: float = DEFAULT_MAX_FILE_MB):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise VaultError("vault root is not a directory: " + str(self.root))
        self.max_bytes = int(max_file_mb * 1024 * 1024)
        self.notes: List[Record] = []
        self.fulltexts: List[Record] = []
        self.by_rel: Dict[str, Record] = {}
        self.fulltexts_by_key: Dict[str, List[Record]] = defaultdict(list)
        self.notes_by_key: Dict[str, List[Record]] = defaultdict(list)
        self.warnings: List[str] = []
        self._scan()

    # -- scanning ---------------------------------------------------------
    def _scan(self) -> None:
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(
                name for name in dirnames
                if name not in EXCLUDED_DIR_NAMES and not name.startswith(".")
            )
            for filename in sorted(filenames):
                if not filename.lower().endswith(".md"):
                    continue
                absolute = Path(dirpath) / filename
                if absolute.is_symlink():
                    continue
                try:
                    rel_path = absolute.relative_to(self.root).as_posix()
                except ValueError:
                    continue
                try:
                    text = absolute.read_text(encoding="utf-8-sig", errors="replace")[:65536]
                except OSError:
                    continue
                fields, _, _ = parse_frontmatter(text)
                kind = _classify(absolute, Path(rel_path).parts, fields)
                if kind is None:
                    continue
                record = _record_from_file(absolute, rel_path, kind, self.max_bytes)
                if record is None:
                    self.warnings.append("SKIPPED_OVERSIZED_OR_UNREADABLE: " + rel_path)
                    continue
                self.by_rel[rel_path] = record
                if kind == "note":
                    self.notes.append(record)
                    if record.zotero_key:
                        self.notes_by_key[record.zotero_key.upper()].append(record)
                else:
                    self.fulltexts.append(record)
                    if record.zotero_key:
                        self.fulltexts_by_key[record.zotero_key.upper()].append(record)

    # -- resolution -------------------------------------------------------
    def _vault_relative(self, value: str) -> Optional[str]:
        raw = value.strip().replace("\\", "/").lstrip("/")
        # Strip an optional leading "./".
        while raw.startswith("./"):
            raw = raw[2:]
        if not raw:
            return None
        candidate = (self.root / raw).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError:
            return None
        return candidate.relative_to(self.root).as_posix()

    def resolve_fulltext(self, note: Record) -> Tuple[Optional[Record], Optional[str], List[str]]:
        warnings: List[str] = []
        if note.fulltext_path:
            rel = self._vault_relative(note.fulltext_path)
            if rel is None:
                warnings.append("FULLTEXT_PATH_ESCAPES_VAULT: " + note.fulltext_path)
            else:
                candidates = [rel]
                if not rel.lower().endswith(".md"):
                    candidates.append(rel + ".md")
                for candidate in candidates:
                    record = self.by_rel.get(candidate)
                    if record is not None and record.kind == "fulltext":
                        return record, "fulltext_path", warnings
                warnings.append("FULLTEXT_PATH_MISSING: " + note.fulltext_path)
        if note.zotero_key:
            matches = self.fulltexts_by_key.get(note.zotero_key.upper(), [])
            if len(matches) == 1:
                return matches[0], "zotero_key", warnings
            if len(matches) > 1:
                warnings.append(
                    "FULLTEXT_AMBIGUOUS: %d fulltexts share zotero_key %s"
                    % (len(matches), note.zotero_key)
                )
                return None, None, warnings
        normalized = normalize_text(note.title)
        title_matches = [item for item in self.fulltexts if normalize_text(item.title) == normalized]
        if len(title_matches) == 1:
            warnings.append("TITLE_FALLBACK: resolved fulltext by unique title; prefer fulltext_path")
            return title_matches[0], "title", warnings
        if len(title_matches) > 1:
            warnings.append("FULLTEXT_AMBIGUOUS: %d fulltexts match title" % len(title_matches))
        return None, None, warnings

    def resolve_pdf(self, note: Record) -> Tuple[Optional[Path], Optional[str], List[str]]:
        warnings: List[str] = []
        if note.pdf_path:
            rel = self._vault_relative(note.pdf_path)
            candidates = []
            if rel is not None:
                candidates.append(self.root / rel)
            absolute = Path(note.pdf_path).expanduser()
            if absolute.is_absolute():
                candidates.append(absolute)
            for candidate in candidates:
                if candidate.is_file() and candidate.suffix.lower() == ".pdf":
                    return candidate.resolve(), "frontmatter", warnings
            warnings.append("PDF_PATH_MISSING: " + note.pdf_path)
        # Agentero-style paper folder: NOTES.md next to the PDF attachment(s).
        candidates: List[Path] = []
        for folder in (note.abs_path.parent, note.abs_path.parent / "attachments"):
            if folder.is_dir():
                candidates.extend(sorted(
                    item for item in folder.glob("*.pdf")
                    if item.is_file() and not item.is_symlink()
                ))
        if len(candidates) == 1:
            return candidates[0].resolve(), "sibling", warnings
        if len(candidates) > 1:
            warnings.append("PDF_AMBIGUOUS: %d PDFs in paper folder; pass --pdf explicitly" % len(candidates))
        return None, None, warnings

    def find_paper(self, ref: str) -> Tuple[Optional[Record], str, List[Record]]:
        """Resolve a paper reference to one note. Returns (note, match_kind, candidates)."""
        ref_text = ref.strip()
        if not ref_text:
            return None, "", []
        rel = self._vault_relative(ref_text)
        if rel and rel in self.by_rel:
            record = self.by_rel[rel]
            if record.kind == "note":
                return record, "path", []
        matches: List[Tuple[Record, str]] = []
        key = ref_text.upper()
        for note in self.notes_by_key.get(key, []):
            matches.append((note, "zotero_key"))
        lowered = ref_text.casefold()
        if not matches:
            for note in self.notes:
                if note.doi and note.doi.casefold() == lowered:
                    matches.append((note, "doi"))
        if not matches:
            target = normalize_text(ref_text)
            for note in self.notes:
                names = {normalize_text(note.title)} | {normalize_text(alias) for alias in note.aliases}
                if target in names:
                    matches.append((note, "title"))
        deduped: List[Tuple[Record, str]] = []
        seen = set()
        for note, kind in matches:
            if note.rel_path in seen:
                continue
            seen.add(note.rel_path)
            deduped.append((note, kind))
        if len(deduped) == 1:
            return deduped[0][0], deduped[0][1], []
        return None, "", [note for note, _ in deduped]

    def paper_chain(self, note: Record) -> Dict[str, Any]:
        warnings: List[str] = []
        fulltext, via, fulltext_warnings = self.resolve_fulltext(note)
        warnings.extend(fulltext_warnings)
        pdf, pdf_via, pdf_warnings = self.resolve_pdf(note)
        warnings.extend(pdf_warnings)
        fulltext_info: Optional[Dict[str, Any]] = None
        if fulltext is not None:
            key_match = None
            reciprocal_ok = None
            if note.zotero_key and fulltext.zotero_key:
                key_match = note.zotero_key.upper() == fulltext.zotero_key.upper()
                if not key_match:
                    warnings.append(
                        "KEY_MISMATCH: note zotero_key=%s fulltext zotero_key=%s"
                        % (note.zotero_key, fulltext.zotero_key)
                    )
            if fulltext.note_path:
                back = self._vault_relative(fulltext.note_path)
                reciprocal_ok = back == note.rel_path
                if not reciprocal_ok:
                    warnings.append(
                        "NOTE_PATH_MISMATCH: fulltext note_path=%s" % fulltext.note_path
                    )
            fulltext_info = {
                "present": True,
                "rel_path": fulltext.rel_path,
                "via": via,
                "key_match": key_match,
                "reciprocal_ok": reciprocal_ok,
            }
        else:
            fulltext_info = {"present": False, "rel_path": None, "via": None,
                             "key_match": None, "reciprocal_ok": None}
        evidence_levels = ["L1_note_only"]
        if fulltext is not None:
            evidence_levels.append("L2_fulltext_verified")
        if pdf is not None and shutil.which("pdftotext"):
            evidence_levels.append("L3_pdf_verified")
        return {
            "note": _record_summary(note),
            "fulltext": fulltext_info,
            "pdf": {"present": pdf is not None, "path": str(pdf) if pdf else None, "source": pdf_via},
            "evidence_levels_available": evidence_levels,
            "warnings": warnings,
        }


def _record_summary(record: Record) -> Dict[str, Any]:
    return {
        "rel_path": record.rel_path,
        "title": record.title,
        "identity": record.identity,
        "zotero_key": record.zotero_key,
        "doi": record.doi,
        "aliases": record.aliases[:8],
        "tags": record.tags[:12],
    }


# --------------------------------------------------------------------------
# Search (NOTE-FIRST)
# --------------------------------------------------------------------------

def normalize_text(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", folded).strip().casefold()


def query_terms(query: str) -> List[Tuple[str, float]]:
    """Split a query into weighted terms; CJK runs stay whole plus bigrams."""
    normalized = normalize_text(query)
    segments = [segment for segment in QUERY_SPLIT_RE.split(normalized) if segment]
    weights: Dict[str, float] = {}
    for segment in segments:
        if not CJK_RE.search(segment):
            if len(segment) >= 2:
                weights[segment] = max(weights.get(segment, 0.0), 3.0)
            continue
        for match in re.finditer(
                r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+|[a-z0-9][a-z0-9._+-]*",
                segment):
            chunk = match.group(0)
            if CJK_RE.search(chunk):
                weights[chunk] = max(weights.get(chunk, 0.0), 3.0)
                if len(chunk) >= 3:
                    for index in range(len(chunk) - 1):
                        weights.setdefault(chunk[index:index + 2], 1.0)
            elif len(chunk) >= 2:
                weights[chunk] = max(weights.get(chunk, 0.0), 3.0)
    return sorted(weights.items(), key=lambda item: (-item[1], item[0]))


def _count_occurrences(haystack: str, needle: str, cap: int = 5) -> int:
    if not needle or not haystack:
        return 0
    count = 0
    start = 0
    while count < cap:
        index = haystack.find(needle, start)
        if index == -1:
            break
        count += 1
        start = index + len(needle)
    return count


def _snippet(text: str, terms: Iterable[str], width: int = 160) -> str:
    lowered = text.casefold()
    for term in terms:
        index = lowered.find(term)
        if index != -1:
            start = max(0, index - width // 2)
            piece = text[start:start + width].replace("\n", " ").strip()
            return ("…" if start > 0 else "") + piece + ("…" if start + width < len(text) else "")
    return re.sub(r"\s+", " ", text[:width]).strip()


def search_notes(index: VaultIndex, query: str, limit: int = 8,
                 offset: int = 0) -> List[Dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []
    results: List[Dict[str, Any]] = []
    for note in index.notes:
        fields: List[Tuple[str, str, float]] = []
        for name, weight in NOTE_FIELDS:
            if name == "body":
                fields.append((name, note.body, weight))
            elif name == "aliases":
                fields.append((name, " ".join(note.aliases), weight))
            elif name == "tags":
                fields.append((name, " ".join(note.tags), weight))
            else:
                fields.append((name, field_text(note.fields, name), weight))
        score = 0.0
        matched_terms: List[str] = []
        matched_fields: List[str] = []
        for term, term_weight in terms:
            term_hit = False
            for name, text, field_weight in fields:
                lowered = normalize_text(text)
                count = _count_occurrences(lowered, term)
                if count:
                    term_hit = True
                    score += count * field_weight * term_weight
                    if name not in matched_fields:
                        matched_fields.append(name)
            if term_hit:
                matched_terms.append(term)
        if score <= 0:
            continue
        results.append({
            "record": note,
            "score": round(score, 2),
            "matched_terms": matched_terms[:12],
            "matched_fields": matched_fields,
            "snippet": _snippet(note.body or field_text(note.fields, "key_finding"),
                                matched_terms or [query]),
        })
    results.sort(key=lambda item: (-item["score"], item["record"].rel_path))
    return results[offset:offset + limit]


def search_fulltext_supplementary(index: VaultIndex, query: str,
                                  limit: int = 5) -> List[Dict[str, Any]]:
    terms = query_terms(query)
    if not terms:
        return []
    results: List[Dict[str, Any]] = []
    for record in index.fulltexts:
        haystack = normalize_text(record.body)
        score = 0.0
        hits: List[str] = []
        for term, term_weight in terms:
            count = _count_occurrences(haystack, term)
            if count:
                score += count * term_weight
                hits.append(term)
        if score <= 0:
            continue
        note = None
        if record.zotero_key:
            candidates = index.notes_by_key.get(record.zotero_key.upper(), [])
            if len(candidates) == 1:
                note = candidates[0]
        if note is None and record.note_path:
            note = index.by_rel.get(index._vault_relative(record.note_path) or "")
        resolved = note is not None and note.kind == "note"
        results.append({
            "rel_path": record.rel_path,
            "title": record.title,
            "zotero_key": record.zotero_key,
            "score": round(score, 2),
            "matched_terms": hits[:12],
            "snippet": _snippet(record.body, hits or [query]),
            "resolved_note": note.rel_path if resolved else None,
            "note_resolution": "resolved" if resolved else "unresolved-do-not-count-as-paper",
        })
    results.sort(key=lambda item: (-item["score"], item["rel_path"]))
    return results[:limit]


# --------------------------------------------------------------------------
# Quote location verification
# --------------------------------------------------------------------------

def _normalize_ws(text: str, casefold: bool) -> str:
    value = unicodedata.normalize("NFKC", text or "").replace("\u00a0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value.casefold() if casefold else value


class TextLookup:
    """Whitespace-normalized view of a text with a map back to source lines."""

    def __init__(self, text: str, casefold: bool):
        segments: List[Tuple[str, int]] = []
        for line_number, raw in enumerate(text.splitlines(), start=1):
            normalized = _normalize_ws(raw, casefold)
            if normalized:
                segments.append((normalized, line_number))
        pieces: List[str] = []
        self.starts: List[int] = []
        self.line_numbers: List[int] = []
        position = 0
        for normalized, line_number in segments:
            self.starts.append(position)
            self.line_numbers.append(line_number)
            pieces.append(normalized)
            position += len(normalized) + 1
        self.doc = " ".join(pieces)

    def locate(self, quote: str) -> Optional[Tuple[int, int]]:
        index = self.doc.find(quote)
        if index == -1:
            return None
        start_segment = bisect.bisect_right(self.starts, index) - 1
        end_segment = bisect.bisect_right(self.starts, index + len(quote) - 1) - 1
        if start_segment < 0:
            return None
        return self.line_numbers[start_segment], self.line_numbers[max(end_segment, 0)]


def find_quote_in_text(text: str, quote: str) -> Optional[Dict[str, Any]]:
    normalized_quote = _normalize_ws(quote, False)
    if not normalized_quote:
        return None
    for casefold in (False, True):
        lookup = TextLookup(text, casefold)
        needle = normalized_quote.casefold() if casefold else normalized_quote
        location = lookup.locate(needle)
        if location is not None:
            return {
                "match_kind": "case_insensitive" if casefold else "exact",
                "line_start": location[0],
                "line_end": location[1],
            }
    return None


def _compile_page_markers(extra: Optional[str]) -> List["re.Pattern[str]"]:
    patterns = list(SCALAR_PAGE_MARKERS)
    if extra:
        try:
            patterns.insert(0, re.compile(extra))
        except re.error as error:
            raise VaultError("invalid --page-marker-regex: %s" % error)
    return patterns


def line_page_map(text: str, patterns: Sequence["re.Pattern[str]"]) -> Dict[int, int]:
    pages: Dict[int, int] = {}
    current: Optional[int] = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        for pattern in patterns:
            match = pattern.match(line)
            if match:
                try:
                    current = int(match.group(1))
                except (IndexError, ValueError):
                    continue
                break
        if current is not None:
            pages[line_number] = current
    return pages


def verify_markdown(path: Path, quote: str, page: Optional[int],
                    extra_marker: Optional[str]) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as error:
        return {"status": "unavailable", "reason": "source unreadable: %s" % error.__class__.__name__}
    hit = find_quote_in_text(text, quote)
    if hit is None:
        return {"status": "not_found", "reason": "quote not found in source text"}
    pages = line_page_map(text, _compile_page_markers(extra_marker))
    detected_page = pages.get(hit["line_start"])
    warnings: List[str] = []
    if page is not None:
        if detected_page is None:
            warnings.append("PAGE_UNKNOWN: Markdown has no page marker; pass a PDF to verify the page")
        elif detected_page != page:
            return {
                "status": "not_found",
                "reason": "quote found on page %d, not page %d" % (detected_page, page),
                "match_kind": hit["match_kind"],
                "line_start": hit["line_start"],
                "line_end": hit["line_end"],
                "page": detected_page,
            }
    if len(_normalize_ws(quote, False)) < 12:
        warnings.append("QUOTE_TOO_SHORT: quotes under 12 characters are weak evidence")
    return {
        "status": "verified",
        "match_kind": hit["match_kind"],
        "line_start": hit["line_start"],
        "line_end": hit["line_end"],
        "page": detected_page,
        "page_basis": "markdown-marker" if detected_page is not None else "unknown",
        "warnings": warnings,
    }


def _pdf_pages(pdf_path: Path) -> Tuple[Optional[List[str]], Optional[str]]:
    executable = shutil.which("pdftotext")
    if not executable:
        return None, "pdftotext not installed; PDF verification unavailable"
    try:
        result = subprocess.run(
            [executable, "-enc", "UTF-8", str(pdf_path), "-"],
            capture_output=True, timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return None, "pdftotext failed: %s" % error.__class__.__name__
    if result.returncode != 0:
        return None, "pdftotext rejected the PDF (encrypted or corrupt)"
    return result.stdout.decode("utf-8", "replace").split("\f"), None


def verify_pdf(path: Path, quote: str, page: Optional[int]) -> Dict[str, Any]:
    pages, reason = _pdf_pages(path)
    if pages is None:
        return {"status": "unavailable", "reason": reason}
    indices = [page - 1] if page is not None else list(range(len(pages)))
    scanned = 0
    for index in indices:
        if not (0 <= index < len(pages)):
            continue
        scanned += 1
        hit = find_quote_in_text(pages[index], quote)
        if hit is not None:
            warnings: List[str] = []
            if len(_normalize_ws(quote, False)) < 12:
                warnings.append("QUOTE_TOO_SHORT: quotes under 12 characters are weak evidence")
            return {
                "status": "verified",
                "match_kind": hit["match_kind"],
                "page": index + 1,
                "page_basis": "pdf",
                "page_line_start": hit["line_start"],
                "page_line_end": hit["line_end"],
                "warnings": warnings,
            }
    return {
        "status": "not_found",
        "reason": "quote not found on page %s" % (page if page is not None else "any page"),
        "pages_scanned": scanned,
    }


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_find(args: argparse.Namespace, index: VaultIndex) -> Tuple[Dict[str, Any], int]:
    candidates = search_notes(index, args.query, args.limit, args.offset)
    output: Dict[str, Any] = {
        "skill": SKILL_NAME,
        "version": TOOL_VERSION,
        "vault": str(index.root),
        "scope": "notes",
        "query": args.query,
        "terms": [term for term, _ in query_terms(args.query)],
        "candidates": [],
        "coverage": {
            "notes_scanned": len(index.notes),
            "candidate_count": len(candidates),
            "truncated": len(candidates) == args.limit,
        },
        "supplementary_fulltext_hits": [],
        "warnings": list(index.warnings),
        "note_first_rule": "fulltext is not scanned unless --include-fulltext is passed",
    }
    for item in candidates:
        note: Record = item["record"]
        fulltext, via, warnings = index.resolve_fulltext(note)
        pdf, _, pdf_warnings = index.resolve_pdf(note)
        output["candidates"].append({
            **_record_summary(note),
            "score": item["score"],
            "matched_terms": item["matched_terms"],
            "matched_fields": item["matched_fields"],
            "snippet": item["snippet"],
            "fulltext": {
                "present": fulltext is not None,
                "rel_path": fulltext.rel_path if fulltext else None,
                "via": via,
            },
            "pdf_present": pdf is not None,
            "warnings": warnings + pdf_warnings,
        })
    output["coverage"]["with_fulltext"] = sum(
        1 for item in output["candidates"] if item["fulltext"]["present"]
    )
    output["coverage"]["note_only"] = (
        output["coverage"]["candidate_count"] - output["coverage"]["with_fulltext"]
    )
    if args.include_fulltext:
        output["scope"] = "notes+supplementary-fulltext"
        output["supplementary_fulltext_hits"] = search_fulltext_supplementary(
            index, args.query, max(1, args.limit)
        )
        output["note_first_rule"] = (
            "supplementary fulltext recall was explicitly requested; "
            "resolve every hit through its note before counting it as a paper"
        )
    return output, 0


def cmd_resolve(args: argparse.Namespace, index: VaultIndex) -> Tuple[Dict[str, Any], int]:
    note, match_kind, candidates = index.find_paper(args.paper)
    base: Dict[str, Any] = {
        "skill": SKILL_NAME,
        "vault": str(index.root),
        "ref": args.paper,
    }
    if note is None:
        if candidates:
            base.update({
                "status": "ambiguous",
                "match_kind": None,
                "candidates": [_record_summary(item) for item in candidates],
                "next": "use one of the candidate paths or zotero_key values; do not guess",
            })
            return base, 1
        base.update({
            "status": "not_found",
            "candidates": [],
            "next": "report the gap; do not reconstruct the paper from model memory",
        })
        return base, 1
    chain = index.paper_chain(note)
    base.update({"status": "resolved", "match_kind": match_kind, **chain})
    return base, 0


def _resolve_verify_target(args: argparse.Namespace, index: VaultIndex,
                           use_note: bool) -> Dict[str, Any]:
    warnings: List[str] = []
    if use_note:
        note, match_kind, candidates = index.find_paper(args.note)
        if note is None:
            return {"status": "unavailable", "reason": "note not resolved",
                    "candidates": [item.rel_path for item in candidates]}
        fulltext, via, fulltext_warnings = index.resolve_fulltext(note)
        warnings.extend(fulltext_warnings)
        pdf, _, pdf_warnings = index.resolve_pdf(note)
        warnings.extend(pdf_warnings)
        if args.page is not None:
            if pdf is not None:
                return {"kind": "pdf", "path": pdf, "warnings": warnings, "note": note.rel_path}
            return {"status": "unavailable",
                    "reason": "page verification requires a PDF; none resolved for " + note.rel_path,
                    "warnings": warnings, "note": note.rel_path}
        if fulltext is not None:
            return {"kind": "markdown", "path": fulltext.abs_path,
                    "warnings": warnings, "note": note.rel_path,
                    "source_rel_path": fulltext.rel_path}
        if pdf is not None:
            # PDF-only layout (e.g. Agentero paper folders): verify against the
            # original PDF text directly; the returned page is the PDF page.
            warnings.append("PDF_ONLY: no fulltext layer; verifying against the original PDF")
            return {"kind": "pdf", "path": pdf, "warnings": warnings, "note": note.rel_path}
        return {"status": "unavailable",
                "reason": "note has no resolvable fulltext or PDF; verify against --source explicitly",
                "warnings": warnings, "note": note.rel_path}
    raw = (args.source or "").strip()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        relative = index._vault_relative(raw)
        path = (index.root / relative) if relative else (index.root / raw)
    else:
        try:
            path.resolve().relative_to(index.root)
        except ValueError:
            if not getattr(args, "allow_outside_vault", False):
                return {"status": "unavailable",
                        "reason": "source is outside the Vault; pass --allow-outside-vault"}
    if not path.is_file():
        return {"status": "unavailable", "reason": "source not found: " + raw}
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return {"kind": "pdf", "path": path.resolve(), "warnings": warnings}
    if suffix in (".md", ".markdown", ".txt"):
        try:
            rel_path = path.resolve().relative_to(index.root).as_posix()
        except ValueError:
            rel_path = str(path.resolve())
        return {"kind": "markdown", "path": path.resolve(), "warnings": warnings,
                "source_rel_path": rel_path}
    return {"status": "unavailable", "reason": "unsupported source type: " + suffix}


def _verify_item(item: Dict[str, Any], index: VaultIndex,
                 extra_marker: Optional[str], allow_outside: bool) -> Dict[str, Any]:
    quote = str(item.get("quote") or "").strip()
    record: Dict[str, Any] = {
        "id": item.get("id"),
        "claim": item.get("claim"),
        "quote": quote,
        "source": item.get("source") or item.get("note"),
    }
    if not quote:
        record.update({"status": "unavailable", "reason": "empty quote"})
        return record
    page = item.get("page")
    if page is not None:
        try:
            page = int(page)
        except (TypeError, ValueError):
            record.update({"status": "unavailable", "reason": "page must be an integer"})
            return record
    target: Dict[str, Any]
    if item.get("note") and not item.get("source"):
        args = argparse.Namespace(note=str(item["note"]), source=None, page=page,
                                  allow_outside_vault=allow_outside)
        target = _resolve_verify_target(args, index, use_note=True)
    else:
        args = argparse.Namespace(note=None, source=str(item.get("source") or ""), page=page,
                                  allow_outside_vault=allow_outside)
        target = _resolve_verify_target(args, index, use_note=False)
    if target.get("status") == "unavailable":
        record.update(target)
        return record
    if target["kind"] == "pdf":
        result = verify_pdf(Path(target["path"]), quote, page)
    else:
        pdf_path = item.get("pdf")
        if page is not None and pdf_path:
            # Page claims must be verified against the original PDF.
            result = verify_pdf(Path(str(pdf_path)).expanduser(), quote, page)
            target["pdf_used"] = True
        else:
            result = verify_markdown(Path(target["path"]), quote, page, extra_marker)
    record.update(result)
    record.setdefault("source", target.get("source_rel_path") or str(target["path"]))
    if target.get("source_rel_path"):
        record["source"] = target["source_rel_path"]
    if target.get("note"):
        record["note"] = target["note"]
    warnings = list(target.get("warnings") or []) + list(result.get("warnings") or [])
    record["warnings"] = warnings
    return record


def _verify_exit_code(items: Sequence[Dict[str, Any]]) -> int:
    statuses = [item.get("status") for item in items]
    if any(status == "not_found" for status in statuses):
        return 1
    if any(status in ("ambiguous",) for status in statuses):
        return 1
    if any(status in ("unavailable",) for status in statuses):
        return 2
    return 0


def cmd_verify(args: argparse.Namespace, index: VaultIndex) -> Tuple[Dict[str, Any], int]:
    if args.evidence:
        evidence_path = Path(args.evidence).expanduser()
        try:
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise VaultError("cannot read --evidence JSON: %s" % error.__class__.__name__)
        raw_items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(raw_items, list) or not raw_items:
            raise VaultError("--evidence must contain a non-empty list under 'items'")
    else:
        if not args.quote:
            raise VaultError("verify requires --quote (with --note/--source) or --evidence")
        if not args.note and not args.source:
            raise VaultError("verify requires --note or --source when --quote is given")
        raw_items = [{
            "id": "E1",
            "claim": None,
            "quote": args.quote,
            "page": args.page,
            "note": args.note,
            "source": args.source,
            "pdf": args.pdf,
        }]
    items = [_verify_item(item if isinstance(item, dict) else {},
                          index, args.page_marker_regex, args.allow_outside_vault)
             for item in raw_items]
    summary = {
        "total": len(items),
        "verified": sum(1 for item in items if item.get("status") == "verified"),
        "not_found": sum(1 for item in items if item.get("status") == "not_found"),
        "unavailable": sum(1 for item in items if item.get("status") == "unavailable"),
    }
    output = {
        "skill": SKILL_NAME,
        "vault": str(index.root),
        "items": items,
        "summary": summary,
        "verification_semantics": (
            "location check proves the quote exists at the given source; "
            "it does not prove the study's conclusion is correct"
        ),
        "read_only": True,
    }
    return output, _verify_exit_code(items)


def cmd_stats(args: argparse.Namespace, index: VaultIndex) -> Tuple[Dict[str, Any], int]:
    dirs_present = sorted(
        name for name in ("01knowledge", "02vault", "03fulltext", "papers", "notes", "fulltext")
        if (index.root / name).is_dir()
    )
    layout = "unknown"
    if {"02vault", "03fulltext"} & set(dirs_present):
        layout = "research-vault (note + fulltext layers)"
    elif "papers" in dirs_present:
        layout = "agentero-papers (paper folders with NOTES.md + PDF)"
    elif dirs_present:
        layout = "generic (" + ", ".join(dirs_present) + ")"
    pdf_only_layout = layout.startswith("agentero")
    issues: List[Dict[str, Any]] = []
    with_fulltext = 0
    note_only = 0
    pdf_resolvable = 0
    for note in index.notes:
        fulltext, _via, warnings = index.resolve_fulltext(note)
        if fulltext is None:
            note_only += 1
            if not pdf_only_layout:
                issues.append({"kind": "FULLTEXT_MISSING", "note": note.rel_path,
                               "detail": "; ".join(warnings) or "no fulltext_path and no key match"})
        else:
            with_fulltext += 1
            chain = index.paper_chain(note)
            for warning in chain.get("warnings") or []:
                if warning.startswith(("KEY_MISMATCH", "NOTE_PATH_MISMATCH", "FULLTEXT_AMBIGUOUS")):
                    issues.append({"kind": warning.split(":", 1)[0], "note": note.rel_path,
                                   "detail": warning})
        pdf, _, pdf_warnings = index.resolve_pdf(note)
        if pdf is not None:
            pdf_resolvable += 1
        elif note.pdf_path:
            issues.append({"kind": "PDF_PATH_MISSING", "note": note.rel_path,
                           "detail": "; ".join(pdf_warnings) or "pdf_path does not resolve"})
        elif note.abs_path.name.lower() == "notes.md" or note.rel_path.startswith("papers/"):
            # A paper folder is expected to hold its original PDF; a ZAWS-style
            # note keeps the PDF in Zotero, so absence is not an issue there.
            issues.append({"kind": "PDF_MISSING", "note": note.rel_path,
                           "detail": "paper folder has no single resolvable PDF"})
    covered_keys = {note.zotero_key.upper() for note in index.notes if note.zotero_key}
    for key, notes in sorted(index.notes_by_key.items()):
        if len(notes) > 1:
            issues.append({
                "kind": "DUPLICATE_NOTE_KEY",
                "detail": "zotero_key=%s shared by: %s"
                          % (key, ", ".join(item.rel_path for item in notes)),
            })
    orphan_fulltexts = [
        record.rel_path for record in index.fulltexts
        if not record.zotero_key or record.zotero_key.upper() not in covered_keys
    ]
    for rel_path in orphan_fulltexts:
        issues.append({"kind": "FULLTEXT_WITHOUT_NOTE", "fulltext": rel_path,
                       "detail": "no analytical note references this fulltext key"})
    issues.sort(key=lambda item: (str(item.get("kind")),
                                  str(item.get("note") or item.get("fulltext") or "")))
    env = {
        "python": sys.version.split()[0],
        "pdftotext": shutil.which("pdftotext"),
        "ripgrep": shutil.which("rg"),
    }
    output = {
        "skill": SKILL_NAME,
        "version": TOOL_VERSION,
        "vault": str(index.root),
        "layout_guess": layout,
        "layout_dirs_present": dirs_present,
        "counts": {
            "notes": len(index.notes),
            "fulltexts": len(index.fulltexts),
            "pdf_resolvable": pdf_resolvable,
        },
        "coverage": {
            "notes_with_fulltext": with_fulltext,
            "notes_note_only": note_only,
            "notes_pdf_verifiable": pdf_resolvable,
        },
        "issues": issues[:200],
        "issue_counts": _issue_counts(issues),
        "scan_warnings": index.warnings,
        "environment": env,
        "read_only": True,
    }
    return output, 0


def _issue_counts(issues: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for issue in issues:
        counts[str(issue.get("kind"))] += 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------
# Human-readable rendering
# --------------------------------------------------------------------------

def render_find(output: Dict[str, Any]) -> str:
    lines = [
        "VAULT: " + output["vault"],
        "SCOPE: " + output["scope"] + " (NOTE-FIRST)",
        "QUERY: " + output["query"],
    ]
    if not output["candidates"]:
        lines.append("NO_MATCH: no analytical note matched. Report the gap; do not answer from memory.")
    for position, item in enumerate(output["candidates"], start=1):
        fulltext = item["fulltext"]
        availability = (
            "fulltext=" + (fulltext["rel_path"] or "missing") if fulltext["present"] else "fulltext=missing"
        )
        lines.append("")
        lines.append("[%d] %s  (score %s)" % (position, item["title"], item["score"]))
        lines.append("    note: " + item["rel_path"])
        identity = "zotero_key=" + (item["zotero_key"] or "-")
        if item["doi"]:
            identity += "  doi=" + item["doi"]
        lines.append("    identity: " + identity)
        lines.append("    evidence: %s  pdf=%s" % (availability, "yes" if item["pdf_present"] else "no"))
        lines.append("    match: " + ", ".join(item["matched_fields"]))
        if item["snippet"]:
            lines.append("    > " + item["snippet"][:220])
        for warning in item.get("warnings") or []:
            lines.append("    ! " + warning)
    if output["supplementary_fulltext_hits"]:
        lines.append("")
        lines.append("SUPPLEMENTARY FULLTEXT RECALL (explicit; not counted as papers):")
        for hit in output["supplementary_fulltext_hits"]:
            lines.append("    - %s  note=%s  (%s)" % (
                hit["rel_path"], hit["resolved_note"] or "unresolved", hit["score"]))
    coverage = output["coverage"]
    lines.append("")
    lines.append("COVERAGE: %d notes scanned, %d candidates, %d with fulltext, %d note-only%s" % (
        coverage["notes_scanned"], coverage["candidate_count"],
        coverage["with_fulltext"], coverage["note_only"],
        " (truncated at limit)" if coverage["truncated"] else "",
    ))
    lines.append("READ-ONLY: no file was written.")
    return "\n".join(lines)


def render_resolve(output: Dict[str, Any]) -> str:
    if output["status"] != "resolved":
        lines = [
            "REF: " + output["ref"],
            "STATUS: " + output["status"].upper(),
        ]
        for candidate in output.get("candidates") or []:
            lines.append("    candidate: %s  key=%s" % (
                candidate["rel_path"], candidate["zotero_key"] or "-"))
        lines.append("NEXT: " + output.get("next", ""))
        return "\n".join(lines)
    note = output["note"]
    lines = [
        "REF: " + output["ref"],
        "STATUS: resolved (matched by %s)" % output["match_kind"],
        "PAPER: " + note["title"],
        "  note: " + note["rel_path"],
        "  identity: " + note["identity"],
    ]
    fulltext = output["fulltext"]
    if fulltext["present"]:
        lines.append("  fulltext: %s  via=%s  key_match=%s  reciprocal=%s" % (
            fulltext["rel_path"], fulltext["via"],
            fulltext["key_match"], fulltext["reciprocal_ok"]))
    else:
        lines.append("  fulltext: MISSING (note-only evidence)")
    pdf = output["pdf"]
    lines.append("  pdf: %s" % (pdf["path"] if pdf["present"] else "not resolved"))
    lines.append("  available evidence levels: " + ", ".join(output["evidence_levels_available"]))
    for warning in output.get("warnings") or []:
        lines.append("  ! " + warning)
    return "\n".join(lines)


def render_verify(output: Dict[str, Any]) -> str:
    summary = output["summary"]
    lines = ["VERIFY: %d item(s) — %d verified, %d not_found, %d unavailable" % (
        summary["total"], summary["verified"], summary["not_found"], summary["unavailable"]),
        ""]
    for item in output["items"]:
        identifier = item.get("id") or "?"
        status = str(item.get("status") or "unavailable").upper()
        where = item.get("source") or item.get("note") or "-"
        location = ""
        if item.get("status") == "verified":
            if item.get("line_start") is not None:
                location = " L%s-L%s" % (item["line_start"], item["line_end"])
            if item.get("page") is not None:
                location += " · page %s (%s)" % (item["page"], item.get("page_basis") or "?")
        lines.append("[%s] %s · %s%s" % (identifier, status, where, location))
        if item.get("match_kind"):
            lines.append("    match: " + item["match_kind"])
        if item.get("reason"):
            lines.append("    reason: " + str(item["reason"]))
        for warning in item.get("warnings") or []:
            lines.append("    ! " + warning)
    lines.append("")
    lines.append("NOTE: location verified, semantics still require independent review.")
    return "\n".join(lines)


def render_stats(output: Dict[str, Any]) -> str:
    counts = output["counts"]
    coverage = output["coverage"]
    pdf_only = str(output["layout_guess"]).startswith("agentero")
    coverage_line = "COVERAGE: %d with fulltext, %d note-only" % (
        coverage["notes_with_fulltext"], coverage["notes_note_only"])
    if pdf_only:
        coverage_line += " (%d PDF-verifiable; no fulltext layer by design)" % (
            coverage["notes_pdf_verifiable"])
    lines = [
        "VAULT: " + output["vault"],
        "LAYOUT: " + output["layout_guess"],
        "COUNTS: %d notes, %d fulltexts, %d pdf-resolvable" % (
            counts["notes"], counts["fulltexts"], counts["pdf_resolvable"]),
        coverage_line,
        "ISSUES: " + (", ".join("%s=%d" % item for item in output["issue_counts"].items())
                      or "none"),
        "ENV: python %s, pdftotext=%s" % (
            output["environment"]["python"], output["environment"]["pdftotext"] or "missing"),
        "READ-ONLY: no file was written.",
    ]
    for issue in output["issues"][:20]:
        target = issue.get("note") or issue.get("fulltext") or "-"
        lines.append("  - %s | %s | %s" % (issue.get("kind"), target, issue.get("detail", "")))
    if len(output["issues"]) > 20:
        lines.append("  ... %d more (use --json)" % (len(output["issues"]) - 20))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rv_retrieval.py",
        description="Read-only NOTE-FIRST literature retrieval for Markdown research vaults.",
    )
    parser.add_argument("--vault", default=os.environ.get("RESEARCH_VAULT_ROOT"),
                        help="Vault root (default: $RESEARCH_VAULT_ROOT)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--max-file-mb", type=float, default=DEFAULT_MAX_FILE_MB,
                        help="skip Markdown files larger than this (default: 8)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    find = subparsers.add_parser("find", help="search analytical notes first")
    find.add_argument("--query", required=True)
    find.add_argument("--limit", type=int, default=8)
    find.add_argument("--offset", type=int, default=0)
    find.add_argument("--include-fulltext", action="store_true",
                      help="explicit supplementary fulltext recall (still resolve hits via notes)")

    resolve = subparsers.add_parser("resolve", help="resolve one paper to note/fulltext/PDF")
    resolve.add_argument("--paper", required=True,
                         help="note path, zotero_key, DOI, or exact title/alias")

    verify = subparsers.add_parser("verify", help="verify quote location in fulltext or PDF")
    verify.add_argument("--note", help="paper reference; verify against its fulltext/PDF chain")
    verify.add_argument("--source", help="Markdown/PDF path to verify against directly")
    verify.add_argument("--quote")
    verify.add_argument("--page", type=int)
    verify.add_argument("--pdf", help="PDF path used when a Markdown source has no page markers")
    verify.add_argument("--evidence", help="JSON file: {\"items\":[{id,claim,quote,source|note,page}]}")
    verify.add_argument("--page-marker-regex",
                        help="extra Markdown page marker regex with one capture group")
    verify.add_argument("--allow-outside-vault", action="store_true",
                        help="allow --source paths outside the Vault (read-only)")

    subparsers.add_parser("stats", help="read-only vault diagnostics")
    return parser


RENDERERS = {
    "find": render_find,
    "resolve": render_resolve,
    "verify": render_verify,
    "stats": render_stats,
}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not args.vault:
            raise VaultError("--vault is required (or set RESEARCH_VAULT_ROOT)")
        if args.command == "find":
            if args.limit <= 0:
                raise VaultError("--limit must be positive")
            if args.offset < 0:
                raise VaultError("--offset must be non-negative")
        if args.command == "verify" and getattr(args, "page", None) is not None and args.page < 1:
            raise VaultError("--page must be >= 1")
        index = VaultIndex(Path(args.vault), args.max_file_mb)
        handler = {
            "find": cmd_find,
            "resolve": cmd_resolve,
            "verify": cmd_verify,
            "stats": cmd_stats,
        }[args.command]
        output, code = handler(args, index)
    except VaultError as error:
        payload = {"skill": SKILL_NAME, "status": "error", "message": str(error)}
        if getattr(args, "json", False):
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print("ERROR: " + str(error), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(output, ensure_ascii=False, indent=2))
    else:
        print(RENDERERS[args.command](output))
    return code


if __name__ == "__main__":
    sys.exit(main())
