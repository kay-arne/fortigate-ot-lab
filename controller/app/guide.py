"""De lab guide (docs/lab-guide.md) als hoofdstukken voor het Lab guide-tabblad.

Eén bron: hetzelfde bestand dat op GitHub staat. Per `## `-kop een hoofdstuk;
wat vóór de eerste kop staat is de inleiding. Afbeeldingen (img/...) lopen via
de controller, zodat de gids ook werkt zonder internet."""
from __future__ import annotations

import re
from pathlib import Path

import markdown

from .config import ROOT

GUIDE_FILE = ROOT / "docs" / "lab-guide.md"
IMG_DIR = ROOT / "docs" / "img"
IMG_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".webp": "image/webp", ".gif": "image/gif", ".svg": "image/svg+xml"}

_cache: dict = {"mtime": None, "data": None}


def _split(text: str) -> list[tuple[str, str]]:
    """[(titel, markdown)] per `## `-kop, met de inleiding vooraan. Koppen in
    codeblokken tellen niet mee."""
    parts: list[tuple[str, list[str]]] = [("Inleiding", [])]
    fence = False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        if not fence and line.startswith("## "):
            parts.append((line[3:].strip(), [line]))
        else:
            parts[-1][1].append(line)
    return [(title, "\n".join(lines)) for title, lines in parts if "".join(lines).strip()]


_LIST_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])\s")


def _blank_before_lists(md: str) -> str:
    """GitHub toont een lijst die direct op een alinea volgt als lijst; Python-Markdown
    wil er een lege regel voor. Die voegen we toe (niet in codeblokken, niet binnen
    een lijst of citaat)."""
    out: list[str] = []
    fence = False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            fence = not fence
        elif not fence and _LIST_ITEM.match(line) and out:
            prev = out[-1]
            if prev.strip() and not _LIST_ITEM.match(prev) and not prev[0].isspace() and not prev.startswith(">"):
                out.append("")
        out.append(line)
    return "\n".join(out)


def _render(md: str) -> str:
    html = markdown.markdown(_blank_before_lists(md), extensions=["tables", "fenced_code", "sane_lists"])
    return re.sub(r'(<img[^>]*\ssrc=")img/', r"\1/api/guide/img/", html)


def load() -> dict:
    """{'chapters': [{'id', 'title', 'html'}]}; opnieuw ingelezen als het bestand wijzigt."""
    if not GUIDE_FILE.exists():
        return {"chapters": []}
    mtime = GUIDE_FILE.stat().st_mtime
    if _cache["mtime"] != mtime:
        chapters = [{"id": f"h{i}", "title": title, "html": _render(md)}
                    for i, (title, md) in enumerate(_split(GUIDE_FILE.read_text(encoding="utf-8")))]
        _cache.update(mtime=mtime, data={"chapters": chapters})
    return _cache["data"]


def image(name: str) -> tuple[Path, str] | None:
    """Pad en mediatype van een afbeelding uit docs/img, of None."""
    if "/" in name or "\\" in name or name.startswith("."):
        return None
    path = IMG_DIR / name
    kind = IMG_TYPES.get(path.suffix.lower())
    return (path, kind) if kind and path.is_file() else None
