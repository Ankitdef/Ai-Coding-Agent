import re
from pathlib import Path

MENTION_PATTERN = re.compile(r"@([A-Za-z0-9_\-./\\]+)")

MAX_FILE_CHARS = 30_000
MAX_DIR_ENTRIES = 50


def is_only_mentions(message: str) -> bool:
    """True if the message contains @mentions but no actual question/text."""
    if not MENTION_PATTERN.search(message):
        return False

    remaining = MENTION_PATTERN.sub("", message)
    return not remaining.strip(" \t\n.,;:?!")


def expand_mentions(message: str, cwd: Path) -> str:
    """Expand @path mentions by attaching file contents or folder listings.

    Mentions that do not resolve to an existing file or folder are left
    untouched (so emails like a@b.com are ignored).
    """
    blocks: list[str] = []
    seen: set[str] = set()

    for match in MENTION_PATTERN.finditer(message):
        raw = match.group(1).rstrip(".,;:")

        if not raw or raw in seen:
            continue
        seen.add(raw)

        path = Path(raw)
        if not path.is_absolute():
            path = cwd / raw

        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            truncated = len(content) > MAX_FILE_CHARS
            if truncated:
                content = content[:MAX_FILE_CHARS]

            block = f'Attached file @{raw}:\n"""\n{content}\n"""'
            if truncated:
                block += "\n[file truncated - use read_file with offset to see more]"
            blocks.append(block)
        elif path.is_dir():
            try:
                entries = sorted(
                    entry.name + ("/" if entry.is_dir() else "")
                    for entry in path.iterdir()
                    if not entry.name.startswith(".")
                )
            except OSError:
                continue

            listing = "\n".join(entries[:MAX_DIR_ENTRIES])
            if len(entries) > MAX_DIR_ENTRIES:
                listing += f"\n... +{len(entries) - MAX_DIR_ENTRIES} more entries"
            blocks.append(f"Attached folder @{raw} contents:\n{listing}")

    if not blocks:
        return message

    return message + "\n\n" + "\n\n".join(blocks)
