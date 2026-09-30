"""Pure snapshot comparison and bounded text rendering."""

import difflib
from collections.abc import Callable

from .limits import MAX_DIFF_FILE, MAX_DIFF_LINES, MAX_DIFF_OUTPUT


def changes(before: dict, after: dict) -> list[dict]:
    left = {f["depot"]: f for f in before["files"]}
    right = {f["depot"]: f for f in after["files"]}
    return [
        {
            "depot": depot,
            "path": (right.get(depot) or left[depot])["path"],
            "before": left.get(depot),
            "after": right.get(depot),
        }
        for depot in sorted(left.keys() | right.keys())
        if left.get(depot) != right.get(depot)
    ]


def metadata_changed(before: dict, after: dict) -> bool:
    return {k: v for k, v in before.items() if k != "files"} != {
        k: v for k, v in after.items() if k != "files"
    }


def render(before: dict, after: dict, read_blob: Callable[[str], bytes]) -> list[dict]:
    result, budget = [], MAX_DIFF_OUTPUT
    for delta in changes(before, after):
        a, b = delta["before"], delta["after"]
        item = {"path": delta["path"], "before": a, "after": b}
        if not budget:
            item.update(patch="", truncated=True)
        elif (a or {}).get("blob") == (b or {}).get("blob"):
            item["summary"] = "Perforce metadata changed; file contents are identical."
        elif any(entry and entry["size"] > MAX_DIFF_FILE for entry in (a, b)):
            item["summary"] = "Large file; compare blob IDs and sizes."
        else:
            try:
                payloads = [read_blob(e["blob"]) if e and e["blob"] else b"" for e in (a, b)]
                if any(b"\0" in data for data in payloads):
                    raise ValueError
                texts = [data.decode("utf-8").splitlines(keepends=True) for data in payloads]
                if sum(len(lines) for lines in texts) > MAX_DIFF_LINES:
                    raise ValueError
                pieces, truncated = [], False
                for line in difflib.unified_diff(
                    *texts, fromfile=f"a/{delta['path']}", tofile=f"b/{delta['path']}"
                ):
                    if not line.endswith("\n"):
                        line += "\n\\ No newline at end of file\n"
                    pieces.append(line[:budget])
                    if len(line) > budget:
                        budget, truncated = 0, True
                        break
                    budget -= len(line)
                item.update(patch="".join(pieces), truncated=truncated)
            except (UnicodeDecodeError, ValueError):
                item["summary"] = (
                    "Binary, non-UTF-8, or too many lines; compare blob IDs and sizes."
                )
        result.append(item)
    return result
