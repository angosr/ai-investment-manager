from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator

from investment_manager.information.models import SourceObservation, SourceTier
from investment_manager.information.official.records import OfficialRecordKind
from investment_manager.information.raw_payload import build_raw_source_payload
from investment_manager.kernel.identity import content_hash, stable_id
from investment_manager.kernel.time import optional_utc, require_utc
from investment_manager.kernel.types import FrozenModel

FEDERAL_REGISTER_SOURCE_ID = "federal-register"
FEDERAL_REGISTER_RULEMAKING_STREAM_ID = "federal-register-digital-assets"
FEDERAL_REGISTER_API_ROOT = "https://www.federalregister.gov/api/v1/documents.json"

_ALLOWED_AGENCIES = frozenset(
    {
        "Commodity Futures Trading Commission",
        "Securities and Exchange Commission",
    }
)
_DECISION_RELEVANT_TYPES = frozenset({"Notice", "Proposed Rule", "Rule"})
_DIGITAL_ASSET_TERMS = re.compile(
    r"\b(?:bitcoin|blockchain|crypto(?:currencies|currency|assets?|markets?)?|"
    r"digital assets?|distributed ledger|ethereum|stablecoins?|tokeni[sz](?:ation|ed)|"
    r"virtual currenc(?:y|ies))\b",
    re.IGNORECASE,
)


class FederalRegisterRulemakingRecord(FrozenModel):
    """Decision-relevant SEC/CFTC publication from the official register."""

    observation: SourceObservation
    kind: Literal[OfficialRecordKind.FEDERAL_REGISTER_RULEMAKING] = (
        OfficialRecordKind.FEDERAL_REGISTER_RULEMAKING
    )
    document_number: str = Field(min_length=1, max_length=80)
    document_type: str = Field(min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=1_000)
    abstract: str = Field(default="", max_length=4_000)
    action: str = Field(default="", max_length=1_000)
    publication_date: date
    effective_at: datetime | None = None
    comments_close_at: datetime | None = None
    agencies: tuple[str, ...] = Field(min_length=1)
    source_url: str = Field(min_length=1, max_length=2_000)

    _utc_effective = field_validator("effective_at")(optional_utc)
    _utc_comments_close = field_validator("comments_close_at")(optional_utc)

    @model_validator(mode="after")
    def identity_and_source_are_consistent(self):
        if self.observation.source_id != FEDERAL_REGISTER_SOURCE_ID:
            raise ValueError("Federal Register 记录来源身份非法")
        if self.observation.source_tier != SourceTier.FIRST_PARTY:
            raise ValueError("Federal Register 记录必须来自一手官方发布")
        if self.observation.source_published_at is None:
            raise ValueError("Federal Register 记录缺少发布时间")
        if self.observation.source_record_id != self.document_number:
            raise ValueError("Federal Register 记录身份与文号不一致")
        if self.document_type not in _DECISION_RELEVANT_TYPES:
            raise ValueError("Federal Register 文档类型不属于规则制定范围")
        if tuple(sorted(set(self.agencies))) != self.agencies:
            raise ValueError("Federal Register agencies 必须唯一且排序")
        if not set(self.agencies).issubset(_ALLOWED_AGENCIES):
            raise ValueError("Federal Register 记录包含未授权机构")
        parsed = urlparse(self.source_url)
        if parsed.scheme != "https" or parsed.hostname != "www.federalregister.gov":
            raise ValueError("Federal Register 记录必须引用官方 HTTPS 页面")
        expected_hash = content_hash(federal_register_semantic_payload(self))
        if self.observation.payload_hash != expected_hash:
            raise ValueError("Federal Register 记录语义哈希不一致")
        expected_id = stable_id(
            "source_observation",
            FEDERAL_REGISTER_SOURCE_ID,
            self.document_number,
            expected_hash,
            self.observation.observed_at.isoformat(),
        )
        if self.observation.observation_id != expected_id:
            raise ValueError("Federal Register observation identity 不一致")
        return self


