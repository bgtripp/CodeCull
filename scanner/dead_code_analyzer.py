"""
Dead Code Analyzer — statically identifies dead code branches created by
stale feature flags.

For each flag reference (``if is_enabled("flag-key")``), the analyzer
determines which branch is dead based on the flag's variation:

* **always-on**  -> the ``else`` branch is dead code
* **always-off** -> the ``if`` (true) branch is dead code

The result is a per-file diff preview showing exactly which lines would be
removed and what the cleaned-up code looks like.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DiffLine:
    """A single line in the dead-code preview diff."""

    line_number: int  # original line number (1-based)
    text: str  # line content (without trailing newline)
    kind: str  # "remove" | "keep" | "context"


@dataclass
class DeadCodeBlock:
    """A contiguous block of dead code around a single flag check."""

    file_path: str
    flag_key: str
    start_line: int  # first line of the if-block (1-based)
    end_line: int  # last line of the if/else block (1-based)
    diff_lines: list[DiffLine] = field(default_factory=list)
    dead_line_count: int = 0  # lines that would be removed


@dataclass
class FilePreview:
    """All dead-code blocks within a single file for a given flag."""

    file_path: str
    blocks: list[DeadCodeBlock] = field(default_factory=list)

    @property
    def total_dead_lines(self) -> int:
        return sum(b.dead_line_count for b in self.blocks)


@dataclass
class CleanupPreview:
    """Complete cleanup preview for a single flag across all files."""

    flag_key: str
    variation: str  # "always-on" | "always-off"
    file_previews: list[FilePreview] = field(default_factory=list)

    @property
    def total_dead_lines(self) -> int:
        return sum(fp.total_dead_lines for fp in self.file_previews)

    @property
    def total_files(self) -> int:
        return len(self.file_previews)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_FLAG_CHECK_RE = re.compile(
    r'^(\s*)if\s+is_enabled\(\s*["\']([a-z0-9\-]+)["\']\s*\)\s*:\s*$'
)

_ELSE_RE = re.compile(r"^(\s*)else\s*:\s*$")
_ELIF_RE = re.compile(r"^(\s*)elif\s+")


def _indent_level(line: str) -> int:
    """Return the number of leading spaces in *line*."""
    return len(line) - len(line.lstrip())


def _find_block_end(lines: list[str], start_idx: int, base_indent: int) -> int:
    """Return the index of the last line belonging to an indented block.

    *start_idx* is the index of the line **after** the ``if`` / ``else``
    statement.  Lines with indent > *base_indent* (or blank lines inside the
    block) belong to the block.
    """
    idx = start_idx
    last_content = start_idx - 1
    while idx < len(lines):
        line = lines[idx]
        # blank / whitespace-only lines are included if followed by more block
        if line.strip() == "":
            idx += 1
            continue
        if _indent_level(line) > base_indent:
            last_content = idx
            idx += 1
        else:
            break
    return last_content


def analyse_file(
    file_path: str,
    lines: list[str],
    flag_key: str,
    variation: str,
    context: int = 2,
) -> list[DeadCodeBlock]:
    """Parse *lines* for ``if is_enabled("{flag_key}")`` blocks.

    Returns a list of :class:`DeadCodeBlock` instances describing the dead
    code that would be removed.

    *variation* must be ``"always-on"`` or ``"always-off"``.
    *context* is the number of unchanged lines shown above/below each block.
    """
    blocks: list[DeadCodeBlock] = []

    idx = 0
    while idx < len(lines):
        m = _FLAG_CHECK_RE.match(lines[idx])
        if not m or m.group(2) != flag_key:
            idx += 1
            continue

        base_indent = _indent_level(lines[idx])
        if_line_idx = idx

        # Find the end of the if-body
        if_body_start = idx + 1
        if_body_end = _find_block_end(lines, if_body_start, base_indent)

        # Look for else clause
        else_line_idx = None
        else_body_start = None
        else_body_end = None

        next_after_if = if_body_end + 1
        if next_after_if < len(lines):
            stripped = lines[next_after_if]
            if _ELSE_RE.match(stripped):
                else_line_idx = next_after_if
                else_body_start = next_after_if + 1
                else_body_end = _find_block_end(lines, else_body_start, base_indent)
            elif _ELIF_RE.match(stripped):
                # elif — treat as complex; skip for now
                idx = next_after_if
                continue

        # Determine the full block range
        block_end_idx = else_body_end if else_body_end is not None else if_body_end
        block_start = if_line_idx
        block_end = block_end_idx

        # Build diff lines
        diff_lines: list[DiffLine] = []
        dead_count = 0

        # Context lines before
        ctx_start = max(0, block_start - context)
        for i in range(ctx_start, block_start):
            diff_lines.append(DiffLine(i + 1, lines[i], "context"))

        if variation == "always-on":
            # The if-branch is the LIVE code, else-branch is dead
            # Remove: the if line, else line, else body
            # Keep: the if body (dedented to base_indent)

            # Mark the if line as removed
            diff_lines.append(DiffLine(if_line_idx + 1, lines[if_line_idx], "remove"))
            dead_count += 1

            # The if body lines are kept but dedented
            for i in range(if_body_start, if_body_end + 1):
                line = lines[i]
                if line.strip() == "":
                    diff_lines.append(DiffLine(i + 1, line, "keep"))
                else:
                    # Dedent by one level (4 spaces or 1 tab)
                    dedented = _dedent_line(line, base_indent)
                    diff_lines.append(DiffLine(i + 1, dedented, "keep"))

            # Remove the else line and else body
            if else_line_idx is not None:
                diff_lines.append(DiffLine(else_line_idx + 1, lines[else_line_idx], "remove"))
                dead_count += 1
                for i in range(else_body_start, else_body_end + 1):  # type: ignore[arg-type]
                    diff_lines.append(DiffLine(i + 1, lines[i], "remove"))
                    dead_count += 1

        elif variation == "always-off":
            # The else-branch is the LIVE code, if-branch is dead
            # Remove: the if line, if body, else line
            # Keep: the else body (dedented)

            # Mark the if line as removed
            diff_lines.append(DiffLine(if_line_idx + 1, lines[if_line_idx], "remove"))
            dead_count += 1

            # Remove if body
            for i in range(if_body_start, if_body_end + 1):
                diff_lines.append(DiffLine(i + 1, lines[i], "remove"))
                dead_count += 1

            if else_line_idx is not None:
                # Remove the else line
                diff_lines.append(DiffLine(else_line_idx + 1, lines[else_line_idx], "remove"))
                dead_count += 1

                # Keep else body dedented
                for i in range(else_body_start, else_body_end + 1):  # type: ignore[arg-type]
                    line = lines[i]
                    if line.strip() == "":
                        diff_lines.append(DiffLine(i + 1, line, "keep"))
                    else:
                        dedented = _dedent_line(line, base_indent)
                        diff_lines.append(DiffLine(i + 1, dedented, "keep"))

        # Context lines after
        ctx_end = min(len(lines), block_end + 1 + context)
        for i in range(block_end + 1, ctx_end):
            diff_lines.append(DiffLine(i + 1, lines[i], "context"))

        blocks.append(
            DeadCodeBlock(
                file_path=file_path,
                flag_key=flag_key,
                start_line=block_start + 1,
                end_line=block_end + 1,
                diff_lines=diff_lines,
                dead_line_count=dead_count,
            )
        )

        idx = block_end + 1

    return blocks


def _dedent_line(line: str, base_indent: int) -> str:
    """Remove one indentation level (the if/else wrapper) from *line*.

    The wrapper's indentation is *base_indent* spaces.  Lines inside the
    block are indented at *base_indent* + N.  We strip one level (typically
    4 spaces) so they sit at *base_indent*.
    """
    current = _indent_level(line)
    # Determine the block's indentation (one level deeper than base)
    extra = current - base_indent
    if extra <= 0:
        return line
    # Find how many spaces the first body line is indented beyond base
    # We just strip (current - base_indent) leading spaces up to standard 4
    strip_amount = min(extra, 4)
    return " " * (current - strip_amount) + line.lstrip()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_cleanup_preview(
    repo_path: str,
    flag_key: str,
    variation: str,
    affected_files: list[str],
) -> CleanupPreview:
    """Generate a dead-code cleanup preview for *flag_key*.

    Reads each file in *affected_files* (relative to *repo_path*), parses
    ``if is_enabled(...)`` blocks, and returns a :class:`CleanupPreview`
    describing what would be removed.
    """
    preview = CleanupPreview(flag_key=flag_key, variation=variation)
    repo = Path(repo_path)

    for rel_path in affected_files:
        full = repo / rel_path
        if not full.is_file():
            continue

        try:
            source = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue

        lines = source.splitlines()
        blocks = analyse_file(rel_path, lines, flag_key, variation)
        if blocks:
            preview.file_previews.append(FilePreview(file_path=rel_path, blocks=blocks))

    return preview
