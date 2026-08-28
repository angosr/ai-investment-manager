"""Minimal parser for the decision-relevant body of a first-party HTML page."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True, slots=True)
class OfficialHtmlDocument:
    title: str
    body: str
    time_values: tuple[str, ...]


class _OfficialHtmlDocumentParser(HTMLParser):
    _HIDDEN_TAGS = frozenset({"script", "style", "noscript", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_title: str | None = None
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
            else None
        )
        should_select_candidate = (
            self._main_index is not None
            and self._content_index is None
            and candidate_kind is not None
            and (
                self._selected_content_kind is None
                or (
                    candidate_kind == "article"
                    and self._selected_content_kind != "article"
                )
            )
        )
        if should_select_candidate:
            if self._selected_content_kind is not None:
                self.time_values.clear()
                self._heading_parts.clear()
                self._body_parts.clear()
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
        if tag == "meta" and (values.get("property") or values.get("name")) == "og:title":
            title = (values.get("content") or "").strip()
            if title:
                self.meta_title = title

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
        if self._heading_depth:
            self._heading_parts.append(text)

    def document(self) -> OfficialHtmlDocument:
        title = (
            self.meta_title
            or " ".join(self._heading_parts).strip()
            or " ".join(self._document_title_parts).strip()
        )
        return OfficialHtmlDocument(
            title=title,
            body="\n".join(self._body_parts).strip(),
            time_values=tuple(self.time_values),
        )


def parse_official_html_document(content: str) -> OfficialHtmlDocument:
    parser = _OfficialHtmlDocumentParser()
    parser.feed(content)
    return parser.document()
