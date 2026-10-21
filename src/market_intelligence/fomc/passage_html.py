"""Bounded tree and inline coordinates, without changing acquisition validation."""

import re
from dataclasses import dataclass
from urllib.parse import unquote

from market_intelligence.fomc.passage_models import (
    Anchor,
    FomcParseError,
    Reference,
    ReferenceKind,
)
from market_intelligence.fomc.validation import EXCLUDED, VOID_TAGS, _Node, _one, _Page

HIDDEN = EXCLUDED | {"nav", "aside", "form", "button", "header", "footer"}
INLINE = {
    "a",
    "span",
    "sup",
    "sub",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "small",
    "abbr",
    "code",
    "br",
}


def hidden(node: _Node) -> bool:
    return node.tag in HIDDEN or bool(
        {"shareOptions", "shareWrapper", "shareLink"} & set((node.attrs.get("class") or "").split())
    )


def names(node: _Node) -> tuple[str, ...]:
    result = tuple(dict.fromkeys(value for key in ("id", "name") if (value := node.attrs.get(key))))
    if any(len(value) > 256 or any(c.isspace() or ord(c) < 32 for c in value) for value in result):
        raise FomcParseError("invalid_fomc_anchor")
    return result


class _PassagePage(_Page):
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        extra = [(k, v) for k, v in attrs if k in {"name", "start", "value", "type"}]
        if len({k for k, _ in extra}) != len(extra):
            raise FomcParseError("ambiguous_fomc_attribute")
        super().handle_starttag(tag, attrs)
        node = self.stack[-1] if tag not in VOID_TAGS else self.stack[-1].children[-1]
        assert isinstance(node, _Node)
        node.attrs.update(extra)


def article_tree(body: bytes) -> _Node:
    page = _PassagePage()
    page.feed(body.decode("utf-8-sig"))
    page.close()
    return _one([n for n in page.root.nodes() if n.attrs.get("id") == "article"])


@dataclass(frozen=True)
class InlineText:
    text: str
    anchors: tuple[Anchor, ...]
    references: tuple[Reference, ...]

    def portion(self, start: int, end: int) -> InlineText:
        while start < end and self.text[start] == " ":
            start += 1
        while end > start and self.text[end - 1] == " ":
            end -= 1
        anchors = tuple(
            Anchor(a.name, a.offset - start)
            for a in self.anchors
            if start <= a.offset < end or a.offset == end == len(self.text)
        )
        refs = []
        for r in self.references:
            if start <= r.start <= r.end <= end:
                refs.append(Reference(r.target, r.label, r.start - start, r.end - start, r.kind))
            elif r.start < end and r.end > start:
                raise FomcParseError("split_fomc_reference")
        return InlineText(self.text[start:end], anchors, tuple(refs))


def inline_text(children: list[_Node | str]) -> InlineText:
    pieces: list[str] = []
    length = 0
    anchors: list[tuple[str, int]] = []
    references: list[tuple[str, int, int, ReferenceKind]] = []

    def append(value: str) -> None:
        nonlocal length
        pieces.append(value)
        length += len(value)

    def visit(item: _Node | str, superscript: bool = False) -> None:
        if isinstance(item, str):
            append(item)
            return
        if hidden(item):
            return
        if item.tag not in INLINE or not item.complete:
            raise FomcParseError()
        for name in names(item):
            anchors.append((name, length))
        if item.tag == "br":
            append(" ")
            return
        href = item.attrs.get("href") or ""
        target = (
            unquote(href[1:]) if item.tag == "a" and href.startswith("#") and len(href) > 1 else ""
        )
        start = length
        backlink = target and item.text() == "Return to text"
        if not backlink:
            for child in item.children:
                visit(child, superscript or item.tag == "sup")
        if target:
            if len(target) > 256 or any(c.isspace() or ord(c) < 32 for c in target):
                raise FomcParseError("invalid_fomc_anchor")
            kind = (
                ReferenceKind.BACKLINK
                if backlink
                else (
                    ReferenceKind.FOOTNOTE
                    if superscript or re.fullmatch(r"fn[0-9]+", target)
                    else ReferenceKind.LOCAL
                )
            )
            references.append((target, start, length, kind))

    for child in children:
        visit(child)
    raw = "".join(pieces)
    mapping = [0] * (len(raw) + 1)
    output: list[str] = []
    pending = False
    for index, char in enumerate(raw):
        if char.isspace():
            pending = bool(output)
            mapping[index + 1] = len(output)
        else:
            if pending:
                output.append(" ")
            pending = False
            mapping[index] = len(output)
            output.append(char)
            mapping[index + 1] = len(output)
    text = "".join(output)
    result_refs = []
    for target, start, end, kind in references:
        left, right = mapping[start], mapping[end]
        while left < right and text[left] == " ":
            left += 1
        while right > left and text[right - 1] == " ":
            right -= 1
        result_refs.append(Reference(target, text[left:right], left, right, kind))
    return InlineText(
        text, tuple(Anchor(name, mapping[offset]) for name, offset in anchors), tuple(result_refs)
    )
