#!/usr/bin/env python3
"""Scrub portco-specific identifiers from the codebase.

Reads bin/scrub-portco-patterns.yml, scans the repo (or any --root), and reports
every match. Exits 1 if any HIGH-severity finding remains after the allowlist;
0 otherwise. Designed to run pre-public-flip and as a CI gate after flip.

Usage:
    python bin/scrub-portco.py                       # scan repo root, human-readable
    python bin/scrub-portco.py --json                # JSON for tooling
    python bin/scrub-portco.py --severity HIGH       # HIGH only
    python bin/scrub-portco.py --root path/to/dir    # scan a subdir
    python bin/scrub-portco.py --patterns alt.yml    # alternate catalog
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import yaml


@dataclass(frozen=True)
class _SymlinkProbe:
    """Sentinel yielded by iter_files for tracked symlinks.

    The runner treats the link target string as scannable content so a
    sensitive substring in the target is caught even though the link
    itself isn't a regular file (codex round 11 P2).
    """

    path: Path
    rel: str
    target: str


REPO_ROOT = Path(__file__).resolve().parent.parent
PATTERN_FILE = REPO_ROOT / "bin" / "scrub-portco-patterns.yml"
NAMES_FILE = REPO_ROOT / "bin" / "scrub-portco-names.yml"

SEVERITY_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}

# Any tracked file whose basename starts with `scrub-portco-names` is
# treated as a private catalog — even if its path doesn't match the
# configured `--names` argument. A maintainer backup like
# `scrub-portco-names.yml.bak`, `scrub-portco-names-2026-05.yml`, or
# `scrub-portco-names_OLD.yml` would otherwise sail through with no
# public-shape matches. Codex round 17 P1 + claude review (`_` separator
# bypass: `\b` does not match between word chars, and `_` is a word
# char in regex, so the prior pattern missed underscore-separated
# backups).
#
# The committed synthetic template (scrub-portco-names.yml.example) is
# the one legitimate file matching this prefix; round 18 P1 removed the
# blanket basename exemption that used to live here and replaced it
# with an allowlist entry pinned by content_sha256 in the catalog YAML.
# The runner no longer special-cases the filename; the YAML allowlist
# does the path+content pin so anyone overwriting the template, or
# saving a private file under the same basename elsewhere, still trips
# the gate.
_PRIVATE_NAMES_FILENAME_RE = re.compile(r"^scrub-portco-names(?![a-zA-Z0-9])")


@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    column: int
    matched_text: str
    category: str
    pattern_name: str
    severity: str


def load_catalog(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Pattern catalog {path} did not parse to a mapping.")
    return data


def merge_catalogs(primary: dict, *others: dict) -> dict:
    """Merge multiple catalogs. Categories combine by concatenating patterns
    (with the primary's severity winning on conflict). exclude_paths and
    allowlist are concatenated. Other keys take the primary value."""
    merged: dict = dict(primary or {})
    categories = dict((merged.get("categories") or {}))
    excludes = list(merged.get("exclude_paths") or [])
    allowlist = list(merged.get("allowlist") or [])

    for other in others:
        if not other:
            continue
        for cat_name, cat_body in (other.get("categories") or {}).items():
            if not isinstance(cat_body, dict):
                continue
            existing = categories.get(cat_name)
            if existing is None:
                categories[cat_name] = dict(cat_body)
                continue
            combined = dict(existing)
            combined_patterns = list(existing.get("patterns") or [])
            combined_patterns.extend(cat_body.get("patterns") or [])
            combined["patterns"] = combined_patterns
            # Combine extension lists too if present (unscanned_binaries)
            if "extensions" in cat_body or "extensions" in existing:
                exts = list(existing.get("extensions") or [])
                for e in cat_body.get("extensions") or []:
                    if e not in exts:
                        exts.append(e)
                combined["extensions"] = exts
            categories[cat_name] = combined
        excludes.extend(other.get("exclude_paths") or [])
        allowlist.extend(other.get("allowlist") or [])

    merged["categories"] = categories
    merged["exclude_paths"] = excludes
    merged["allowlist"] = allowlist
    return merged


def collect_private_pattern_ids(catalog: dict) -> set[str]:
    """Return the set of 'category.name' pattern IDs from a private catalog.

    The gitignored bin/scrub-portco-names.yml carries real portco / vendor /
    operator names as the regex names themselves (e.g. `acme_corp`). Under
    --redact the matched_text and any in-path substrings are scrubbed, but
    the pattern_name field would still leak those names in CI logs and
    uploaded artifacts (codex round 12 P1). This set drives a second-pass
    redactor over pattern_name for findings produced by private patterns.
    """
    out: set[str] = set()
    for cat_name, cat_body in (catalog.get("categories") or {}).items():
        if not isinstance(cat_body, dict):
            continue
        for entry in cat_body.get("patterns") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if name:
                out.add(f"{cat_name}.{name}")
    return out


def collect_private_terms(catalog: dict) -> set[str]:
    """Return every identifier a private catalog might leak into error text.

    Includes both bare names ('acme_corp') and qualified ones
    ('portco_names.acme_corp'). compile_index raises ValueError with both
    shapes depending on whether the structural problem is at the category
    or pattern level. _redact_private_text uses this set to scrub
    diagnostics emitted under --redact. Codex round 14 P2.
    """
    terms: set[str] = set()
    for cat_name, cat_body in (catalog.get("categories") or {}).items():
        terms.add(cat_name)
        if not isinstance(cat_body, dict):
            continue
        for entry in cat_body.get("patterns") or []:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if name:
                terms.add(f"{cat_name}.{name}")
                terms.add(name)
    return terms


def collect_private_categories(catalog: dict) -> set[str]:
    """Return the set of top-level category names from a private catalog.

    A maintainer might name a category after a private entity (e.g.
    `acme_corp_employees`), in which case the `category` field
    inside redacted JSON output and the `summary.by_category` rollup
    would leak that name even with the existing matched_text /
    pattern_name redactions. Codex round 20 P2 — this set drives a
    second-pass redactor over `category` for findings whose source
    came from a private pattern.
    """
    out: set[str] = set()
    for cat_name in (catalog.get("categories") or {}).keys():
        out.add(cat_name)
    return out


def _redact_private_text(msg: str, private_terms: set[str]) -> str:
    """Replace every occurrence of a private term in `msg` with [REDACTED].

    Longest-first iteration prevents short tokens from collapsing prefixes
    of longer ones (e.g. 'acme' before 'acme_corp').
    """
    out = msg
    for term in sorted(private_terms, key=len, reverse=True):
        if term and term in out:
            out = out.replace(term, "[REDACTED]")
    return out


def compile_index(
    catalog: dict,
) -> list[tuple[str, str, list[tuple[str, re.Pattern[str]]]]]:
    out: list[tuple[str, str, list[tuple[str, re.Pattern[str]]]]] = []
    for category, body in (catalog.get("categories") or {}).items():
        if not isinstance(body, dict):
            raise ValueError(f"Category '{category}' must be a mapping.")
        severity = str(body.get("severity", "MEDIUM")).upper()
        if severity not in SEVERITY_RANK:
            raise ValueError(
                f"Category '{category}' severity must be HIGH/MEDIUM/LOW, got {severity!r}."
            )
        compiled: list[tuple[str, re.Pattern[str]]] = []
        for entry in body.get("patterns") or []:
            if (
                not isinstance(entry, dict)
                or "name" not in entry
                or "regex" not in entry
            ):
                raise ValueError(f"Bad pattern entry in '{category}': {entry!r}")
            try:
                compiled.append((entry["name"], re.compile(entry["regex"])))
            except re.error as exc:
                raise ValueError(
                    f"Invalid regex in {category}.{entry['name']}: {exc}"
                ) from exc
        out.append((category, severity, compiled))
    return out


def is_excluded(rel_path: str, excludes: Iterable[str]) -> bool:
    normalized = rel_path.replace("\\", "/")
    for pattern in excludes:
        if fnmatch.fnmatch(normalized, pattern):
            return True
        # Support directory-prefix excludes without trailing /**
        if pattern.endswith("/**"):
            prefix = pattern[:-3]
            if normalized == prefix or normalized.startswith(prefix + "/"):
                return True
    return False


def is_probably_binary(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            chunk = f.read(8192)
    except OSError:
        return True
    if not chunk:
        return False
    # NUL bytes are the most reliable binary signal.
    if b"\x00" in chunk:
        return True
    # Valid UTF-8 (including box-drawing, emoji, localized Markdown) should
    # be scanned, not skipped. The legacy ASCII-ratio heuristic dropped these
    # to "binary"; codex round 7 caught a tracked test file whose PR-id
    # finding was silently skipped that way. Try a strict decode first.
    try:
        chunk.decode("utf-8")
        return False
    except UnicodeDecodeError:
        pass
    # Fall back to byte-ratio: if most of the file is plain ASCII we still
    # treat it as text. Below the threshold, treat as binary.
    text_bytes = sum(1 for b in chunk if 9 <= b <= 13 or 32 <= b < 127)
    return text_bytes / len(chunk) < 0.75


def list_git_tracked(root: Path) -> list[tuple[Path, bool]] | None:
    """Return [(path, is_tracked)] for tracked AND nonignored-untracked files.

    Two git ls-files invocations:
      --cached: already-tracked files (the index) → is_tracked=True
      --others --exclude-standard: untracked files NOT matched by
        .gitignore / .git/info/exclude → is_tracked=False

    Knowing which list a file came from is what lets the caller skip
    exclude_paths for tracked entries (codex round 18 P1): a tracked
    file in `.claude/worktrees/` or `node_modules/` ships publicly and
    must be scanned, but the same path untracked is local-cache noise.

    Catches files that are about to be committed but not yet staged
    (codex round 11 P1), while still keeping gitignored items (a local
    .env) out of the scan. When `root` is a subdirectory inside a git
    worktree the prior `(root / ".git").exists()` check fell through to
    rglob, scanning gitignored files like bin/scrub-portco-names.yml —
    codex round 14 P2. The new path resolves the worktree top via
    `git rev-parse --show-toplevel` and limits ls-files to the subtree
    via pathspec, so any descendant scan still honors .gitignore.

    Returns None when root isn't inside any git worktree so callers fall
    back to rglob.
    """
    try:
        toplevel_proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    toplevel = Path(toplevel_proc.stdout.strip())
    if not toplevel:
        return None
    try:
        cached_proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--cached",
                "-z",
                "--",
                ".",
            ],
            capture_output=True,
            check=True,
        )
        others_proc = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
            ],
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    out: list[tuple[Path, bool]] = []
    for entry in cached_proc.stdout.split(b"\x00"):
        if not entry:
            continue
        rel = entry.decode("utf-8", errors="replace")
        # Paths are relative to `root` because of `-C root` + `-- .`.
        out.append((root / rel, True))
    for entry in others_proc.stdout.split(b"\x00"):
        if not entry:
            continue
        rel = entry.decode("utf-8", errors="replace")
        out.append((root / rel, False))
    return out


def iter_files(
    root: Path,
    excludes: Iterable[str],
    use_git: bool = True,
) -> Iterable[tuple[Path | _SymlinkProbe, bool]]:
    """Yield (path, is_binary_skipped_for_content_scan) for every file to evaluate.

    When `use_git` is True and root is a git tree, tracked AND
    nonignored-untracked files are yielded. Tracked files bypass the
    catalog's exclude_paths (codex round 18 P1): a force-add into a
    cache or worktree directory ships publicly and must be scanned.

    The rglob fallback (non-git tree or --no-git) includes symlinks
    (`p.is_symlink()`) in addition to regular files so links to
    directories or broken targets reach the per-file symlink handler
    instead of being dropped by `p.is_file()` — codex round 18 P2.
    """
    excludes_list = list(excludes)
    git_result = list_git_tracked(root) if use_git else None
    if git_result is not None:
        candidates: Iterable[tuple[Path, bool]] = git_result
    else:
        # Non-git mode: include symlinks so a link-to-dir or broken link
        # whose pathname or target carries a portco identifier is still
        # caught. Codex round 18 P2.
        candidates = (
            (p, False) for p in root.rglob("*") if p.is_file() or p.is_symlink()
        )

    for path, is_tracked in candidates:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        rel_str = str(rel)
        # Tracked files are NEVER excluded by catalog exclude_paths — a
        # force-add into a "noise" directory still ships publicly. Codex
        # round 18 P1. Excludes apply only to untracked files (and to
        # the rglob fallback's `is_tracked=False` synthetic entries).
        if not is_tracked and is_excluded(rel_str, excludes_list):
            continue
        # Tracked symlinks may carry a sensitive target string (e.g., a
        # portco-named path) even when the link target does not exist.
        # Git publishes the literal target, so scan that string explicitly
        # (codex round 11 P2). We do NOT follow the link to scan its
        # destination — content scanning operates only on regular files.
        if path.is_symlink():
            try:
                target = os.readlink(path)
            except OSError:
                target = ""
            if target:
                yield _SymlinkProbe(path=path, rel=rel_str, target=target), False
            continue
        if not path.is_file():
            continue
        is_binary = is_probably_binary(path)
        yield path, is_binary


def scan_path_string(
    rel: str,
    index: list[tuple[str, str, list[tuple[str, re.Pattern[str]]]]],
) -> list[Finding]:
    """Run every catalog pattern against the file path itself (not contents).

    Catches cases like `docs/research/kapa-acme-index.md` where the leak
    is in the filename rather than the file body.
    """
    findings: list[Finding] = []
    normalized = rel.replace("\\", "/")
    for category, severity, patterns in index:
        for name, compiled in patterns:
            for match in compiled.finditer(normalized):
                findings.append(
                    Finding(
                        file=rel,
                        line=0,
                        column=match.start() + 1,
                        matched_text=match.group(0),
                        category=category,
                        pattern_name=f"{name} (path)",
                        severity=severity,
                    )
                )
    return findings


def scan_file(
    path: Path,
    index: list[tuple[str, str, list[tuple[str, re.Pattern[str]]]]],
    rel: str,
) -> list[Finding]:
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for line_no, line in enumerate(text.splitlines(), start=1):
        for category, severity, patterns in index:
            for name, compiled in patterns:
                for match in compiled.finditer(line):
                    findings.append(
                        Finding(
                            file=rel,
                            line=line_no,
                            column=match.start() + 1,
                            matched_text=match.group(0),
                            category=category,
                            pattern_name=name,
                            severity=severity,
                        )
                    )
    return findings


def binary_extensions(catalog: dict) -> tuple[set[str], str]:
    """Return (set of high-risk extensions, severity) for unscanned_binaries."""
    cat = (catalog.get("categories") or {}).get("unscanned_binaries") or {}
    exts_raw = cat.get("extensions") or []
    severity = str(cat.get("severity", "MEDIUM")).upper()
    exts = {str(e).lower() for e in exts_raw if isinstance(e, str)}
    return exts, severity


def make_binary_finding(rel: str, severity: str) -> Finding:
    return Finding(
        file=rel,
        line=0,
        column=0,
        matched_text=Path(rel).name,
        category="unscanned_binaries",
        pattern_name="tracked_binary",
        severity=severity,
    )


@dataclass(frozen=True)
class AllowlistEntry:
    file_glob: str
    pattern_id: str
    text_pattern: re.Pattern[str] | None
    content_sha256: str | None


# Allowlist entries that MUST carry a content_sha256 to apply, and whose
# digest is re-verified against the file's current content. Without this
# pinning, a later commit could swap the file's contents (e.g. replace
# an allowlisted .docx with one carrying new private data, or overwrite
# the committed `.example` template with the real names catalog) and
# the gate would still pass. Codex round 17 P2 (binaries) + round 18 P1
# (private-catalog template).
_DIGEST_PINNED_PATTERN_IDS = frozenset(
    {
        "unscanned_binaries.tracked_binary",
        "private_catalog_tracked.private_names_catalog",
    }
)


def build_allowlist(catalog: dict) -> list[AllowlistEntry]:
    """Compile allowlist entries from the catalog.

    Each entry binds a (file_glob, pattern_id) and OPTIONALLY a regex that the
    matched text must satisfy for the suppression to apply. Codex round 8
    P2: without the text discriminator, a copy-pasted real identifier in a
    test file is silently accepted. Round 17 P2 adds an optional
    content_sha256 to pin binary exemptions to the reviewed content.
    """
    out: list[AllowlistEntry] = []
    raw = catalog.get("allowlist") or []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        file_glob = entry.get("file")
        pattern_id = entry.get("pattern")
        if not file_glob or not pattern_id:
            continue
        text_pattern_raw = entry.get("text_pattern")
        text_pattern: re.Pattern[str] | None = None
        if text_pattern_raw:
            try:
                text_pattern = re.compile(str(text_pattern_raw))
            except re.error:
                continue
        sha256_raw = entry.get("content_sha256")
        content_sha256 = str(sha256_raw).lower().strip() if sha256_raw else None
        out.append(
            AllowlistEntry(
                file_glob=str(file_glob),
                pattern_id=str(pattern_id),
                text_pattern=text_pattern,
                content_sha256=content_sha256,
            )
        )
    return out


def _file_sha256(path: Path) -> str | None:
    """Return the hex sha256 of `path` or None on read failure."""
    import hashlib

    try:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def is_allowed(
    finding: Finding,
    allowlist: list[AllowlistEntry],
    subtree_prefix: str = "",
    *,
    file_path: Path | None = None,
) -> bool:
    """Return True if `finding` is suppressed by any allowlist entry.

    Allowlist file_globs are repo-relative by convention (e.g.
    `bin/scrub_portco_test.py`). When --root scans a subdirectory the
    Finding.file is subtree-relative (`scrub_portco_test.py`) and would
    miss those globs. Codex round 12 P2: when `subtree_prefix` is set
    (the scan root's path relative to the repo root) try the glob
    against the prefixed form too so a subtree scan still honors the
    same allowlist a full-repo scan does.

    For unscanned_binaries findings the allowlist requires a
    content_sha256 (codex round 17 P2). Without one the entry simply
    does not suppress the binary; with one, the digest must still match
    the file's current content. Caller passes `file_path` for the digest
    check; if absent the digest gate is skipped (e.g. path-string
    findings where the file may have been removed by the time we emit).
    """
    pattern_id = f"{finding.category}.{finding.pattern_name}"
    # Path findings end with " (path)" and symlink findings with
    # " (symlink target)"; allowlist entries match the bare pattern name.
    base_pattern_id = pattern_id.replace(" (path)", "").replace(" (symlink target)", "")
    rel = finding.file.replace("\\", "/")
    rel_with_prefix = f"{subtree_prefix}/{rel}" if subtree_prefix else rel
    requires_digest = base_pattern_id in _DIGEST_PINNED_PATTERN_IDS
    for entry in allowlist:
        if entry.pattern_id not in (pattern_id, base_pattern_id):
            continue
        if not (
            fnmatch.fnmatch(rel, entry.file_glob)
            or (subtree_prefix and fnmatch.fnmatch(rel_with_prefix, entry.file_glob))
        ):
            continue
        if entry.text_pattern is not None:
            if not entry.text_pattern.search(finding.matched_text):
                continue
        if requires_digest:
            # Hard requirement: digest-pinned findings (binaries +
            # tracked private catalogs) MUST carry a content_sha256
            # that equals the file's current digest. Otherwise a later
            # commit could swap the contents and the gate would still
            # pass.
            if entry.content_sha256 is None:
                continue
            if file_path is None:
                continue
            actual = _file_sha256(file_path)
            if actual is None or actual != entry.content_sha256:
                continue
        return True
    return False


def filter_by_severity(findings: list[Finding], min_severity: str) -> list[Finding]:
    if min_severity == "ALL":
        return findings
    threshold = SEVERITY_RANK[min_severity]
    return [f for f in findings if SEVERITY_RANK[f.severity] >= threshold]


def counts_by(findings: list[Finding], attr: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for f in findings:
        key = getattr(f, attr)
        out[key] = out.get(key, 0) + 1
    return out


def _sha_redact(text: str, strategy: str = "placeholder") -> str:
    """Redact a sensitive matched value for emission.

    Two strategies:
      placeholder (default, CI-safe): all values collapse to a constant
        '[REDACTED]'. Cross-run correlation is lost but low-entropy names
        ('Acme', 'Acme Corp') cannot be dictionary-attacked from the
        published digest (codex round 11 P2).
      hash: 12-prefix of sha256. Useful for local debugging where the
        operator wants to see distinct findings without printing the
        plaintext, but unsafe for public CI logs.
    """
    if strategy == "hash":
        import hashlib

        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"[REDACTED sha256:{digest}]"
    return "[REDACTED]"


def _build_path_redactor(
    findings: list[Finding], strategy: str = "placeholder"
) -> dict[str, str]:
    """Collect every matched substring that appears in any file PATH (across
    all findings). Codex round 10 P1: a single path may produce multiple
    findings (e.g., path-name + tracked_binary), or contain multiple
    sensitive substrings; redact every known one before emitting.
    """
    out: dict[str, str] = {}
    for f in findings:
        if f.matched_text and f.matched_text in f.file:
            out.setdefault(f.matched_text, _sha_redact(f.matched_text, strategy))
    return out


def _apply_path_redactor(file: str, redactor: dict[str, str]) -> str:
    out = file
    # Longest-first: with overlapping captures ('Acme' and 'Acme Corp')
    # a short-first pass collapses 'Acme' before the longer literal can
    # match, leaving '[REDACTED] Corp.md' visible. Sort by descending
    # length so the longer literal redacts first. Codex round 14 P2.
    for literal in sorted(redactor, key=len, reverse=True):
        if literal in out:
            out = out.replace(literal, redactor[literal])
    return out


def _strip_pattern_suffix(pattern_name: str) -> str:
    """Strip ' (path)' / ' (symlink target)' suffixes from a Finding's
    pattern_name so the result matches the bare 'category.name' form used
    in the private-pattern set and the allowlist."""
    return pattern_name.replace(" (path)", "").replace(" (symlink target)", "")


def _redact_pattern_name(f: Finding, private_pattern_ids: set[str]) -> str:
    """Replace the name portion of a private pattern_name with [REDACTED].

    Path findings carry a ' (path)' suffix and symlink findings carry
    ' (symlink target)'; preserve the suffix so an operator running with
    --redact can still see which scan path produced the hit.
    """
    base = _strip_pattern_suffix(f.pattern_name)
    pattern_id = f"{f.category}.{base}"
    if pattern_id not in private_pattern_ids:
        return f.pattern_name
    suffix = f.pattern_name[len(base) :]
    return f"[REDACTED]{suffix}"


def _redact_category(category: str, private_categories: set[str]) -> str:
    """Replace a private catalog's category name with '[REDACTED]'.

    A maintainer-controlled category in the gitignored names catalog
    can encode a private entity name (e.g. `acme_corp_employees`).
    Under --redact this collapses to '[REDACTED]' for both the
    per-finding `category` field and the `summary.by_category` rollup.
    Codex round 20 P2.
    """
    if category in private_categories:
        return "[REDACTED]"
    return category


def _redact_finding(
    f: Finding,
    path_redactor: dict[str, str],
    private_pattern_ids: set[str],
    private_categories: set[str],
    strategy: str = "placeholder",
) -> dict:
    """Convert a Finding to dict with the actual matched_text redacted, and
    every known sensitive substring in `file` replaced too."""
    return {
        "file": _apply_path_redactor(f.file, path_redactor),
        "line": f.line,
        "column": f.column,
        "matched_text": _sha_redact(f.matched_text, strategy),
        "category": _redact_category(f.category, private_categories),
        "pattern_name": _redact_pattern_name(f, private_pattern_ids),
        "severity": f.severity,
    }


def emit_json(
    summary: dict,
    findings: list[Finding],
    path_redactor: dict[str, str],
    private_pattern_ids: set[str],
    private_categories: set[str],
    redact: bool = False,
    redact_strategy: str = "placeholder",
) -> None:
    if redact:
        rows = [
            _redact_finding(
                f,
                path_redactor,
                private_pattern_ids,
                private_categories,
                redact_strategy,
            )
            for f in findings
        ]
    else:
        rows = [asdict(f) for f in findings]
    payload = {
        "summary": summary,
        "findings": rows,
    }
    sys.stdout.write(json.dumps(payload, indent=2))
    sys.stdout.write("\n")


def _display_text(text: str, redact: bool, strategy: str = "placeholder") -> str:
    if not redact:
        return repr(text)
    return f"'{_sha_redact(text, strategy)}'"


def emit_human(
    summary: dict,
    findings: list[Finding],
    path_redactor: dict[str, str],
    private_pattern_ids: set[str],
    private_categories: set[str],
    max_per_severity: int = 50,
    redact: bool = False,
    redact_strategy: str = "placeholder",
) -> None:
    print(f"scrub-portco: scanned {summary['scan_root']}")
    print(f"  total findings: {summary['total_findings']}")
    if summary["total_findings"] == 0:
        print("\nNo findings. Codebase is scrub-clean.")
        return
    print(f"  by severity:   {summary['by_severity']}")
    print(f"  by category:   {summary['by_category']}")
    for severity in ("HIGH", "MEDIUM", "LOW"):
        bucket = [f for f in findings if f.severity == severity]
        if not bucket:
            continue
        shown = bucket[:max_per_severity]
        print(f"\n{severity} ({len(bucket)}):")
        for f in shown:
            display = _display_text(f.matched_text, redact, redact_strategy)
            display_path = (
                _apply_path_redactor(f.file, path_redactor) if redact else f.file
            )
            pattern_display = (
                _redact_pattern_name(f, private_pattern_ids)
                if redact
                else f.pattern_name
            )
            category_display = (
                _redact_category(f.category, private_categories)
                if redact
                else f.category
            )
            print(
                f"  {display_path}:{f.line}:{f.column} "
                f"[{category_display}.{pattern_display}] {display}"
            )
        if len(bucket) > max_per_severity:
            print(f"  ... and {len(bucket) - max_per_severity} more {severity}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None
    )
    parser.add_argument(
        "--root",
        default=str(REPO_ROOT),
        help="Repo root to scan (default: repo root inferred from script path).",
    )
    parser.add_argument(
        "--patterns",
        default=str(PATTERN_FILE),
        help="Pattern catalog YAML (default: bin/scrub-portco-patterns.yml). Contains "
        "generic shape patterns safe to ship publicly.",
    )
    parser.add_argument(
        "--names",
        default=str(NAMES_FILE),
        help="Additional catalog with literal portco/vendor/operator names "
        "(default: bin/scrub-portco-names.yml). Auto-loaded if present; "
        "ignored without error if absent. Gitignored by design.",
    )
    parser.add_argument(
        "--no-names",
        dest="load_names",
        action="store_false",
        default=True,
        help="Skip the --names file even if it exists. Useful for CI runs "
        "that should only validate generic shape patterns.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout instead of human-readable output.",
    )
    parser.add_argument(
        "--severity",
        choices=["HIGH", "MEDIUM", "LOW", "ALL"],
        default="ALL",
        help="Report only findings at this severity or above (default: ALL).",
    )
    parser.add_argument(
        "--fail-on",
        choices=["HIGH", "MEDIUM", "LOW", "NEVER"],
        default="HIGH",
        help="Exit non-zero when at least one finding at this severity remains (default: HIGH).",
    )
    parser.add_argument(
        "--no-git",
        dest="use_git",
        action="store_false",
        default=True,
        help="Disable `git ls-files` scoping; scan every file under --root.",
    )
    parser.add_argument(
        "--redact",
        action="store_true",
        help="Replace matched_text (and any matched substring inside `file`) "
        "with a placeholder in both human and JSON output. Use in CI/Action "
        "logs and uploaded artifacts so a later-scrubbed identifier is not "
        "retained in the workflow history.",
    )
    parser.add_argument(
        "--redact-strategy",
        choices=["placeholder", "hash"],
        default="placeholder",
        help="placeholder (default, CI-safe): collapses every redacted value to "
        "the literal string '[REDACTED]'. Low-entropy names cannot be "
        "dictionary-attacked from the output. "
        "hash: emits a 12-char sha256 prefix; preserves cross-run correlation "
        "for local debugging but is NOT safe for public CI logs.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    root = Path(args.root).resolve()
    if not root.exists():
        sys.stderr.write(f"scrub-portco: --root not found: {root}\n")
        return 2

    patterns_path = Path(args.patterns)
    if not patterns_path.is_absolute():
        patterns_path = (REPO_ROOT / patterns_path).resolve()
    if not patterns_path.exists():
        sys.stderr.write(f"scrub-portco: --patterns file not found: {patterns_path}\n")
        return 2

    private_pattern_ids: set[str] = set()
    private_terms: set[str] = set()
    private_categories: set[str] = set()
    try:
        catalog = load_catalog(patterns_path)
        names_path = Path(args.names)
        if not names_path.is_absolute():
            names_path = (REPO_ROOT / names_path).resolve()
        if args.load_names and names_path.exists():
            try:
                names_catalog = load_catalog(names_path)
            except yaml.YAMLError as exc:
                # PyYAML errors include the offending source line, which
                # in the private names catalog can be a real portco /
                # vendor name. Under --redact print a generic error;
                # otherwise let the maintainer see the real diagnostic.
                # Codex round 15 P2.
                if args.redact:
                    sys.stderr.write(
                        "scrub-portco: catalog error: failed to parse "
                        "the private names catalog (YAML error; details "
                        "hidden under --redact)\n"
                    )
                else:
                    sys.stderr.write(f"scrub-portco: catalog error: {exc}\n")
                return 2
            private_pattern_ids = collect_private_pattern_ids(names_catalog)
            private_terms = collect_private_terms(names_catalog)
            private_categories = collect_private_categories(names_catalog)
            # Validate the private catalog in isolation so any subsequent
            # ValueError on the merged compile is attributable to the
            # public catalog only. _redact_private_text() only substitutes
            # known category/pattern names; if compile_index() raises with
            # arbitrary sensitive text from a pattern body (e.g. an invalid
            # regex containing a portco name), partial sanitization leaks
            # that text. The right answer is to never print verbatim
            # str(exc) for private-catalog errors at all. Codex round 16 P2.
            try:
                compile_index(names_catalog)
            except ValueError as exc:
                if args.redact:
                    sys.stderr.write(
                        "scrub-portco: catalog error: failed to compile "
                        "the private names catalog (details hidden under "
                        "--redact)\n"
                    )
                else:
                    sys.stderr.write(f"scrub-portco: catalog error: {exc}\n")
                return 2
            catalog = merge_catalogs(catalog, names_catalog)
        index = compile_index(catalog)
    except ValueError as exc:
        msg = str(exc)
        # Defense-in-depth: if a private term leaks into a merged-compile
        # error (which shouldn't happen now that we validate the private
        # catalog in isolation), still scrub known terms under --redact.
        # Codex round 14 P2.
        if args.redact and private_terms:
            msg = _redact_private_text(msg, private_terms)
        sys.stderr.write(f"scrub-portco: catalog error: {msg}\n")
        return 2

    excludes = catalog.get("exclude_paths") or []
    allowlist = build_allowlist(catalog)
    high_risk_exts, binary_severity = binary_extensions(catalog)

    # Tracked-private-catalog guard: the names catalog at `--names` is
    # supposed to be gitignored. If a force-add has tracked it, the scanner
    # would normally either skip it (--no-names) or treat it as ordinary
    # text whose content does not match any public-shape regex — the gate
    # passes clean and the entire private catalog ships publicly. Pin the
    # absolute path here so the per-file loop can flag it with HIGH
    # regardless of contents or --no-names. Codex round 16 P1.
    try:
        private_catalog_abs = names_path.resolve()
    except (OSError, RuntimeError):
        private_catalog_abs = None
    private_catalog_severity = str(
        ((catalog.get("categories") or {}).get("private_catalog_tracked") or {}).get(
            "severity", "HIGH"
        )
    ).upper()

    # Subtree-scan support: when --root points inside REPO_ROOT, capture the
    # relative prefix so allowlist file_globs written repo-relatively (e.g.
    # `bin/scrub_portco_test.py`) still match findings whose rel path is
    # subtree-relative (`scrub_portco_test.py`). Codex round 12 P2.
    try:
        rel_prefix = root.relative_to(REPO_ROOT)
        subtree_prefix = (
            "" if str(rel_prefix) == "." else str(rel_prefix).replace("\\", "/")
        )
    except ValueError:
        subtree_prefix = ""

    all_findings: list[Finding] = []
    for item, is_binary in iter_files(root, excludes, use_git=args.use_git):
        # Tracked symlinks: scan the target string only.
        if isinstance(item, _SymlinkProbe):
            rel = item.rel
            basename = Path(rel).name
            # Symlink whose name or target points at the private names
            # catalog: same risk as tracking the file directly. The
            # regular-file branch below catches the regular-file case;
            # this branch covers tracked symlinks. Claude review follow-up
            # to codex round 18 P1.
            is_private_name_link = (
                _PRIVATE_NAMES_FILENAME_RE.match(basename) is not None
            )
            link_target_abs: Path | None = None
            if private_catalog_abs is not None:
                try:
                    link_target_abs = (item.path.parent / item.target).resolve()
                except (OSError, RuntimeError):
                    link_target_abs = None
            is_link_to_private = (
                link_target_abs is not None and link_target_abs == private_catalog_abs
            )
            if is_private_name_link or is_link_to_private:
                sym_finding = Finding(
                    file=rel,
                    line=0,
                    column=0,
                    matched_text=basename,
                    category="private_catalog_tracked",
                    pattern_name="private_names_catalog (symlink)",
                    severity=private_catalog_severity,
                )
                if not is_allowed(
                    sym_finding, allowlist, subtree_prefix, file_path=item.path
                ):
                    all_findings.append(sym_finding)
                # Skip further symlink scans — already flagged HIGH.
                continue
            # Path-string scan against the link's location:
            for finding in scan_path_string(rel, index):
                if is_allowed(finding, allowlist, subtree_prefix):
                    continue
                all_findings.append(finding)
            # Target-string scan: build a synthetic single-line Finding by
            # running the patterns against the readlink output.
            for category, severity, patterns in index:
                for name, compiled in patterns:
                    for match in compiled.finditer(item.target):
                        sym_finding = Finding(
                            file=rel,
                            line=0,
                            column=match.start() + 1,
                            matched_text=match.group(0),
                            category=category,
                            pattern_name=f"{name} (symlink target)",
                            severity=severity,
                        )
                        if not is_allowed(sym_finding, allowlist, subtree_prefix):
                            all_findings.append(sym_finding)
            continue

        path = item
        rel = str(path.relative_to(root))

        # Tracked private names catalog → unconditional HIGH unless
        # allowlist-suppressed (with content_sha256 pinning the
        # reviewed template). Codex round 16 P1 + round 18 P1.
        #
        # The check is two-pronged: (1) exact path match against the
        # configured --names argument, (2) filename-shape match so a
        # maintainer backup such as `scrub-portco-names.yml.bak` or
        # `scrub-portco-names-2026-05.yml` is rejected too — codex
        # round 17 P1 flagged the renamed-copy bypass. The committed
        # synthetic .example file is allowed only through an explicit
        # allowlist entry pinned to a content_sha256 (round 18 P1);
        # there is no runner-side filename exemption.
        basename = Path(rel).name
        is_private_copy = _PRIVATE_NAMES_FILENAME_RE.match(basename) is not None
        is_configured_private = False
        if private_catalog_abs is not None:
            try:
                resolved = path.resolve()
            except (OSError, RuntimeError):
                resolved = None
            if resolved is not None and resolved == private_catalog_abs:
                is_configured_private = True
        if is_private_copy or is_configured_private:
            finding = Finding(
                file=rel,
                line=0,
                column=0,
                matched_text=basename,
                category="private_catalog_tracked",
                pattern_name="private_names_catalog",
                severity=private_catalog_severity,
            )
            if not is_allowed(finding, allowlist, subtree_prefix, file_path=path):
                all_findings.append(finding)
            # Skip content scanning of this file: anything inside is by
            # definition private and (if not allowlist-suppressed) the
            # HIGH finding already blocks the gate.
            continue

        # Always scan the path string first so a sensitive identifier in a
        # filename (e.g., a Slack channel ID embedded in a PDF export name)
        # surfaces as HIGH even when the file is a binary we cannot read.
        for finding in scan_path_string(rel, index):
            if is_allowed(finding, allowlist, subtree_prefix):
                continue
            all_findings.append(finding)

        if path.suffix.lower() in high_risk_exts:
            finding = make_binary_finding(rel, binary_severity)
            # Pass file_path so the binary allowlist's content_sha256 can
            # be verified against the file's current digest. Codex round
            # 17 P2: without this an allowlisted .docx can be replaced
            # with one carrying new private data and the gate still
            # passes.
            if not is_allowed(finding, allowlist, subtree_prefix, file_path=path):
                all_findings.append(finding)
            # When the high-risk extension is allowlisted (operator has
            # reviewed it) AND the file is actually readable as text
            # (e.g., .csv/.tsv exports), continue into content scanning so
            # a later commit that adds a sensitive identifier is still
            # caught. Truly binary high-risk files (.docx/.pdf/.parquet)
            # fall through to the binary check below.
            if not is_binary:
                for f in scan_file(path, index, rel):
                    if is_allowed(f, allowlist, subtree_prefix):
                        continue
                    all_findings.append(f)
            continue
        if is_binary:
            continue

        for finding in scan_file(path, index, rel):
            if is_allowed(finding, allowlist, subtree_prefix):
                continue
            all_findings.append(finding)

    # Build the path redactor over ALL findings (pre severity filter) so a
    # substring matched only by a MEDIUM pattern is still scrubbed from a
    # HIGH finding's `file` field when --severity HIGH suppresses the MEDIUM
    # row. Codex round 12 P2.
    path_redactor = (
        _build_path_redactor(all_findings, args.redact_strategy) if args.redact else {}
    )

    visible = filter_by_severity(all_findings, args.severity)

    # Under --redact, scan_root may itself contain portco/operator names
    # (e.g. a CI workspace named `acme-corp-runner`). Collapse it to a
    # constant rather than leaking it into summary metadata of an
    # uploaded artifact. Codex round 15 P2.
    scan_root_display = "[REDACTED]" if args.redact else str(root)

    # The by_category rollup uses raw category strings, which can
    # encode private entity names when sourced from a private catalog.
    # Substitute private category names with '[REDACTED]' under
    # --redact. Codex round 20 P2.
    if args.redact and private_categories:
        by_category_raw = counts_by(visible, "category")
        by_category: dict[str, int] = {}
        for cat, count in by_category_raw.items():
            display_cat = _redact_category(cat, private_categories)
            by_category[display_cat] = by_category.get(display_cat, 0) + count
    else:
        by_category = counts_by(visible, "category")

    summary = {
        "scan_root": scan_root_display,
        "total_findings": len(visible),
        "by_severity": counts_by(visible, "severity"),
        "by_category": by_category,
    }

    if args.json:
        emit_json(
            summary,
            visible,
            path_redactor=path_redactor,
            private_pattern_ids=private_pattern_ids,
            private_categories=private_categories,
            redact=args.redact,
            redact_strategy=args.redact_strategy,
        )
    else:
        emit_human(
            summary,
            visible,
            path_redactor=path_redactor,
            private_pattern_ids=private_pattern_ids,
            private_categories=private_categories,
            redact=args.redact,
            redact_strategy=args.redact_strategy,
        )

    if args.fail_on == "NEVER":
        return 0
    threshold = SEVERITY_RANK[args.fail_on]
    blocking = [f for f in all_findings if SEVERITY_RANK[f.severity] >= threshold]
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
