#!/usr/bin/env python3
r"""
findtool.py

File-level pattern finder and pair matcher. All patterns use Python regex.

TEXT SEARCH
  -mr  PAT [PAT ...]        all matching line numbers for each pattern
  -n   PAT LINE             first match strictly after LINE  (0 = from top)
  -b   PAT LINE             last match strictly before LINE  (999999 = whole file)
  -e   PAT                  does any line match?

PAIR MATCHING
  -c   OPEN CLOSE LINE N    line of the Nth OPEN's closing partner  (scan forward)
  -o   OPEN CLOSE LINE N    line of the Nth CLOSE's opening partner (scan backward)

  -s   (with -c / -o)       skip strings and comments when tracking depth

OUTPUT  (JSON by default, --text for human-readable)
  success → stdout:
    {"matches": {"pat": [1, 5]}}   -mr
    {"line": 45}                   -n / -b / -c / -o
    {"matched": true}              -e
  failure → stderr, exit 1:
    {"ok": false, "error": "..."}

EXAMPLES
  python findtool.py --file app.py -mr "processOrder" "cancelOrder"
  python findtool.py --file app.py -n "def \w+" 0
  python findtool.py --file app.py -b "^import" 999999
  python findtool.py --file app.py -e "TODO"
  python findtool.py --file app.py -c "\{" "\}" 248 1
  python findtool.py --file app.py -c "\{" "\}" 248 1 -s
  python findtool.py --file index.html -c "<div\b[^>]*>" "</div>" 10 1
  python findtool.py --file app.py -o "\{" "\}" 298 1
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterator, List, Optional


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def json_print(payload: object, *, stream=sys.stdout) -> None:
    print(json.dumps(payload, ensure_ascii=False), file=stream)


def emit_success(result: object, as_text: bool, mode: str = "") -> None:
    if as_text:
        print(_format_text(mode, result))
        return
    json_print(result)


def emit_error(message: str, *, as_text: bool) -> None:
    if as_text:
        print(f"Error: {message}", file=sys.stderr)
        return
    json_print({"ok": False, "error": message}, stream=sys.stderr)


def _format_text(mode: str, result: object) -> str:
    if mode == "mr":
        return "; ".join(f"{p}: {lines}" for p, lines in result["matches"].items())
    if mode == "exists":
        return "true" if result["matched"] else "false"
    if mode in {"n", "b", "c", "o"}:
        return str(result["line"])
    return str(result)


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

def iter_lines(file_path: Optional[str]) -> Iterator[str]:
    """Yield lines (trailing newline stripped) from file or stdin."""
    if file_path is None:
        for line in sys.stdin:
            yield line.rstrip("\r\n")
        return
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line.rstrip("\r\n")


def read_text(file_path: Optional[str]) -> str:
    if file_path is None:
        return sys.stdin.read()
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise IsADirectoryError(f"Not a file: {file_path}")
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Regex helpers
# ---------------------------------------------------------------------------

def compile_re(pattern: str, ignore_case: bool) -> re.Pattern:
    if not pattern:
        raise ValueError("Pattern must not be empty.")
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regex pattern {pattern!r}: {exc}") from exc


def _dedupe(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# Text search
# ---------------------------------------------------------------------------

def search_many_lines(
    file_path: Optional[str], patterns: List[str], *, ignore_case: bool
) -> Dict[str, List[int]]:
    compiled = [(p, compile_re(p, ignore_case)) for p in _dedupe(patterns)]
    results: Dict[str, List[int]] = {p: [] for p, _ in compiled}
    for line_no, line in enumerate(iter_lines(file_path), start=1):
        for pat, rx in compiled:
            if rx.search(line):
                results[pat].append(line_no)
    return results


def find_next_line(
    file_path: Optional[str], pattern: str, after_line: int, *, ignore_case: bool
) -> int:
    if after_line < 0:
        raise ValueError("For -n, LINE must be >= 0.")
    rx = compile_re(pattern, ignore_case)
    last = 0
    for line_no, line in enumerate(iter_lines(file_path), start=1):
        last = line_no
        if line_no > after_line and rx.search(line):
            return line_no
    if after_line > last:
        raise ValueError(f"For -n, LINE must be between 0 and {last}.")
    raise ValueError(f"No line matching {pattern!r} found after line {after_line}.")


def find_prev_line(
    file_path: Optional[str], pattern: str, before_line: int, *, ignore_case: bool
) -> int:
    if before_line < 1:
        raise ValueError("For -b, LINE must be >= 1.")
    rx = compile_re(pattern, ignore_case)
    last_match: Optional[int] = None
    for line_no, line in enumerate(iter_lines(file_path), start=1):
        if line_no >= before_line:
            break
        if rx.search(line):
            last_match = line_no
    if last_match is None:
        raise ValueError(f"No line matching {pattern!r} found before line {before_line}.")
    return last_match


def exists_match(
    file_path: Optional[str], pattern: str, *, ignore_case: bool
) -> bool:
    rx = compile_re(pattern, ignore_case)
    for line in iter_lines(file_path):
        if rx.search(line):
            return True
    return False


# ---------------------------------------------------------------------------
# Smart masking — strip strings and comments before depth tracking
# ---------------------------------------------------------------------------

def mask_strings_and_comments(text: str) -> str:
    """Replace string / comment content with spaces, preserving newlines and length."""
    buf = list(text)
    n = len(text)
    i = 0

    NORMAL = 0; SL = 1; HC = 2; BC = 3; SQ = 4; DQ = 5; BQ = 6; TSQ = 7; TDQ = 8
    state = NORMAL

    def blank(a: int, b: int) -> None:
        for j in range(a, b):
            if buf[j] != "\n":
                buf[j] = " "

    while i < n:
        ch = text[i]
        n1 = text[i + 1] if i + 1 < n else ""
        n2 = text[i + 2] if i + 2 < n else ""

        if state == NORMAL:
            if ch == "/" and n1 == "/":
                state = SL; blank(i, i + 2); i += 2
            elif ch == "/" and n1 == "*":
                state = BC; blank(i, i + 2); i += 2
            elif ch == "#":
                state = HC; buf[i] = " "; i += 1
            elif ch == "'" and n1 == "'" and n2 == "'":
                state = TSQ; blank(i, i + 3); i += 3
            elif ch == '"' and n1 == '"' and n2 == '"':
                state = TDQ; blank(i, i + 3); i += 3
            elif ch == "'":
                state = SQ; buf[i] = " "; i += 1
            elif ch == '"':
                state = DQ; buf[i] = " "; i += 1
            elif ch == "`":
                state = BQ; buf[i] = " "; i += 1
            else:
                i += 1

        elif state in (SL, HC):
            if ch == "\n":
                state = NORMAL; i += 1
            else:
                buf[i] = " "; i += 1

        elif state == BC:
            if ch == "*" and n1 == "/":
                blank(i, i + 2); state = NORMAL; i += 2
            elif ch != "\n":
                buf[i] = " "; i += 1
            else:
                i += 1

        elif state in (SQ, DQ, BQ):
            q = ("'", '"', "`")[state - SQ]
            if ch == "\\" and i + 1 < n:
                if text[i + 1] != "\n":
                    blank(i, i + 2)
                i += 2
            elif ch == q:
                buf[i] = " "; state = NORMAL; i += 1
            elif ch != "\n":
                buf[i] = " "; i += 1
            else:
                i += 1

        elif state == TSQ:
            if ch == "'" and n1 == "'" and n2 == "'":
                blank(i, i + 3); state = NORMAL; i += 3
            elif ch != "\n":
                buf[i] = " "; i += 1
            else:
                i += 1

        elif state == TDQ:
            if ch == '"' and n1 == '"' and n2 == '"':
                blank(i, i + 3); state = NORMAL; i += 3
            elif ch != "\n":
                buf[i] = " "; i += 1
            else:
                i += 1

        else:
            i += 1

    return "".join(buf)


def _get_lines(text: str, smart: bool) -> List[str]:
    src = mask_strings_and_comments(text) if smart else text
    return src.splitlines()


def _ensure_line_exists(lines: List[str], line_no: int) -> None:
    if line_no < 1 or line_no > len(lines):
        raise ValueError(f"LINE must be between 1 and {len(lines)}.")


# ---------------------------------------------------------------------------
# Pair matching — depth tracking
# ---------------------------------------------------------------------------

def find_closing_line(
    lines: List[str],
    open_pat: str,
    close_pat: str,
    start_line: int,
    ordinal: int,
    *,
    ignore_case: bool,
) -> int:
    """Scan forward: find the line closing the Nth OPEN on start_line."""
    flags = re.IGNORECASE if ignore_case else 0
    open_re = re.compile(open_pat, flags)
    close_re = re.compile(close_pat, flags)

    anchor_line = lines[start_line - 1]
    opens_on_start = list(open_re.finditer(anchor_line))

    if ordinal < 1 or ordinal > len(opens_on_start):
        raise ValueError(
            f"Line {start_line} contains only {len(opens_on_start)} occurrence(s) "
            f"of open pattern {open_pat!r}, cannot get occurrence #{ordinal}."
        )

    after_anchor = anchor_line[opens_on_start[ordinal - 1].end():]
    depth = 1 + len(open_re.findall(after_anchor)) - len(close_re.findall(after_anchor))

    if depth <= 0:
        return start_line

    for line_no in range(start_line + 1, len(lines) + 1):
        line = lines[line_no - 1]
        depth += len(open_re.findall(line)) - len(close_re.findall(line))
        if depth <= 0:
            return line_no

    raise ValueError(
        f"No closing match for {open_pat!r} at line {start_line}, occurrence {ordinal}."
    )


def find_opening_line(
    lines: List[str],
    open_pat: str,
    close_pat: str,
    start_line: int,
    ordinal: int,
    *,
    ignore_case: bool,
) -> int:
    """Scan backward: find the line opening the Nth CLOSE on start_line."""
    flags = re.IGNORECASE if ignore_case else 0
    open_re = re.compile(open_pat, flags)
    close_re = re.compile(close_pat, flags)

    anchor_line = lines[start_line - 1]
    closes_on_start = list(close_re.finditer(anchor_line))

    if ordinal < 1 or ordinal > len(closes_on_start):
        raise ValueError(
            f"Line {start_line} contains only {len(closes_on_start)} occurrence(s) "
            f"of close pattern {close_pat!r}, cannot get occurrence #{ordinal}."
        )

    before_anchor = anchor_line[: closes_on_start[ordinal - 1].start()]
    depth = 1 + len(close_re.findall(before_anchor)) - len(open_re.findall(before_anchor))

    if depth <= 0:
        return start_line

    for line_no in range(start_line - 1, 0, -1):
        line = lines[line_no - 1]
        depth += len(close_re.findall(line)) - len(open_re.findall(line))
        if depth <= 0:
            return line_no

    raise ValueError(
        f"No opening match for {close_pat!r} at line {start_line}, occurrence {ordinal}."
    )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _parse_int(raw: str, name: str) -> int:
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer.")


def _parse_ordinal(raw: str) -> int:
    v = _parse_int(raw, "N")
    if v < 1:
        raise ValueError("N must be >= 1.")
    return v


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="File-level regex finder and pair matcher. JSON output by default."
    )
    p.add_argument("--file", "-p", help="Input file. Omit to read from stdin.")
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--text", action="store_true", help="Human-readable output.")
    p.add_argument(
        "-s",
        action="store_true",
        help="Smart mode for -c/-o: skip strings and comments when tracking depth.",
    )

    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("-mr", nargs="+", metavar="PAT",
                   help="All matching line numbers for each pattern.")
    g.add_argument("-n", nargs=2, metavar=("PAT", "LINE"),
                   help="First match strictly after LINE (0 = from top).")
    g.add_argument("-b", nargs=2, metavar=("PAT", "LINE"),
                   help="Last match strictly before LINE (999999 = whole file).")
    g.add_argument("-e", "--exists", metavar="PAT",
                   help="Does any line match?")
    g.add_argument("-c", nargs=4, metavar=("OPEN", "CLOSE", "LINE", "N"),
                   help="Line of closing match for Nth OPEN on LINE (scan forward).")
    g.add_argument("-o", nargs=4, metavar=("OPEN", "CLOSE", "LINE", "N"),
                   help="Line of opening match for Nth CLOSE on LINE (scan backward).")
    return p


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    as_text = args.text

    try:
        if args.mr is not None:
            matches = search_many_lines(args.file, args.mr, ignore_case=args.ignore_case)
            emit_success({"matches": matches}, as_text, mode="mr")

        elif args.n is not None:
            pat, line_s = args.n
            line = find_next_line(args.file, pat, _parse_int(line_s, "LINE"), ignore_case=args.ignore_case)
            emit_success({"line": line}, as_text, mode="n")

        elif args.b is not None:
            pat, line_s = args.b
            line = find_prev_line(args.file, pat, _parse_int(line_s, "LINE"), ignore_case=args.ignore_case)
            emit_success({"line": line}, as_text, mode="b")

        elif args.exists is not None:
            matched = exists_match(args.file, args.exists, ignore_case=args.ignore_case)
            emit_success({"matched": matched}, as_text, mode="exists")

        elif args.c is not None:
            open_pat, close_pat, line_s, ord_s = args.c
            line_no = _parse_int(line_s, "LINE")
            ordinal = _parse_ordinal(ord_s)
            text = read_text(args.file)
            lines = _get_lines(text, args.s)
            _ensure_line_exists(lines, line_no)
            result_line = find_closing_line(lines, open_pat, close_pat, line_no, ordinal, ignore_case=args.ignore_case)
            emit_success({"line": result_line}, as_text, mode="c")

        elif args.o is not None:
            open_pat, close_pat, line_s, ord_s = args.o
            line_no = _parse_int(line_s, "LINE")
            ordinal = _parse_ordinal(ord_s)
            text = read_text(args.file)
            lines = _get_lines(text, args.s)
            _ensure_line_exists(lines, line_no)
            result_line = find_opening_line(lines, open_pat, close_pat, line_no, ordinal, ignore_case=args.ignore_case)
            emit_success({"line": result_line}, as_text, mode="o")

        else:
            parser.error("No operation specified.")
            return 2

        return 0

    except Exception as exc:
        emit_error(str(exc), as_text=as_text)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
