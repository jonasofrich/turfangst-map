#!/usr/bin/env python3
"""Marks every node in a label file with turfangst_outside=yes, so the schema can tell
labels from outside Norway and Sweden from those in the extracts. Goes through osmium's
text format, because osmium has no command that adds a tag.

    tag_outside_labels.py IN.osm.pbf OUT.osm.pbf
"""
import re
import subprocess
import sys

TAG = "turfangst_outside=yes"
TAGS_FIELD = re.compile(r" T(\S*)")


def tag_line(line: str) -> str:
    """Adds the tag to one line of OPL. Commas inside values are escaped by OPL, so a
    tag list is safe to extend, and an empty list has nothing to separate."""
    match = TAGS_FIELD.search(line)
    if match is None:
        raise ValueError(f"no tags field: {line[:80]}")
    tags = f"{match[1]},{TAG}" if match[1] else TAG
    return f"{line[:match.start()]} T{tags}{line[match.end():]}"


def main(source: str, target: str) -> int:
    text = subprocess.run(["osmium", "cat", "-f", "opl", source], check=True, capture_output=True, text=True).stdout
    tagged = "".join(tag_line(line) + "\n" for line in text.splitlines())
    subprocess.run([
            "osmium", "cat", "--overwrite",
            "--clean", "version", "--clean", "changeset", "--clean", "timestamp",
            "--clean", "uid", "--clean", "user",
            "-F", "opl", "-o", target,
        ], input=tagged, check=True, text=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
