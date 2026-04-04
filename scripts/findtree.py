#!/usr/bin/env python3
r"""
findtree.py

Folder-level scanner: "Which files under this root contain this pattern?"

SEARCH MODEL
------------
- Answers only whether a file matches — not where inside it.
- Patterns are Python regular expressions.
- Matching is line-based (no cross-line regex).
- Skips bulky dirs by default: .git, node_modules, venv, __pycache__, dist, etc.
- Skips binary files by default (--allow-binary to disable).

OUTPUT  (JSON by default)
  success → stdout:
    {"matched_files": ["src/foo.py", ...]}
    with --show errors/summary/all: also "error_files", "summary" keys
  failure → stderr, exit 1:
    {"ok": false, "error": "..."}

EXAMPLES
--------
  python findtree.py --root ./src --pattern "refreshAssemblyPanel"
  python findtree.py --root ./src --pattern "class\s+\w+Service"
  python findtree.py --root ./src --pattern "TODO" --include "*.py" "*.java"
  python findtree.py --root ./src --pattern "TODO" --exclude "tests/**"
  python findtree.py --root ./src --pattern "deprecated_api" --max-results 1
  python findtree.py --root ./src --pattern "TODO" --show all
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Pattern, Sequence

# Directory names skipped by default to avoid spending most time inside caches,
# vendored dependencies, or build outputs.
DEFAULT_EXCLUDED_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "venv",
    ".venv",
    "__pycache__",
    "dist",
    "build",
    "target",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "coverage",
}

# A small header read is enough for a useful binary heuristic in the common
# source-search use case.
BINARY_SAMPLE_SIZE = 8192
# A 1 MiB read buffer is a good default for streaming search: large enough to
# reduce syscall overhead, small enough to keep memory bounded.
CHUNK_SIZE = 1024 * 1024


@dataclass
class ScanError:
    path: str
    error: str


@dataclass
class ScanSummary:
    visited_dirs: int = 0
    candidate_files: int = 0
    searched_files: int = 0
    matched_count: int = 0
    error_count: int = 0
    skipped_by_filter_count: int = 0
    skipped_binary_count: int = 0
    skipped_size_count: int = 0
    truncated: bool = False


@dataclass
class SearchConfig:
    root: Path
    pattern: str
    ignore_case: bool
    include: List[str]
    exclude: List[str]
    show: str
    max_results: Optional[int]
    max_file_size: Optional[int]
    skip_binary: bool
    follow_symlinks: bool
    use_default_dir_excludes: bool
    error_details: bool
    as_text: bool


@dataclass
class SearchResult:
    root: str
    matched_files: List[str]
    errors: List[ScanError]
    summary: ScanSummary


def parse_positive_int(value: str, field_name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be > 0.")
    return parsed


def parse_size_limit(value: Optional[str]) -> Optional[int]:
    """
    Parse max file size from forms like:
        1000
        64K
        10M
        1G
    Units are powers of 1024.
    """
    if value is None:
        return None

    text = value.strip()
    match = re.fullmatch(r"(\d+)([KMG]?)", text, flags=re.IGNORECASE)
    if not match:
        raise ValueError("--max-file-size must be an integer optionally followed by K, M, or G.")

    amount = int(match.group(1))
    unit = match.group(2).upper()
    multiplier = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3}[unit]
    return amount * multiplier




def parse_regex(pattern: str, ignore_case: bool) -> Pattern[str]:
    if pattern == "":
        raise ValueError("PATTERN must not be empty.")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern {pattern!r}: {exc}") from exc


def normalize_relpath(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def matches_any_glob(rel_path: str, patterns: Sequence[str]) -> bool:
    """
    Match user globs against both:
    - the normalized relative path (e.g. src/core/a.py)
    - the basename only          (e.g. a.py)

    This makes patterns like '*.py' and 'tests/**' both convenient.
    """
    if not patterns:
        return False
    basename = rel_path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(basename, pat) for pat in patterns)


def should_search_file(rel_path: str, config: SearchConfig) -> bool:
    if config.include and not matches_any_glob(rel_path, config.include):
        return False
    if config.exclude and matches_any_glob(rel_path, config.exclude):
        return False
    return True


def should_skip_dir(entry_name: str, rel_dir: str, config: SearchConfig) -> bool:
    if config.use_default_dir_excludes and entry_name in DEFAULT_EXCLUDED_DIR_NAMES:
        return True
    if config.exclude and matches_any_glob(rel_dir, config.exclude):
        return True
    return False


def iter_files(root: Path, config: SearchConfig, errors: List[ScanError], summary: ScanSummary) -> Iterator[Path]:
    """
    Iterative directory traversal using os.scandir() for speed.

    Important design choices:
    - iterative stack instead of recursion to avoid recursion depth issues
    - optional symlink following
    - guarded directory open so permission errors become result data rather than
      hard failures
    - realpath cycle protection when symlink following is enabled
    """
    stack: List[Path] = [root]
    seen_real_dirs: set[str] = set()

    while stack:
        current_dir = stack.pop()
        try:
            real_dir = os.path.realpath(current_dir)
            if config.follow_symlinks:
                if real_dir in seen_real_dirs:
                    continue
                seen_real_dirs.add(real_dir)

            summary.visited_dirs += 1
            with os.scandir(current_dir) as it:
                for entry in it:
                    entry_path = Path(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=config.follow_symlinks):
                            rel_dir = normalize_relpath(entry_path, root)
                            if should_skip_dir(entry.name, rel_dir, config):
                                continue
                            stack.append(entry_path)
                            continue

                        if entry.is_file(follow_symlinks=config.follow_symlinks):
                            yield entry_path
                    except OSError as exc:
                        summary.error_count += 1
                        errors.append(ScanError(path=normalize_best_effort(entry_path, root), error=str(exc)))
        except OSError as exc:
            summary.error_count += 1
            errors.append(ScanError(path=normalize_best_effort(current_dir, root), error=str(exc)))


def normalize_best_effort(path: Path, root: Path) -> str:
    try:
        return normalize_relpath(path, root)
    except Exception:
        try:
            return path.as_posix()
        except Exception:
            return str(path)


def is_probably_binary(path: Path) -> bool:
    with path.open("rb") as fh:
        sample = fh.read(BINARY_SAMPLE_SIZE)
    return b"\x00" in sample



def file_contains_regex_linewise(path: Path, compiled: Pattern[str]) -> bool:
    """
    Regex existence search with line-based semantics.

    We stream the file line-by-line instead of loading the whole file. This is a
    deliberate trade-off:
    - better memory profile on large files
    - semantics align with the file-level tool's line-oriented regex search
    - early exit on first matching line
    """
    with path.open("r", encoding="utf-8", errors="replace", buffering=CHUNK_SIZE) as fh:
        for line in fh:
            if compiled.search(line):
                return True
    return False


def search_path(path: Path, compiled_regex: Pattern[str]) -> bool:
    return file_contains_regex_linewise(path, compiled_regex)


def run_search(config: SearchConfig) -> SearchResult:
    matched_files: List[str] = []
    errors: List[ScanError] = []
    summary = ScanSummary()

    compiled_regex = parse_regex(config.pattern, config.ignore_case)

    for path in iter_files(config.root, config, errors, summary):
        rel_path = normalize_relpath(path, config.root)

        if not should_search_file(rel_path, config):
            summary.skipped_by_filter_count += 1
            continue

        summary.candidate_files += 1

        try:
            stat = path.stat(follow_symlinks=config.follow_symlinks)
            if config.max_file_size is not None and stat.st_size > config.max_file_size:
                summary.skipped_size_count += 1
                continue

            if config.skip_binary and is_probably_binary(path):
                summary.skipped_binary_count += 1
                continue

            summary.searched_files += 1
            if search_path(path, compiled_regex):
                matched_files.append(rel_path)
                summary.matched_count += 1
                if config.max_results is not None and summary.matched_count >= config.max_results:
                    summary.truncated = True
                    break

        except OSError as exc:
            summary.error_count += 1
            errors.append(ScanError(path=rel_path, error=str(exc)))

    matched_files.sort()
    errors.sort(key=lambda item: item.path)
    return SearchResult(root=str(config.root), matched_files=matched_files, errors=errors, summary=summary)


def build_payload(result: SearchResult, config: SearchConfig) -> dict:
    payload: dict = {}

    if config.show in {"matched", "both", "all"}:
        payload["matched_files"] = result.matched_files

    if config.show in {"errors", "both", "all"}:
        payload["error_files"] = [item.path for item in result.errors]
        if config.error_details:
            payload["error_details"] = [{"path": item.path, "error": item.error} for item in result.errors]

    if config.show in {"summary", "all"}:
        payload["summary"] = {
            "visited_dirs": result.summary.visited_dirs,
            "candidate_files": result.summary.candidate_files,
            "searched_files": result.summary.searched_files,
            "matched_count": result.summary.matched_count,
            "error_count": result.summary.error_count,
            "skipped_by_filter_count": result.summary.skipped_by_filter_count,
            "skipped_binary_count": result.summary.skipped_binary_count,
            "skipped_size_count": result.summary.skipped_size_count,
            "truncated": result.summary.truncated,
        }

    return payload


def emit_text(result: SearchResult, config: SearchConfig) -> None:
    payload = build_payload(result, config)

    # Simple readable text. JSON remains the canonical machine-friendly format.
    if "matched_files" in payload:
        print("MATCHED FILES")
        for rel_path in payload["matched_files"]:
            print(rel_path)

    if "error_files" in payload:
        if "matched_files" in payload:
            print()
        print("ERROR FILES")
        if config.error_details and "error_details" in payload:
            for item in payload["error_details"]:
                print(f"{item['path']} :: {item['error']}")
        else:
            for rel_path in payload["error_files"]:
                print(rel_path)

    if "summary" in payload:
        if "matched_files" in payload or "error_files" in payload:
            print()
        print("SUMMARY")
        for key, value in payload["summary"].items():
            print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fast folder scanner that returns relative paths of files containing a pattern."
    )
    parser.add_argument("--root", required=True, help="Root directory to scan.")
    parser.add_argument("--pattern", required=True, help="Pattern to search for (Python regex).")
    parser.add_argument(
        "--ignore-case",
        action="store_true",
        help="Search case-insensitively.",
    )
    parser.add_argument(
        "--include",
        nargs="*",
        default=[],
        metavar="GLOB",
        help="Only search files whose relative path or basename matches any of these globs.",
    )
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        metavar="GLOB",
        help="Skip files or directories whose relative path or basename matches any of these globs.",
    )
    parser.add_argument(
        "--show",
        choices=["matched", "errors", "both", "summary", "all"],
        default="matched",
        help="Which result sections to emit. Default: matched.",
    )
    parser.add_argument(
        "--max-results",
        help="Stop after this many matching files have been found.",
    )
    parser.add_argument(
        "--max-file-size",
        help="Skip files larger than this size. Examples: 64K, 10M, 1G.",
    )
    parser.add_argument(
        "--allow-binary",
        action="store_true",
        help="Search files even if they look binary. By default binary files are skipped.",
    )
    parser.add_argument(
        "--follow-symlinks",
        action="store_true",
        help="Follow symlinked files/directories. Directory cycles are guarded by realpath tracking.",
    )
    parser.add_argument(
        "--no-default-dir-excludes",
        action="store_true",
        help="Do not skip built-in bulky directories such as .git and node_modules.",
    )
    parser.add_argument(
        "--error-details",
        action="store_true",
        help="Include detailed error messages in JSON output or text output error section.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Emit human-readable text instead of JSON.",
    )
    return parser


def parse_args(argv: Optional[Sequence[str]] = None) -> SearchConfig:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    if not root.exists():
        raise ValueError(f"Root directory not found: {args.root}")
    if not root.is_dir():
        raise ValueError(f"Root path is not a directory: {args.root}")
    if args.pattern == "":
        raise ValueError("PATTERN must not be empty.")

    max_results = parse_positive_int(args.max_results, "--max-results") if args.max_results is not None else None
    max_file_size = parse_size_limit(args.max_file_size)

    return SearchConfig(
        root=root,
        pattern=args.pattern,
        ignore_case=args.ignore_case,
        include=args.include,
        exclude=args.exclude,
        show=args.show,
        max_results=max_results,
        max_file_size=max_file_size,
        skip_binary=not args.allow_binary,
        follow_symlinks=args.follow_symlinks,
        use_default_dir_excludes=not args.no_default_dir_excludes,
        error_details=args.error_details,
        as_text=args.text,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    # Peek at raw argv once so we can emit the right error format even if
    # parse_args itself raises (e.g. invalid --root or empty --pattern).
    raw_argv: Sequence[str] = argv if argv is not None else sys.argv[1:]
    as_text = "--text" in raw_argv

    try:
        config = parse_args(argv)
        result = run_search(config)
        payload = build_payload(result, config)
        if config.as_text:
            emit_text(result, config)
        else:
            print(json.dumps(payload, ensure_ascii=False))
        return 0
    except Exception as exc:
        if as_text:
            print(f"Error: {exc}", file=sys.stderr)
        else:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
