#!/usr/bin/env python3
"""Gives every way that is a direct member of a nordic ski route relation the tags
piste:type=nordic and, when the relation has it, piste:grooming. Planetiler reads tags on
ways only, and about 30% of the Swedish nordic ways carry them only on the relation. Goes
through osmium's text format (OPL), because osmium has no command that adds a tag.

    tag_ski_routes.py IN.osm.pbf OUT.osm.pbf

Every other object comes out as it went in, and no node is added or removed.
"""
from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

NORDIC = re.compile(r"(^|;) *nordic *(;|$)")
TAGS_FIELD = re.compile(r" T(\S*)")
ESCAPE = re.compile(r"%([0-9a-fA-F]+)%")
TYPE_KEY = "piste:type"
GROOMING_KEY = "piste:grooming"


def is_nordic(value: str) -> bool:
    """True when a piste:type value lists nordic, like the schema's own test does."""
    return NORDIC.search(value) is not None


def unescape(text: str) -> str:
    """OPL writes a space, a comma, an equals sign and the like as %hex%."""
    return ESCAPE.sub(lambda m: chr(int(m[1], 16)), text)


SAFE = set(":;+-_.")


def escape(text: str) -> str:
    """The reverse of unescape: letters, digits and :;+-_. stay, the rest is written %hex%
    (a space, a comma, an equals sign and the like would break the OPL line)."""
    return "".join(c if c.isalnum() or c in SAFE else f"%{ord(c):x}%" for c in text)


def enough_tagged(tagged: int, requested: int) -> bool:
    """getid also exits 1 on data errors, so too few ways out is the only sign of a bad run."""
    return tagged >= 0.9 * requested


def parse_tags(field: str) -> dict[str, str]:
    tags = {}
    for pair in field.split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            tags[unescape(key)] = unescape(value)
    return tags


def rank(grooming: str | None) -> int:
    """Prepared tracks beat backcountry, and anything beats nothing."""
    if grooming is None:
        return 0
    return 1 if grooming == "backcountry" else 2


def members_of(relation_lines: Iterable[str]) -> dict[str, str | None]:
    """Way id (without the w) to the best grooming of the nordic relations it belongs to,
    or None when none of them says. Areas are not routes and are skipped."""
    found: dict[str, str | None] = {}
    for line in relation_lines:
        fields = line.split(" ")
        tags = {}
        members = ""
        for field in fields[1:]:
            if field.startswith("T"):
                tags = parse_tags(field[1:])
            elif field.startswith("M"):
                members = field[1:]
        if tags.get("type") == "multipolygon" or not is_nordic(tags.get(TYPE_KEY, "")):
            continue
        grooming = tags.get(GROOMING_KEY)
        for member in members.split(","):
            if member.startswith("w"):
                way = member[1:].split("@", 1)[0]
                if way not in found or rank(grooming) > rank(found[way]):
                    found[way] = grooming
    return found


def tag_way_line(line: str, grooming: str | None) -> str:
    """Adds nordic to one way's OPL line, and the grooming unless the way has its own."""
    match = TAGS_FIELD.search(line)
    if match is None:
        raise ValueError(f"no tags field: {line[:80]}")
    pairs = [pair for pair in match[1].split(",") if pair]
    tags = parse_tags(match[1])
    if TYPE_KEY not in tags:
        pairs.append(f"{TYPE_KEY}=nordic")
    elif not is_nordic(tags[TYPE_KEY]):
        pairs = [f"{pair};nordic" if unescape(pair.split("=", 1)[0]) == TYPE_KEY else pair for pair in pairs]
    if grooming is not None and GROOMING_KEY not in tags:
        pairs.append(f"{GROOMING_KEY}={escape(grooming)}")
    return f"{line[:match.start()]} T{','.join(pairs)}{line[match.end():]}"


def osmium(*args: str, ok: tuple[int, ...] = (0,), **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(["osmium", *args], text=True, encoding="utf-8", **kwargs)
    if result.returncode not in ok:
        raise RuntimeError(f"osmium {args[0]} failed with {result.returncode}")
    return result


def main(source: str, target: str) -> int:
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        osmium("tags-filter", "-R", "-O", "-o", str(work / "relations.osm.pbf"), source, "r/piste:type")
        opl = osmium("cat", "-f", "opl", str(work / "relations.osm.pbf"), capture_output=True).stdout
        members = members_of(opl.splitlines())
        if not members:
            print("no nordic ski relations", file=sys.stderr)
            return 1

        ids = work / "ways.txt"
        ids.write_text("".join(f"w{way}\n" for way in members))
        # getid exits 1 when an id is missing, as some are in an extract cut at a border.
        osmium("getid", "-O", "--id-file", str(ids), "-o", str(work / "ways.osm.pbf"), source, ok=(0, 1))
        ways = osmium("cat", "-f", "opl", str(work / "ways.osm.pbf"), capture_output=True).stdout
        if not enough_tagged(len(ways.splitlines()), len(members)):
            print(f"only {len(ways.splitlines())} of {len(members)} member ways found", file=sys.stderr)
            return 1
        tagged = "".join(tag_way_line(line, members[line.split(" ", 1)[0][1:]]) + "\n" for line in ways.splitlines())
        osmium("cat", "-O", "-F", "opl", "-o", str(work / "ways-tagged.osm.pbf"), input=tagged)

        osmium("removeid", "-O", "--id-file", str(ids), "-o", str(work / "rest.osm.pbf"), source)
        osmium("merge", "-O", "-o", target, str(work / "rest.osm.pbf"), str(work / "ways-tagged.osm.pbf"))
    print(f"tagged {len(ways.splitlines())} ways from {len(members)} members of nordic ski relations")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
