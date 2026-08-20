import json
from datetime import UTC, datetime

import httpx
import pytest

from investment_manager.information.official.public_calendar import (
    build_fed_chair_calendar_revision,
    build_fed_chair_cancellation,
    parse_fed_chair_calendar,
)
from investment_manager.information.official.records import (
    CalendarEventStatus,
    build_fomc_calendar_revision,
    parse_fed_monetary_rss,
    parse_fomc_calendar,
)
from investment_manager.information.official.source import HttpFedOfficialSource

OBSERVED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)


def test_fed_official_source_uses_conditional_request_without_redirects() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                text="<html>calendar</html>",
                headers={"etag": '"revision-1"'},
            )
        assert request.headers["if-none-match"] == '"revision-1"'
        return httpx.Response(304)

    source = HttpFedOfficialSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    assert source.fetch_calendar() == "<html>calendar</html>"
    assert source.fetch_calendar() is None
    assert len(requests) == 2
    assert requests[0].headers["accept"].startswith("text/html")


def test_fed_monetary_source_retries_406_once_with_broad_xml_accept() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            assert "text/xml" in request.headers["accept"]
            assert "*/*" not in request.headers["accept"]
            return httpx.Response(406)
        assert request.headers["accept"].endswith("*/*;q=0.8")
        return httpx.Response(200, text="<rss>monetary</rss>")

    source = HttpFedOfficialSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    assert source.fetch_monetary_rss() == "<rss>monetary</rss>"
    assert len(requests) == 2


def test_fed_public_calendar_source_requests_fixed_json_endpoint() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text='{"events": []}')

    source = HttpFedOfficialSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    assert source.fetch_public_calendar() == '{"events": []}'
    assert requests[0].url == "https://www.federalreserve.gov/json/calendar.json"
    assert requests[0].headers["accept"] == "application/json"


