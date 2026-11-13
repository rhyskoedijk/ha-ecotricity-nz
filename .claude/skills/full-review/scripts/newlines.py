"""List non-empty text files that don't end with a newline, sorted.

Stands in for pre-commit's end-of-file-fixer, which rewrites files instead of reporting them.
"""

from __future__ import annotations

from _repo import read_text, repo_files


def main() -> None:
    """Print each text file missing its final newline."""
    missing = [
        path.as_posix()
        for path in repo_files()
        if (text := read_text(path)) and not text.endswith("\n")
    ]
    print(f"## NO FINAL NEWLINE ({len(missing)})")
    print("\n".join(missing) or "none")


if __name__ == "__main__":
    main()
