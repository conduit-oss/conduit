"""One plain sentence for people from Conduit's own finding text.

Findings are written for the model: locations, field lists and instructions.
The report's ``why`` is the short human version; the full text stays in
``error`` and is what retry prompts carry. Only Conduit's own message shapes
are parsed here; nothing names a library, method or type.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_FINDING = re.compile(r"(?P<path>[^\s;:]+\.py):(?P<line>\d+) (?P<expr>.*?): (?P<rest>.*)", re.S)
_SPLIT = re.compile(r"; (?=[^\s;:]+\.py:\d+ )")
_DEPENDS = re.compile(r"; depends on (?P<sites>.+?), whose fix was not applied")
_VERSION = r"(?P<pkg>[\w.\-]+) (?P<ver>[\w.\-+]+)"

_SHAPES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?P<callee>\w+)\(\) gets (?P<param>\w+) = (?P<types>[\w, ]+?) from (?P<src>\S+) in " + _VERSION + r", which has no attribute (?P<attr>\w+)"),
     "`{callee}()` reads `.{attr}`, which the `{types}` this line now passes doesn't have in {ver}"),
    (re.compile(r"(?P<name>[\w.]+) may be (?P<types>.+?) in " + _VERSION + r", which lack (?P<attr>\w+); the (?P<func>\w+)"),
     "{at} uses `{func}` to make up a value when `.{attr}` is missing; some `{name}` types in {ver} don't have it"),
    (re.compile(r"(?P<name>[\w.]+) is (?P<types>[\w, ]+?) in " + _VERSION + r", which always has (?P<attr>\w+); the (?P<func>\w+)"),
     "{at} guards `.{attr}` with `{func}`, but `{types}` always has it in {ver}; the fallback is dead code"),
    (re.compile(r"(?P<name>[\w.]+) is (?P<types>[\w, ]+?) in " + _VERSION + r", which has no attribute (?P<attr>\w+)"),
     "{at} reads `.{attr}`, which `{types}` doesn't have in {ver}"),
    (re.compile(r"(?P<callee>\w+) in this repo takes no keyword (?P<kws>[\w, ]+?) \("),
     "{at} calls `{callee}()` with keywords it doesn't take: {kws}"),
    (re.compile(r"(?P<callee>\w+) in this repo requires (?P<kws>[\w, ]+?), which"),
     "{at} calls `{callee}()` without its required {kws}"),
    (re.compile(r"(?P<callee>\w+) in this repo takes (?P<n>\d+) positional arguments"),
     "{at} passes `{callee}()` more positional arguments than it takes"),
    (re.compile(r"rewrite drops (?P<arg>\w+) which (?P<callee>[\w.]+) in " + _VERSION + r" still accepts"),
     "{at} drops `{arg}`, which `{callee}` still accepts in {ver}"),
    (re.compile(r"keyword (?P<arg>\w+) is not a"),
     "{at} passes `{arg}`, which the new call doesn't accept"),
    (re.compile(r"annotated (?P<ann>[\w, ]+?) but callers pass (?P<types>[\w, ]+?) from " + _VERSION),
     "{at} is annotated `{ann}`, but callers now pass `{types}` in {ver}"),
    (re.compile(r"name (?P<name>\w+) is no longer imported or defined after the rewrite at (?P<owner>\S+) removed it"),
     "{at} still uses `{name}`, which this rewrite stopped importing or defining"),
    (re.compile(r"(?P<rest>.+? (?:removed|renamed|changed)\b.*)"),
     "{at} still uses `{expr}` ({rest})"),
]


def plain_why(raw: str, site_path: str) -> str:
    """One sentence a person can read in a table cell. Never empty."""
    text = (raw or "").strip()
    if not text:
        return ""
    first = text.splitlines()[0]
    if first.startswith("reverted: the tests still fail"):
        return "Reverted: the tests failed with this rewrite."
    if first.startswith("rewrite repeats a decorator"):
        match = re.search(r"\((@[\w.]+) on (\w+)\)", first)
        return f"The rewrite added a second `{match.group(1)}`." if match else "The rewrite repeated a decorator."
    if first.startswith("its caller "):
        match = re.match(r"its caller (\S+) was not fixed", first)
        where = _short(match.group(1), site_path) if match else "its caller"
        return f"Its caller {where} was not fixed, so the new value never arrives."
    depends = _DEPENDS.search(first)
    body = first[: depends.start()] if depends else first
    body = body.split("rewrite still breaks against the new release: ", 1)[-1]
    parts = [part for part in _SPLIT.split(body) if part.strip()]
    sentence = _one(parts[0], site_path) if parts else ""
    if not sentence:
        sentence = _clean(body)
    if len(parts) > 1:
        sentence += f" (and {len(parts) - 1} more)"
    if depends:
        sites = ", ".join(_short(item.strip(), site_path) for item in depends.group("sites").split(","))
        sentence += f"; depends on {sites} (not fixed)"
    return sentence.rstrip(".") + "."


def _one(finding: str, site_path: str) -> str:
    match = _FINDING.match(finding.strip())
    if match is None:
        return ""
    at = "Line " + match.group("line") if _same(match.group("path"), site_path) else _short(
        f"{match.group('path')}:{match.group('line')}", site_path
    )
    rest = match.group("rest")
    for pattern, template in _SHAPES:
        found = pattern.search(rest)
        if found:
            values = {key: value for key, value in found.groupdict().items() if value is not None}
            values.setdefault("expr", match.group("expr"))
            values["types"] = _types(values.get("types", ""))
            if "rest" in values:
                values["rest"] = _clean(values["rest"])
            sentence = template.format(at=at, **values)
            return sentence[0].upper() + sentence[1:]
    return f"{at}: {_clean(rest)}"


def _types(text: str) -> str:
    names = [name.strip() for name in text.split(",") if name.strip()]
    if " and " in text:  # "A, B and 13 more"
        return text.strip()
    return names[0] if len(names) == 1 else " / ".join(names[:3]) + (" / ..." if len(names) > 3 else "")


def _clean(text: str) -> str:
    """Drop field lists and instructions; keep one short clause."""
    text = re.sub(r"\s*\((?:[\w, ]+ )?fields[^)]*\)", "", text)
    text = text.split("; ", 1)[0].strip()
    return text if len(text) <= 160 else text[:157].rstrip() + "..."


def _same(left: str, right: str) -> bool:
    a, b = left.replace("\\", "/").lstrip("./"), right.replace("\\", "/").lstrip("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _short(location: str, site_path: str) -> str:
    """``line N`` in the site's own file, else ``file.py:N``."""
    path, _, line = location.rpartition(":")
    if not path or not line.isdigit():
        return location
    if _same(path, site_path):
        return f"line {line}"
    return f"{PurePosixPath(path.replace(chr(92), '/')).name}:{line}"