def test_fed_official_source_does_not_retry_other_http_errors() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503)

    source = HttpFedOfficialSource(
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(httpx.HTTPStatusError):
        source.fetch_monetary_rss()
    assert len(requests) == 1


def test_fomc_calendar_uses_stable_ordinal_and_eastern_release_time() -> None:
    html = """
    <h4><a id="42828">2026 FOMC Meetings</a></h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>January</strong></div>
      <div class="fomc-meeting__date">27-28</div>
      <br>
    </div>
    <div class="fomc-meeting--shaded row fomc-meeting">
      <div class="fomc-meeting__month"><strong>March</strong></div>
      <div class="fomc-meeting__date">17-18*</div>
      <strong>Projection Materials</strong>
    </div>
    """

    meetings = parse_fomc_calendar(html, observed_at=OBSERVED_AT, years=(2026,))

    assert [item.observation.source_record_id for item in meetings] == [
        "fomc-regular-2026-01",
        "fomc-regular-2026-02",
    ]
    assert meetings[0].statement_at == datetime(2026, 1, 28, 19, tzinfo=UTC)
    assert meetings[1].statement_at == datetime(2026, 3, 18, 18, tzinfo=UTC)
    assert meetings[1].has_projection_materials is True


def _public_calendar(*events: dict) -> str:
    return json.dumps({"events": [*events, {}]})


def _chair_event(*, day: str = "28", time: str = "10:00 a.m.") -> dict:
    return {
        "description": "Keynote Remarks",
        "live": "www.youtube.com/KansasCityFed",
        "location": "At the Jackson Hole Economic Policy Symposium",
        "title": "Speech - Chairman Kevin Warsh",
        "time": time,
        "month": "2026-08",
        "days": day,
        "type": "Speeches",
    }


def test_fed_public_calendar_selects_board_chair_and_preserves_reschedule_identity() -> None:
    vice_chair = {
        **_chair_event(day="8"),
        "title": "Discussion - Vice Chair Michelle W. Bowman",
    }
    first = parse_fed_chair_calendar(
        _public_calendar(_chair_event(), vice_chair),
        observed_at=OBSERVED_AT,
        years=(2026,),
    )
    revised = parse_fed_chair_calendar(
        _public_calendar(_chair_event(day="29", time="11:30 a.m."), vice_chair),
        observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        years=(2026,),
    )

    assert first.covered_years == (2026,)
    assert len(first.records) == 1
    assert first.records[0].scheduled_at == datetime(2026, 8, 28, 14, tzinfo=UTC)
    assert (
        first.records[0].observation.source_record_id
        == revised.records[0].observation.source_record_id
    )
    assert first.records[0].observation.payload_hash != revised.records[0].observation.payload_hash


def test_fed_chair_calendar_revision_and_cancellation_keep_logical_event() -> None:
    record = parse_fed_chair_calendar(
        _public_calendar(_chair_event()),
        observed_at=OBSERVED_AT,
        years=(2026,),
    ).records[0]
    first = build_fed_chair_calendar_revision(record)
    cancelled_record = build_fed_chair_cancellation(
        record,
        observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
        payload_ref="raw_source_payload_cancelled",
    )
    cancelled = build_fed_chair_calendar_revision(cancelled_record, previous=first)

    assert cancelled.event_id == first.event_id
    assert cancelled.previous_revision_id == first.revision_id
    assert cancelled.status == CalendarEventStatus.CANCELLED
    assert cancelled.scheduled_release_at == first.scheduled_release_at


def test_fomc_calendar_parses_cross_month_meeting() -> None:
    html = """
    <h4>2027 FOMC Meetings</h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>Jan/Feb</strong></div>
      <div class="fomc-meeting__date">31-1</div>
    </div>
    """

    meeting = parse_fomc_calendar(html, observed_at=OBSERVED_AT)[0]

    assert meeting.meeting_start.isoformat() == "2027-01-31"
    assert meeting.meeting_end.isoformat() == "2027-02-01"


def test_fomc_calendar_excludes_special_notation_vote_from_regular_schedule() -> None:
    html = """
    <h4>2025 FOMC Meetings</h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>August</strong></div>
      <div class="fomc-meeting__date">22 (notation vote)</div>
    </div>
    <h4>2026 FOMC Meetings</h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">15-16*</div>
    </div>
    """

    meetings = parse_fomc_calendar(html, observed_at=OBSERVED_AT)

    assert len(meetings) == 1
    assert meetings[0].meeting_start.isoformat() == "2026-09-15"


def test_fomc_calendar_revision_changes_payload_identity_not_logical_record() -> None:
    original = """
    <h4>2026 FOMC Meetings</h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">15-16*</div>
      <strong>Projection Materials</strong>
    </div>
    """
    revised = original.replace("15-16*", "16-17*")

    first = parse_fomc_calendar(original, observed_at=OBSERVED_AT)[0]
    second = parse_fomc_calendar(revised, observed_at=OBSERVED_AT)[0]

    assert first.observation.source_record_id == second.observation.source_record_id
    assert first.observation.payload_hash != second.observation.payload_hash
    assert first.observation.observation_id != second.observation.observation_id


def test_same_source_content_seen_later_has_distinct_candidate_identity() -> None:
    html = """
    <h4>2026 FOMC Meetings</h4>
    <div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">15-16*</div>
    </div>
    """

    first = parse_fomc_calendar(html, observed_at=OBSERVED_AT)[0]
    later = parse_fomc_calendar(
        html,
        observed_at=datetime(2026, 8, 20, 12, 1, tzinfo=UTC),
    )[0]

    assert first.observation.payload_hash == later.observation.payload_hash
    assert first.observation.observation_id != later.observation.observation_id


def test_fomc_calendar_revision_links_real_semantic_change() -> None:
    original = """
    <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">15-16*</div>
      <strong>Projection Materials</strong>
    </div>
    """
    revised = original.replace("15-16*", "16-17*")
    first_record = parse_fomc_calendar(original, observed_at=OBSERVED_AT)[0]
    second_record = parse_fomc_calendar(
        revised,
        observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )[0]

    first = build_fomc_calendar_revision(first_record)
    second = build_fomc_calendar_revision(second_record, previous=first)

    assert second.event_id == first.event_id
    assert second.previous_revision_id == first.revision_id
    assert second.content_hash != first.content_hash
    assert second.scheduled_release_at == datetime(2026, 9, 17, 18, tzinfo=UTC)


def test_fomc_calendar_does_not_create_poll_only_revision() -> None:
    html = """
    <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
      <div class="fomc-meeting__month"><strong>September</strong></div>
      <div class="fomc-meeting__date">15-16*</div>
    </div>
    """
    first_record = parse_fomc_calendar(html, observed_at=OBSERVED_AT)[0]
    repeated_record = parse_fomc_calendar(
        html,
        observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
    )[0]

    first = build_fomc_calendar_revision(first_record)

    with pytest.raises(ValueError, match="相同日历语义"):
        build_fomc_calendar_revision(repeated_record, previous=first)


def test_fed_monetary_rss_preserves_guid_and_detects_content_revision() -> None:
    xml = """<?xml version="1.0" encoding="utf-8"?>
    <rss version="2.0"><channel><item>
      <title>Federal Reserve issues FOMC statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm</link>
      <guid>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm</guid>
      <description>Federal Reserve issues FOMC statement</description>
      <pubDate>Wed, 29 Jul 2026 18:00:00 GMT</pubDate>
    </item></channel></rss>"""

    first = parse_fed_monetary_rss(xml, observed_at=OBSERVED_AT)[0]
    second = parse_fed_monetary_rss(
        xml.replace("issues FOMC statement</title>", "updates FOMC statement</title>"),
        observed_at=OBSERVED_AT,
    )[0]

    assert first.observation.source_record_id == first.source_url
    assert first.observation.source_published_at == datetime(2026, 7, 29, 18, tzinfo=UTC)
    assert first.observation.payload_hash != second.observation.payload_hash
    assert first.observation.observation_id != second.observation.observation_id


def test_fed_monetary_rss_rejects_future_publication_and_off_domain_link() -> None:
    template = """<rss><channel><item>
      <title>Statement</title><link>{link}</link><guid>record-1</guid>
      <description>Statement</description><pubDate>{published}</pubDate>
    </item></channel></rss>"""

    with pytest.raises(ValueError, match="不能晚于"):
        parse_fed_monetary_rss(
            template.format(
                link="https://www.federalreserve.gov/release",
                published="Thu, 20 Aug 2026 13:00:00 GMT",
            ),
            observed_at=OBSERVED_AT,
        )
    with pytest.raises(ValueError, match=r"federalreserve\.gov"):
        parse_fed_monetary_rss(
            template.format(
                link="https://example.com/release",
                published="Thu, 20 Aug 2026 11:00:00 GMT",
            ),
            observed_at=OBSERVED_AT,
        )
