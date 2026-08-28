"""Parse one first-party HTML document and derive an auditable prompt projection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from investment_manager.information.text import sanitize_external_text

MAXIMUM_OFFICIAL_DOCUMENT_CHARACTERS = 50_000

_WORD = re.compile(r"[A-Za-z][A-Za-z'-]{3,}")
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_POLICY_ACTION = re.compile(
    r"\b(?:"
    r"adopt\w*|announc\w*|approv\w*|authoriz\w*|begin\w*|bill|codif\w*|"
    r"commenc\w*|commit\w*|decreas\w*|deliver\w*|enact\w*|establish\w*|"
    r"expect(?:ed|s|ing)?|implement\w*|legislat\w*|"
    r"increas\w*|intend\w*|issu\w*|launch\w*|must|plan\w*|propos\w*|"
    r"pass\w*|requir\w*|sign\w*|suspend\w*|terminat\w*|will"
    r")\b",
    re.IGNORECASE,
)
_QUANTIFIED_CLAIM = re.compile(r"(?:\b20\d{2}\b|\b\d+(?:\.\d+)?%|\$\s?\d)")
_POLICY_TOPIC = re.compile(
    r"\b(?:"
    r"balance sheet|central bank|credit|dollar|employment|federal funds|"
    r"financial conditions|fiscal|growth|inflation|interest rates?|liquidity|"
    r"monetary policy|money supply|policy rates?|price stability|regulat\w*|"
    r"sanction\w*|tariff\w*|treasur\w*|unemployment"
    r")\b",
    re.IGNORECASE,
)
_POLICY_STANCE = re.compile(
    r"\b(?:"
    r"above|anchored|below|concern\w*|elevated|firm|focus|objective|"
    r"predominant|readiness|restrict\w*|target|tighten\w*|eas\w*"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class OfficialHtmlBlock:
    kind: str
    text: str


@dataclass(frozen=True, slots=True)
class OfficialHtmlDocument:
    title: str
    body: str
    time_values: tuple[str, ...]
    description: str = ""
    blocks: tuple[OfficialHtmlBlock, ...] = ()


class _OfficialHtmlDocumentParser(HTMLParser):
    _HIDDEN_TAGS = frozenset({"script", "style", "noscript", "svg"})
    _CONTENT_BLOCK_TAGS = frozenset({"blockquote", "h2", "h3", "h4", "li", "p"})

    def __init__(self, *, maximum_body_length: int) -> None:
        super().__init__(convert_charrefs=True)
        self._maximum_body_length = maximum_body_length
        self.meta_title: str | None = None
        self.meta_description: str | None = None
        self.time_values: list[str] = []
        self._tags: list[str] = []
        self._main_index: int | None = None
        self._content_index: int | None = None
        self._selected_content_kind: str | None = None
        self._hidden_depth = 0
        self._heading_depth = 0
        self._document_title_depth = 0
        self._document_title_parts: list[str] = []
        self._heading_parts: list[str] = []
        self._body_parts: list[str] = []
        self._blocks: list[OfficialHtmlBlock] = []
        self._block_index: int | None = None
        self._block_kind: str | None = None
        self._block_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = dict(attrs)
        self._capture_metadata(tag, values)
        if tag == "title":
            self._document_title_depth += 1
        if self._main_index is None and (
            tag == "main" or (values.get("role") or "").lower() == "main"
        ):
            self._main_index = len(self._tags)
        class_tokens = frozenset((values.get("class") or "").split())
        candidate_kind = (
            "article"
            if tag == "article"
            else "body-field"
            if "field--name-body" in class_tokens
            else "article-container"
            if values.get("id") == "article"
            else None
        )
        should_select_candidate = (
            self._main_index is not None
            and self._content_index is None
            and candidate_kind is not None
            and (
                self._selected_content_kind is None
                or (candidate_kind == "article" and self._selected_content_kind != "article")
            )
        )
        if should_select_candidate:
            if self._selected_content_kind is not None:
                self.time_values.clear()
                self._heading_parts.clear()
                self._body_parts.clear()
                self._blocks.clear()
                self._block_index = None
                self._block_kind = None
                self._block_parts.clear()
            self._selected_content_kind = candidate_kind
            self._content_index = len(self._tags)
        self._tags.append(tag)
        if self._content_index is not None:
            if tag in self._HIDDEN_TAGS:
                self._hidden_depth += 1
            if tag == "h1":
                self._heading_depth += 1
            if tag == "time":
                self._capture_time(values)
            if self._block_index is None and tag in self._CONTENT_BLOCK_TAGS:
                self._block_index = len(self._tags) - 1
                self._block_kind = tag
                self._block_parts = []

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.lower()
        values = dict(attrs)
        self._capture_metadata(tag, values)
        if tag == "time" and self._content_index is not None:
            self._capture_time(values)

    def _capture_metadata(self, tag: str, values: dict[str, str | None]) -> None:
        if tag != "meta":
            return
        name = (values.get("property") or values.get("name") or "").lower()
        content = (values.get("content") or "").strip()
        if not content:
            return
        if name == "og:title":
            self.meta_title = content
        elif name == "og:description" or (name == "description" and self.meta_description is None):
            self.meta_description = content

    def _capture_time(self, values: dict[str, str | None]) -> None:
        value = (values.get("datetime") or "").strip()
        if value:
            self.time_values.append(value)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._document_title_depth:
            self._document_title_depth -= 1
        try:
            index = len(self._tags) - 1 - self._tags[::-1].index(tag)
        except ValueError:
            return
        if self._content_index is not None:
            if tag in self._HIDDEN_TAGS and self._hidden_depth:
                self._hidden_depth -= 1
            if tag == "h1" and self._heading_depth:
                self._heading_depth -= 1
            if self._block_index is not None and index <= self._block_index:
                self._finish_block()
            if index <= self._content_index:
                self._content_index = None
                self._hidden_depth = 0
                self._heading_depth = 0
        if self._main_index is not None and index <= self._main_index:
            self._main_index = None
        del self._tags[index:]

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text and self._document_title_depth:
            self._document_title_parts.append(text)
        if not text or self._content_index is None or self._hidden_depth:
            return
        self._body_parts.append(text)
        if self._block_index is not None:
            self._block_parts.append(text)
        if self._heading_depth:
            self._heading_parts.append(text)

    def _finish_block(self) -> None:
        text = " ".join(self._block_parts).strip()
        if text and self._block_kind is not None:
            self._blocks.append(OfficialHtmlBlock(kind=self._block_kind, text=text))
        self._block_index = None
        self._block_kind = None
        self._block_parts = []

    def document(self) -> OfficialHtmlDocument:
        self._finish_block()
        title = (
            self.meta_title
            or " ".join(self._heading_parts).strip()
            or " ".join(self._document_title_parts).strip()
        )
        blocks, body = _bounded_blocks(
            self._blocks,
            fallback="\n".join(self._body_parts).strip(),
            maximum_length=self._maximum_body_length,
        )
        return OfficialHtmlDocument(
            title=title,
            body=body,
            time_values=tuple(self.time_values),
            description=(self.meta_description or "").strip(),
            blocks=blocks,
        )


def parse_official_html_document(
    content: str,
    *,
    maximum_body_length: int = MAXIMUM_OFFICIAL_DOCUMENT_CHARACTERS,
) -> OfficialHtmlDocument:
    if maximum_body_length < 1:
        raise ValueError("official document body limit must be positive")
    parser = _OfficialHtmlDocumentParser(maximum_body_length=maximum_body_length)
    parser.feed(content)
    return parser.document()


def _bounded_blocks(
    blocks: list[OfficialHtmlBlock],
    *,
    fallback: str,
    maximum_length: int,
) -> tuple[tuple[OfficialHtmlBlock, ...], str]:
    if not blocks:
        return (), fallback[:maximum_length]
    selected: list[OfficialHtmlBlock] = []
    used = 0
    for block in blocks:
        separator = 1 if selected else 0
        remaining = maximum_length - used - separator
        if remaining <= 0:
            break
        text = block.text[:remaining]
        if text:
            selected.append(OfficialHtmlBlock(kind=block.kind, text=text))
            used += len(text) + separator
        if len(text) != len(block.text):
            break
    bounded = tuple(selected)
    return bounded, "\n".join(block.text for block in bounded)


def build_official_decision_excerpt(
    document: OfficialHtmlDocument,
    *,
    source_summary: str = "",
    maximum_length: int = 1_200,
    maximum_fragment_length: int = 240,
) -> str:
    """Select source-verbatim claims instead of truncating an article's opening.

    The full normalized body remains the evidence.  This bounded projection only
    orders extracts for model attention; it does not paraphrase or infer a claim.
    Generic action language and title overlap keep the implementation independent
    of any agency, asset or current event.
    """

    if maximum_length < 100 or maximum_fragment_length < 80:
        raise ValueError("official document projection limits are too small")
    title_tokens = _words(document.title)
    candidates = _projection_candidates(document, source_summary=source_summary)
    if not candidates:
        body, _ = sanitize_external_text(document.body, maximum_length=maximum_length)
        return body

    ranked = sorted(
        candidates,
        key=lambda item: (
            -_projection_score(
                item[1],
                kind=item[0],
                position=item[2],
                title_tokens=title_tokens,
            ),
            item[2],
            item[1],
        ),
    )
    selected: list[str] = []
    selected_tokens: list[set[str]] = []
    used = 0
    for _, text, _ in ranked:
        tokens = _words(text)
        if any(_overlap(tokens, existing) >= 0.75 for existing in selected_tokens):
            continue
        fragment = _bounded_fragment(text, maximum_fragment_length)
        separator = 1 if selected else 0
        remaining = maximum_length - used - separator
        if remaining < 80:
            break
        if len(fragment) > remaining:
            fragment = _bounded_fragment(fragment, remaining)
        if len(fragment) < 40:
            continue
        selected.append(fragment)
        selected_tokens.append(tokens)
        used += len(fragment) + separator
        if used >= maximum_length:
            break
    return "\n".join(selected)


def _projection_candidates(
    document: OfficialHtmlDocument,
    *,
    source_summary: str,
) -> list[tuple[str, str, int]]:
    raw: list[tuple[str, str, int]] = []
    summary, _ = sanitize_external_text(source_summary, maximum_length=1_000)
    description, _ = sanitize_external_text(document.description, maximum_length=1_000)
    if _is_substantive(summary, title=document.title):
        raw.append(("source-summary", summary, -2))
    if _is_substantive(description, title=document.title):
        raw.append(("page-description", description, -1))
    for position, block in enumerate(document.blocks):
        text, _ = sanitize_external_text(block.text, maximum_length=2_000)
        if not _is_substantive(text, title=document.title):
            continue
        kind = "heading" if block.kind in {"h2", "h3", "h4"} else "body"
        raw.append((kind, text, position))
    if not raw:
        for position, line in enumerate(document.body.splitlines()):
            text, _ = sanitize_external_text(line, maximum_length=2_000)
            if _is_substantive(text, title=document.title):
                raw.append(("body", text, position))
    deduplicated: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for item in raw:
        identity = item[1].casefold()
        if identity not in seen:
            seen.add(identity)
            deduplicated.append(item)
    return deduplicated


def _projection_score(
    text: str,
    *,
    kind: str,
    position: int,
    title_tokens: set[str],
) -> float:
    title_overlap = _overlap(_words(text), title_tokens)
    action_count = min(4, len(_POLICY_ACTION.findall(text)))
    quantified = 1 if _QUANTIFIED_CLAIM.search(text) else 0
    source_bonus = 0.5 if kind in {"source-summary", "page-description"} else 0
    heading_bonus = 1 if kind == "heading" else 0
    lead_bonus = 0.25 if position == 0 else 0
    policy_topics = min(5, len(_POLICY_TOPIC.findall(text)))
    policy_stance = min(3, len(_POLICY_STANCE.findall(text)))
    return (
        source_bonus
        + heading_bonus
        + lead_bonus
        + action_count * 2.5
        + policy_topics * 1.25
        + policy_stance * 1.5
        + quantified * 0.75
        + title_overlap * 2
    )


def _words(value: str) -> set[str]:
    return {match.group(0).casefold() for match in _WORD.finditer(value)}


def _is_substantive(value: str, *, title: str) -> bool:
    words = _words(value)
    folded = value.casefold()
    return (
        len(value) >= 60
        and len(words) >= 8
        and folded != title.casefold()
        and "return to text" not in folded
        and not value.rstrip().endswith("?")
    )


def _overlap(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0
    return len(left & right) / min(len(left), len(right))


def _bounded_fragment(value: str, maximum_length: int) -> str:
    if len(value) <= maximum_length:
        return value
    sentences = _SENTENCE_BOUNDARY.split(value)
    if len(sentences) > 1:
        head_tail = f"{sentences[0]} … {sentences[-1]}"
        if len(head_tail) <= maximum_length:
            return head_tail
    selected: list[str] = []
    used = 0
    for sentence in sentences:
        cost = len(sentence) + (1 if selected else 0)
        if selected and used + cost > maximum_length:
            break
        if not selected and cost > maximum_length:
            boundary = value.rfind(" ", 0, maximum_length - 1)
            return value[: max(40, boundary)].rstrip(" ,;:") + "…"
        selected.append(sentence)
        used += cost
    return " ".join(selected)