def federal_register_semantic_payload(record: FederalRegisterRulemakingRecord) -> dict:
    return record.model_dump(mode="json", exclude={"observation", "kind"})


def parse_federal_register_rulemaking(
    content: bytes,
    *,
    source_url: str,
    observed_at: datetime,
) -> tuple[FederalRegisterRulemakingRecord, ...]:
    observed_at = require_utc(observed_at)
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Federal Register API JSON 非法") from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise ValueError("Federal Register API 缺少 results")
    raw = build_raw_source_payload(
        source_id=FEDERAL_REGISTER_SOURCE_ID,
        source_url=source_url,
        media_type="application/json",
        observed_at=observed_at,
        content=content,
    )
    records: list[FederalRegisterRulemakingRecord] = []
    for item in results:
        if not isinstance(item, dict):
            raise ValueError("Federal Register 文档必须是对象")
        document_type = _text(item.get("type"), maximum=80)
        title = _text(item.get("title"), maximum=1_000)
        abstract = _text(item.get("abstract"), maximum=4_000, required=False)
        action = _text(item.get("action"), maximum=1_000, required=False)
        searchable = " ".join((title, abstract, action))
        if (
            document_type not in _DECISION_RELEVANT_TYPES
            or _DIGITAL_ASSET_TERMS.search(searchable) is None
        ):
            continue
        agencies_raw = item.get("agencies")
        if not isinstance(agencies_raw, list):
            raise ValueError("Federal Register 文档缺少 agencies")
        agencies = tuple(
            sorted(
                {
                    _text(agency.get("name"), maximum=200)
                    for agency in agencies_raw
                    if isinstance(agency, dict)
                    and agency.get("name") in _ALLOWED_AGENCIES
                }
            )
        )
        if not agencies:
            continue
        document_number = _text(item.get("document_number"), maximum=80)
        publication_date = _date(item.get("publication_date"), "publication_date")
        published_at = datetime.combine(publication_date, time.min, tzinfo=UTC)
        if published_at > observed_at:
            raise ValueError("Federal Register 发布时间晚于观察时间")
        fields = {
            "document_number": document_number,
            "document_type": document_type,
            "title": title,
            "abstract": abstract,
            "action": action,
            "publication_date": publication_date,
            # Federal Register can expose an ``effective_on`` metadata value for
            # proposals.  It is not a legal effective date until the document is
            # a final Rule, so do not let that field overstate proposal status.
            "effective_at": (
                _optional_date_time(item.get("effective_on"))
                if document_type == "Rule"
                else None
            ),
            "comments_close_at": _optional_date_time(item.get("comments_close_on")),
            "agencies": agencies,
            "source_url": _official_document_url(item.get("html_url")),
        }
        semantic_hash = content_hash(fields)
        records.append(
            FederalRegisterRulemakingRecord(
                observation=SourceObservation(
                    observation_id=stable_id(
                        "source_observation",
                        FEDERAL_REGISTER_SOURCE_ID,
                        document_number,
                        semantic_hash,
                        observed_at.isoformat(),
                    ),
                    source_id=FEDERAL_REGISTER_SOURCE_ID,
                    source_tier=SourceTier.FIRST_PARTY,
                    source_record_id=document_number,
                    observed_at=observed_at,
                    source_published_at=published_at,
                    payload_hash=semantic_hash,
                    payload_ref=raw.payload_id,
                ),
                **fields,
            )
        )
    return tuple(sorted(records, key=lambda item: (item.publication_date, item.document_number)))


def _text(value, *, maximum: int, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise ValueError("Federal Register 必填文本为空")
    return text[:maximum]


def _date(value, name: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"Federal Register {name} 日期非法") from exc


def _optional_date_time(value) -> datetime | None:
    if value in {None, ""}:
        return None
    return datetime.combine(_date(value, "可选"), time.min, tzinfo=UTC)


def _official_document_url(value) -> str:
    url = _text(value, maximum=2_000)
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "www.federalregister.gov":
        raise ValueError("Federal Register 文档 URL 非官方地址")
    return url
