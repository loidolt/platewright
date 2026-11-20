"""Matching model names, refs, aliases and slicer object names."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

EXT_RE = re.compile(r"\.(3mf|stl|step|stp|obj)(?=[^a-z0-9]|$)", re.I)
COPY_RE = re.compile(r"[ _-]+\d+$")  # the slicer numbers copies: 'gecko cut.stl_A 7 3'


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def norm_key(text: str) -> str:
    """Key for matching names, refs, aliases and slicer object names: '091' == '91'."""
    s = slugify(text)
    return str(int(s)) if s.isdigit() else s


def object_key(name: str) -> str:
    return norm_key(EXT_RE.sub("", (name or "").strip()))


def base_object_name(name: str) -> str:
    """'gecko cut.stl_A 7 3' -> 'gecko cut.stl_A'."""
    n = (name or "").strip()
    while COPY_RE.search(n):
        n = COPY_RE.sub("", n, count=1)
    return n or (name or "").strip()


def model_slug(name: str, ref: str | None = None) -> str:
    s = slugify(name)
    if not s:
        raise ValueError("a model name must contain letters or numbers")
    if not ref:
        return s
    r = f"{int(ref):03d}" if ref.isdigit() else slugify(ref)
    return f"{r}-{s}" if r else s


@dataclass(frozen=True)
class ModelNames:
    """What a model can be called: its slug, name, catalog ref and slicer aliases."""

    slug: str
    name: str = ""
    ref: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    def keys(self) -> set[str]:
        keys = {norm_key(self.slug), norm_key(self.name)}
        if self.ref:
            keys.add(norm_key(self.ref))
        keys.update(object_key(a) for a in self.aliases)
        keys.discard("")
        return keys


def build_index(models: list[ModelNames]) -> dict[str, str | None]:
    """key -> slug; None when a key fits more than one model."""
    idx: dict[str, str | None] = {}
    for m in models:
        for k in m.keys():
            idx[k] = m.slug if idx.get(k, m.slug) == m.slug else None
    return idx


def lookup(query: str, idx: dict[str, str | None]) -> str | None:
    """A model by name, slug, ref or alias; None if unknown or ambiguous."""
    return idx.get(norm_key(query))


def lookup_object(name: str, idx: dict[str, str | None]) -> str | None:
    """A slicer object: exact name first, then drop trailing copy numbers one at a time."""
    n = (name or "").strip()
    while True:
        slug = idx.get(object_key(n))
        if slug:
            return slug
        if not COPY_RE.search(n):
            return None
        n = COPY_RE.sub("", n, count=1)
