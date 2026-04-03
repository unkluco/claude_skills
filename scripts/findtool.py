#!/usr/bin/env python3
r"""
findtool.py

A file-level command-line helper for agents and developers.

This tool focuses on one file (or stdin) and supports two families of work:

1) Text pattern search
   - find all matching line numbers for one or more patterns
   - find the next / previous matching line around a boundary
   - check whether a file contains a pattern at all

2) Delimiter matching
   - find the matching (), [], {} partner for a delimiter occurrence
   - ignore common comments and strings while scanning delimiters

WHY THIS VERSION EXISTS
-----------------------
This version is designed for agent/tool use first, with these changes:

- JSON is the default output format
- text output remains available with --text for human reading
- text-search modes stream line-by-line for lower memory usage
- literal fast path is supported for better performance on common queries
- a dedicated --exists / -e mode is available for fast "does this file match?"

ENGINES
-------
Text-search modes support three engines:

- auto    (default)
  Choose literal matching for simple patterns without regex metacharacters,
  otherwise choose Python regex.

- literal
  Treat patterns as plain text exactly as written.
  Useful for speed and for strings like "foo(bar)" without manual escaping.

- regex
  Treat patterns as Python regular expressions.

Delimiter modes (-c and -o) do not use the text-search engine at all.
They always use the dedicated delimiter scanner.

OUTPUT CONTRACT
---------------
By default this tool emits JSON to stdout on success.
On failure it emits JSON to stderr and exits with status 1.

Success shape:
    {
      "ok": true,
      "mode": "mr",
      "input": "/abs/or/relative/path/or/<stdin>",
      "result": {...}
    }

Failure shape:
    {
      "ok": false,
      "error": "...message..."
    }

For human-readable output, pass --text.

COMMON EXAMPLES
---------------
Find line numbers for many patterns (auto engine):
    python findtool.py --file DAO.java -mr "refreshAssemblyPanel" "build(Toa|Connector)"

Find many literal strings exactly as written:
    python findtool.py --file DAO.java --engine literal -mr "foo(bar)" "a.b"

Find the first match anywhere in the file:
    python findtool.py --file DAO.java -n "public\s+void\s+\w+\(" 0

Find the last match in the entire file:
    python findtool.py --file DAO.java -b "(get|set)\w+" 999999

Check whether any match exists at all:
    python findtool.py --file DAO.java -e "TODO"

Match the closing delimiter for the 3rd '{' on line 12:
    python findtool.py --file DAO.java -c "{" 12 3

Read from stdin:
    cat DAO.java | python findtool.py -e "TODO"

Switch to text output:
    python findtool.py --file DAO.java -e "TODO" --text

BOUNDARY RULES
--------------
- -n PATTERN LINE
  Search strictly after LINE.
  LINE may be 0, which means "start from the top of file".

- -b PATTERN LINE
  Search strictly before LINE.
  Values larger than the file length are silently clamped, so passing a
  large sentinel like 999999 reliably searches the whole file.

- -c / -o
  LINE must be a real 1-based line number in the file.

NOTES
-----
- Text search is line-based. A match must occur within a single line.
- Regex syntax follows Python's built-in re module, not PCRE/JS exactly.
- Literal and regex matching both support --ignore-case.
- This tool reads UTF-8 with replacement for undecodable bytes to be robust on source trees.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Pattern, Tuple

# Supported delimiter pairs for the structural scanner.
OPEN_TO_CLOSE = {"{": "}", "(": ")", "[": "]"}
CLOSE_TO_OPEN = {v: k for k, v in OPEN_TO_CLOSE.items()}

# Regex metacharacters used by the simple auto-engine heuristic.
# If any of these appear in a pattern, auto mode treats the pattern as regex.
REGEX_META_CHARS = set(".^$*+?{}[]\\|()")


@dataclass
class DelimiterOccurrence:
    """Information about one delimiter occurrence in a file."""

    char: str
    line: int
    column: int
    ordinal_on_line: int
    match_line: Optional[int] = None
    match_column: Optional[int] = None


@dataclass(frozen=True)
class PreparedPattern:
    """Prepared matcher for one pattern.

    Attributes:
        original: The exact user-provided pattern string.
        resolved_engine: Either "literal" or "regex" after auto resolution.
        matcher: A callable that returns True when the given line matches.
    """

    original: str
    resolved_engine: str
    matcher: Callable[[str], bool]


@dataclass(frozen=True)
class SearchContext:
    """Metadata included in success payloads."""

    mode: str
    input_name: str
    engine: Optional[str] = None
    resolved_engine: Optional[str] = None
    ignore_case: Optional[bool] = None


def json_print(payload: object, *, stream: object = sys.stdout) -> None:
    """Emit compact JSON with UTF-8 characters preserved."""
    print(json.dumps(payload, ensure_ascii=False), file=stream)


def emit_success(ctx: SearchContext, result: object, as_text: bool) -> None:
    """Emit success output in either JSON or human-readable text."""
    if as_text:
        print(format_text_success(ctx.mode, result))
        return

    payload = {
        "ok": True,
        "mode": ctx.mode,
        "result": result,
    }
    if ctx.engine is not None:
        payload["engine"] = ctx.resolved_engine
    if ctx.ignore_case is not None:
        payload["ignore_case"] = ctx.ignore_case
    json_print(payload)


def emit_error(message: str, *, as_text: bool) -> None:
    """Emit failure output in either JSON or human-readable text."""
    if as_text:
        print(f"Error: {message}", file=sys.stderr)
        return
    json_print({"ok": False, "error": message}, stream=sys.stderr)


def format_text_success(mode: str, result: object) -> str:
    """Human-friendly output retained for local terminal use."""
    if mode == "mr":
        matches = result["matches"]
        return "; ".join(f"{pattern}: {lines}" for pattern, lines in matches.items())
    if mode == "exists":
        return "true" if result["matched"] else "false"
    if mode in {"n", "b", "c", "o"}:
        return str(result["line"])
    return str(result)


def require_non_empty_pattern(pattern: str) -> None:
    """Reject empty patterns early with a clear message."""
    if pattern == "":
        raise ValueError("Pattern must not be empty.")


def parse_regex(pattern: str, *, ignore_case: bool) -> Pattern[str]:
    """Compile one regex pattern using Python re with optional IGNORECASE."""
    require_non_empty_pattern(pattern)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern {pattern!r}: {exc}") from exc


def dedupe_preserve_order(items: List[str]) -> List[str]:
    """Remove duplicate patterns while preserving the original order."""
    seen = set()
    result: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def normalize_line(raw_line: str) -> str:
    """Remove only trailing line terminators to match splitlines() semantics.

    We intentionally keep all other leading/trailing spaces intact because they
    are part of the searchable content.
    """
    return raw_line.rstrip("\r\n")


def iter_normalized_lines(file_path: Optional[str]) -> Iterator[str]:
    """Yield normalized lines from a file path or stdin.

    This is the fast path for text-search modes because it avoids reading the
    full file into memory.
    """
    if file_path is None:
        for raw_line in sys.stdin:
            yield normalize_line(raw_line)
        return

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw_line in handle:
            yield normalize_line(raw_line)


def read_text(file_path: Optional[str]) -> str:
    """Read full input as text.

    Only delimiter modes need the full text because they scan across the entire
    file while tracking comments and string state.
    """
    if file_path is None:
        return sys.stdin.read()

    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")
    return path.read_text(encoding="utf-8", errors="replace")


def is_probably_literal(pattern: str) -> bool:
    """Heuristic for auto engine.

    Auto mode treats a pattern as literal when it contains no obvious regex
    metacharacters. This keeps common exact-string searches fast without asking
    the caller to pass --engine literal explicitly.
    """
    require_non_empty_pattern(pattern)
    return not any(ch in REGEX_META_CHARS for ch in pattern)


def prepare_pattern(pattern: str, *, engine: str, ignore_case: bool) -> PreparedPattern:
    """Resolve one pattern into a concrete matcher.

    In auto mode, simple strings go to the literal fast path and anything that
    looks regex-like goes through Python re.
    """
    require_non_empty_pattern(pattern)

    resolved_engine = engine
    if engine == "auto":
        resolved_engine = "literal" if is_probably_literal(pattern) else "regex"

    if resolved_engine == "literal":
        needle = pattern.casefold() if ignore_case else pattern

        def matcher(line: str) -> bool:
            haystack = line.casefold() if ignore_case else line
            return needle in haystack

        return PreparedPattern(original=pattern, resolved_engine="literal", matcher=matcher)

    compiled = parse_regex(pattern, ignore_case=ignore_case)

    def regex_matcher(line: str) -> bool:
        return compiled.search(line) is not None

    return PreparedPattern(original=pattern, resolved_engine="regex", matcher=regex_matcher)


def prepare_patterns(patterns: List[str], *, engine: str, ignore_case: bool) -> List[PreparedPattern]:
    """Prepare many patterns once before streaming the file."""
    return [prepare_pattern(pattern, engine=engine, ignore_case=ignore_case) for pattern in dedupe_preserve_order(patterns)]


def compute_resolved_engine(patterns: List[str], engine: str) -> str:
    """Return the effective engine string for JSON output.

    For explicit engines, return as-is.
    For auto, resolve each pattern and return the common engine,
    or "auto" if patterns would resolve differently.
    """
    if engine != "auto":
        return engine
    resolved = {("literal" if is_probably_literal(p) else "regex") for p in patterns if p}
    return resolved.pop() if len(resolved) == 1 else "auto"


def search_many_lines(file_path: Optional[str], patterns: List[str], *, engine: str, ignore_case: bool) -> Dict[str, List[int]]:
    """Find all matching line numbers for many patterns in one streaming pass."""
    prepared = prepare_patterns(patterns, engine=engine, ignore_case=ignore_case)
    results: Dict[str, List[int]] = {item.original: [] for item in prepared}

    for line_no, line in enumerate(iter_normalized_lines(file_path), start=1):
        for item in prepared:
            if item.matcher(line):
                results[item.original].append(line_no)

    return results


def find_next_line(file_path: Optional[str], pattern: str, after_line: int, *, engine: str, ignore_case: bool) -> int:
    """Return the first matching line strictly after the given boundary."""
    if after_line < 0:
        raise ValueError("For -n, LINE must be >= 0.")

    prepared = prepare_pattern(pattern, engine=engine, ignore_case=ignore_case)
    line_count = 0

    for line_count, line in enumerate(iter_normalized_lines(file_path), start=1):
        if line_count > after_line and prepared.matcher(line):
            return line_count

    if after_line > line_count:
        raise ValueError(f"For -n, LINE must be between 0 and {line_count}.")

    raise ValueError(f"No line matching pattern {pattern!r} was found after line {after_line}.")


def find_prev_line(file_path: Optional[str], pattern: str, before_line: int, *, engine: str, ignore_case: bool) -> int:
    """Return the last matching line strictly before the given boundary.

    before_line values larger than the file length are silently clamped, so
    passing a large sentinel like 999999 reliably means "search the whole file".
    """
    if before_line < 1:
        raise ValueError("For -b, LINE must be >= 1.")

    prepared = prepare_pattern(pattern, engine=engine, ignore_case=ignore_case)
    last_match: Optional[int] = None

    for line_no, line in enumerate(iter_normalized_lines(file_path), start=1):
        if line_no < before_line and prepared.matcher(line):
            last_match = line_no

    if last_match is None:
        raise ValueError(f"No line matching pattern {pattern!r} was found before line {before_line}.")

    return last_match


def exists_match(file_path: Optional[str], pattern: str, *, engine: str, ignore_case: bool) -> bool:
    """Return True as soon as a matching line is found."""
    prepared = prepare_pattern(pattern, engine=engine, ignore_case=ignore_case)
    for line in iter_normalized_lines(file_path):
        if prepared.matcher(line):
            return True
    return False


def split_lines_full(text: str) -> List[str]:
    """Split full text into logical lines for delimiter modes."""
    return text.splitlines()


def ensure_line_exists(lines: List[str], line_no: int) -> None:
    """Validate that LINE refers to a real 1-based line in the file."""
    if line_no < 1 or line_no > len(lines):
        raise ValueError(f"LINE must be between 1 and {len(lines)}.")


def scan_delimiters(text: str) -> Dict[Tuple[int, str], List[DelimiterOccurrence]]:
    """Collect (), [], {} outside common comments/strings.

    Ignored regions:
    - // line comments
    - # line comments
    - /* ... */ block comments
    - single, double and backtick strings
    - triple single/double quoted strings
    """
    occurrences: Dict[Tuple[int, str], List[DelimiterOccurrence]] = {}
    stack: List[DelimiterOccurrence] = []

    i = 0
    line = 1
    col = 1
    n = len(text)

    NORMAL = "normal"
    SLASH_COMMENT = "slash_comment"
    HASH_COMMENT = "hash_comment"
    BLOCK_COMMENT = "block_comment"
    SQ = "sq"
    DQ = "dq"
    BQ = "bq"
    TSQ = "tsq"
    TDQ = "tdq"
    state = NORMAL

    def add_occurrence(ch: str, line_no: int, col_no: int) -> DelimiterOccurrence:
        key = (line_no, ch)
        ord_no = len(occurrences.get(key, [])) + 1
        occ = DelimiterOccurrence(char=ch, line=line_no, column=col_no, ordinal_on_line=ord_no)
        occurrences.setdefault(key, []).append(occ)
        return occ

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        nxt2 = text[i + 2] if i + 2 < n else ""

        if state == NORMAL:
            if ch == "/" and nxt == "/":
                state = SLASH_COMMENT
                i += 2
                col += 2
                continue
            if ch == "/" and nxt == "*":
                state = BLOCK_COMMENT
                i += 2
                col += 2
                continue
            if ch == "#":
                state = HASH_COMMENT
                i += 1
                col += 1
                continue
            if ch == "'" and nxt == "'" and nxt2 == "'":
                state = TSQ
                i += 3
                col += 3
                continue
            if ch == '"' and nxt == '"' and nxt2 == '"':
                state = TDQ
                i += 3
                col += 3
                continue
            if ch == "'":
                state = SQ
                i += 1
                col += 1
                continue
            if ch == '"':
                state = DQ
                i += 1
                col += 1
                continue
            if ch == "`":
                state = BQ
                i += 1
                col += 1
                continue

            if ch in OPEN_TO_CLOSE:
                occ = add_occurrence(ch, line, col)
                stack.append(occ)
            elif ch in CLOSE_TO_OPEN:
                occ = add_occurrence(ch, line, col)
                expected_open = CLOSE_TO_OPEN[ch]
                if stack and stack[-1].char == expected_open:
                    open_occ = stack.pop()
                    open_occ.match_line = occ.line
                    open_occ.match_column = occ.column
                    occ.match_line = open_occ.line
                    occ.match_column = open_occ.column

            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue

        if state in (SLASH_COMMENT, HASH_COMMENT):
            if ch == "\n":
                state = NORMAL
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue

        if state == BLOCK_COMMENT:
            if ch == "*" and nxt == "/":
                state = NORMAL
                i += 2
                col += 2
                continue
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue

        if state in (SQ, DQ, BQ):
            quote = {SQ: "'", DQ: '"', BQ: "`"}[state]
            if ch == "\\" and i + 1 < n:
                if text[i + 1] == "\n":
                    line += 1
                    col = 1
                else:
                    col += 2
                i += 2
                continue
            if ch == quote:
                state = NORMAL
                i += 1
                col += 1
                continue
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue

        if state == TSQ:
            if ch == "'" and nxt == "'" and nxt2 == "'":
                state = NORMAL
                i += 3
                col += 3
                continue
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue

        if state == TDQ:
            if ch == '"' and nxt == '"' and nxt2 == '"':
                state = NORMAL
                i += 3
                col += 3
                continue
            if ch == "\n":
                line += 1
                col = 1
            else:
                col += 1
            i += 1
            continue

    return occurrences


def get_nth_occurrence_on_line(
    occurrences: Dict[Tuple[int, str], List[DelimiterOccurrence]],
    token: str,
    line_no: int,
    ordinal: int,
) -> DelimiterOccurrence:
    """Get the Nth valid occurrence of one delimiter token on a line."""
    line_items = occurrences.get((line_no, token), [])
    if ordinal < 1:
        raise ValueError("Occurrence index must be >= 1.")
    if ordinal > len(line_items):
        raise ValueError(
            f"Line {line_no} contains only {len(line_items)} valid occurrence(s) of {token!r}, "
            f"cannot get occurrence #{ordinal}."
        )
    return line_items[ordinal - 1]


def match_closing_line(text: str, opener: str, line_no: int, ordinal: int) -> int:
    """Return the line containing the matching closing delimiter."""
    if opener not in OPEN_TO_CLOSE:
        raise ValueError(f"{opener!r} is not a supported opening delimiter. Use one of {sorted(OPEN_TO_CLOSE)}.")
    occurrences = scan_delimiters(text)
    occ = get_nth_occurrence_on_line(occurrences, opener, line_no, ordinal)
    if occ.match_line is None:
        raise ValueError(
            f"The {ordinal} occurrence of {opener!r} on line {line_no} does not have a valid matching "
            f"{OPEN_TO_CLOSE[opener]!r}."
        )
    return occ.match_line


def match_opening_line(text: str, closer: str, line_no: int, ordinal: int) -> int:
    """Return the line containing the matching opening delimiter."""
    if closer not in CLOSE_TO_OPEN:
        raise ValueError(f"{closer!r} is not a supported closing delimiter. Use one of {sorted(CLOSE_TO_OPEN)}.")
    occurrences = scan_delimiters(text)
    occ = get_nth_occurrence_on_line(occurrences, closer, line_no, ordinal)
    if occ.match_line is None:
        raise ValueError(
            f"The {ordinal} occurrence of {closer!r} on line {line_no} does not have a valid matching "
            f"{CLOSE_TO_OPEN[closer]!r}."
        )
    return occ.match_line


def parse_int_arg(raw: str, arg_name: str) -> int:
    """Parse a required integer argument with a stable error message."""
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{arg_name} must be an integer.") from exc


def parse_positive_occurrence(raw: str) -> int:
    """Parse N for delimiter queries and require N >= 1."""
    value = parse_int_arg(raw, "N")
    if value < 1:
        raise ValueError("N must be >= 1.")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Create the CLI parser.

    The parser is intentionally explicit because this tool is often invoked by
    another agent/tool rather than typed manually by a human.
    """
    parser = argparse.ArgumentParser(
        description=(
            "File-level pattern finder and delimiter matcher. "
            "JSON is emitted by default. Use --text for plain output."
        )
    )
    parser.add_argument(
        "--file",
        "-p",
        help="Path to the input file. If omitted, read from stdin.",
    )
    parser.add_argument(
        "--engine",
        choices=["auto", "literal", "regex"],
        default="auto",
        help=(
            "Text-search engine selection. "
            "auto picks literal for simple patterns and regex otherwise."
        ),
    )
    parser.add_argument(
        "--ignore-case",
        action="store_true",
        help="Apply case-insensitive matching to text-search modes.",
    )
    parser.add_argument(
        "--text",
        action="store_true",
        help="Emit human-readable text instead of JSON.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Accepted for compatibility. JSON is already the default.",
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-mr",
        nargs="+",
        metavar="PATTERN",
        help="Return all matching line numbers for one or more patterns.",
    )
    group.add_argument(
        "-n",
        nargs=2,
        metavar=("PATTERN", "LINE"),
        help="Return the first line matching PATTERN strictly after LINE. LINE may be 0.",
    )
    group.add_argument(
        "-b",
        nargs=2,
        metavar=("PATTERN", "LINE"),
        help="Return the last line matching PATTERN strictly before LINE. Large values are clamped to EOF.",
    )
    group.add_argument(
        "-e",
        "--exists",
        metavar="PATTERN",
        help="Return whether any line in the file matches PATTERN.",
    )
    group.add_argument(
        "-c",
        nargs=3,
        metavar=("OPEN", "LINE", "N"),
        help="Return the line number of the matching closing delimiter for the Nth OPEN on LINE.",
    )
    group.add_argument(
        "-o",
        nargs=3,
        metavar=("CLOSE", "LINE", "N"),
        help="Return the line number of the matching opening delimiter for the Nth CLOSE on LINE.",
    )
    return parser


def input_name(file_path: Optional[str]) -> str:
    """Return the user-facing input identifier for output payloads."""
    return file_path if file_path is not None else "<stdin>"


def main() -> int:
    """CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args()
    as_text = args.text

    try:
        if args.mr is not None:
            result = {
                "matches": search_many_lines(
                    args.file,
                    args.mr,
                    engine=args.engine,
                    ignore_case=args.ignore_case,
                )
            }
            emit_success(
                SearchContext(mode="mr", input_name=input_name(args.file), engine=args.engine,
                              resolved_engine=compute_resolved_engine(args.mr, args.engine),
                              ignore_case=args.ignore_case),
                result,
                as_text,
            )
            return 0

        if args.n is not None:
            pattern, line_s = args.n
            line_no = parse_int_arg(line_s, "LINE")
            result = {
                "line": find_next_line(
                    args.file,
                    pattern,
                    line_no,
                    engine=args.engine,
                    ignore_case=args.ignore_case,
                )
            }
            emit_success(
                SearchContext(mode="n", input_name=input_name(args.file), engine=args.engine,
                              resolved_engine=compute_resolved_engine([pattern], args.engine),
                              ignore_case=args.ignore_case),
                result,
                as_text,
            )
            return 0

        if args.b is not None:
            pattern, line_s = args.b
            line_no = parse_int_arg(line_s, "LINE")
            result = {
                "line": find_prev_line(
                    args.file,
                    pattern,
                    line_no,
                    engine=args.engine,
                    ignore_case=args.ignore_case,
                )
            }
            emit_success(
                SearchContext(mode="b", input_name=input_name(args.file), engine=args.engine,
                              resolved_engine=compute_resolved_engine([pattern], args.engine),
                              ignore_case=args.ignore_case),
                result,
                as_text,
            )
            return 0

        if args.exists is not None:
            result = {
                "matched": exists_match(
                    args.file,
                    args.exists,
                    engine=args.engine,
                    ignore_case=args.ignore_case,
                )
            }
            emit_success(
                SearchContext(mode="exists", input_name=input_name(args.file), engine=args.engine,
                              resolved_engine=compute_resolved_engine([args.exists], args.engine),
                              ignore_case=args.ignore_case),
                result,
                as_text,
            )
            return 0

        if args.c is not None:
            opener, line_s, ord_s = args.c
            line_no = parse_int_arg(line_s, "LINE")
            ordinal = parse_positive_occurrence(ord_s)
            text = read_text(args.file)
            lines = split_lines_full(text)
            ensure_line_exists(lines, line_no)
            result = {"line": match_closing_line(text, opener, line_no, ordinal)}
            emit_success(SearchContext(mode="c", input_name=input_name(args.file)), result, as_text)
            return 0

        if args.o is not None:
            closer, line_s, ord_s = args.o
            line_no = parse_int_arg(line_s, "LINE")
            ordinal = parse_positive_occurrence(ord_s)
            text = read_text(args.file)
            lines = split_lines_full(text)
            ensure_line_exists(lines, line_no)
            result = {"line": match_opening_line(text, closer, line_no, ordinal)}
            emit_success(SearchContext(mode="o", input_name=input_name(args.file)), result, as_text)
            return 0

        parser.error("No operation specified.")
        return 2

    except Exception as exc:
        emit_error(str(exc), as_text=as_text)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
