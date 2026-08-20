from datetime import UTC, datetime, timedelta

import pytest

from investment_manager.asset_management import Materiality
from investment_manager.fact_pipeline import (
    FED_MONETARY_RELEASE_FACT_TYPE,
    FOMC_MEETING_FACT_TYPE,
    FactDeltaPolicy,
    FactDeltaRule,
    OfficialFactProjectionPolicy,
    build_fact_material_delta,
    build_state_snapshot,
    project_fed_monetary_release_fact,
    project_fomc_calendar_fact,
)
from investment_manager.information.official import (
    build_fomc_calendar_revision,
    parse_fed_monetary_rss,
    parse_fomc_calendar,
)

OBSERVED_AT = datetime(2026, 8, 20, 12, tzinfo=UTC)
FACT_POLICY = OfficialFactProjectionPolicy(
    version="fed-fact-v1",
    affected_assets=("BTC", "ETH"),
)
DELTA_POLICY = FactDeltaPolicy(
    version="fact-delta-v1",
    validity_seconds=3_600,
    horizons_minutes=(60, 240),
    rules=(
        FactDeltaRule(
            fact_type=FED_MONETARY_RELEASE_FACT_TYPE,
            materiality=Materiality.HIGH,
            reason_code="FED_RELEASE_REVISION",
        ),
        FactDeltaRule(
            fact_type=FOMC_MEETING_FACT_TYPE,
            materiality=Materiality.NORMAL,
            reason_code="FOMC_SCHEDULE_REVISION",
        ),
    ),
)


def _meeting(date_text: str, *, observed_at: datetime):
    record = parse_fomc_calendar(
        f"""
        <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>September</strong></div>
          <div class="fomc-meeting__date">{date_text}</div>
        </div>
        """,
        observed_at=observed_at,
    )[0]
    return build_fomc_calendar_revision(record)


def test_official_fact_revision_is_semantic_and_links_predecessor() -> None:
    first_calendar = _meeting("15-16", observed_at=OBSERVED_AT)
    second_record = parse_fomc_calendar(
        """
        <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>September</strong></div>
          <div class="fomc-meeting__date">16-17</div>
        </div>
        """,
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )[0]
    second_calendar = build_fomc_calendar_revision(second_record, previous=first_calendar)

    first = project_fomc_calendar_fact(first_calendar, policy=FACT_POLICY)
    second = project_fomc_calendar_fact(
        second_calendar,
        policy=FACT_POLICY,
        previous=first,
    )

    assert second.fact_id == first.fact_id
    assert second.previous_revision_id == first.revision_id
    assert second.revision_id != first.revision_id
    assert second.revision_hash != first.revision_hash
    assert second.source_observation_ids == (second_calendar.source_observation_id,)


def test_same_fact_semantics_do_not_create_revision() -> None:
    calendar = _meeting("15-16", observed_at=OBSERVED_AT)
    first = project_fomc_calendar_fact(calendar, policy=FACT_POLICY)
    later = calendar.model_copy(update={"observed_at": OBSERVED_AT + timedelta(minutes=1)})

    with pytest.raises(ValueError, match="相同事实语义"):
        project_fomc_calendar_fact(later, policy=FACT_POLICY, previous=first)


def test_release_fact_preserves_point_in_time_source_identity() -> None:
    record = parse_fed_monetary_rss(
        """<rss><channel><item>
          <title>Federal Reserve issues FOMC statement</title>
          <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary.htm</link>
          <guid>fed-release-1</guid><description>Policy statement.</description>
          <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate>
        </item></channel></rss>""",
        observed_at=OBSERVED_AT,
    )[0]

    fact = project_fed_monetary_release_fact(record, policy=FACT_POLICY)

    assert fact.event_time == datetime(2026, 8, 19, 18, tzinfo=UTC)
    assert fact.source_observation_ids == (record.observation.observation_id,)
    assert fact.affected_assets == ("BTC", "ETH")


def test_state_identity_excludes_build_latency_but_enforces_visibility() -> None:
    fact = project_fomc_calendar_fact(
        _meeting("15-16", observed_at=OBSERVED_AT),
        policy=FACT_POLICY,
    )
    first = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT,
        facts=(fact,),
    )
    replay = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT + timedelta(seconds=30),
        facts=(fact,),
    )

    assert replay.state_id == first.state_id
    assert replay.content_hash == first.content_hash
    assert replay.built_at != first.built_at
    with pytest.raises(ValueError, match="as_of 之后"):
        build_state_snapshot(
            projection_version="state-v1",
            analysis_scope="crypto-macro",
            as_of=OBSERVED_AT - timedelta(seconds=1),
            built_at=OBSERVED_AT,
            facts=(fact,),
        )


def test_fact_delta_is_noop_on_bootstrap_and_emits_only_new_revision() -> None:
    first_fact = project_fomc_calendar_fact(
        _meeting("15-16", observed_at=OBSERVED_AT),
        policy=FACT_POLICY,
    )
    first_state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT,
        facts=(first_fact,),
    )
    assert (
        build_fact_material_delta(
            previous=None,
            current=first_state,
            current_facts=(first_fact,),
            policy=DELTA_POLICY,
        )
        is None
    )

    second_calendar_record = parse_fomc_calendar(
        """
        <h4>2026 FOMC Meetings</h4><div class="row fomc-meeting">
          <div class="fomc-meeting__month"><strong>September</strong></div>
          <div class="fomc-meeting__date">16-17</div>
        </div>
        """,
        observed_at=OBSERVED_AT + timedelta(minutes=1),
    )[0]
    second_fact = project_fomc_calendar_fact(
        build_fomc_calendar_revision(second_calendar_record),
        policy=FACT_POLICY,
        previous=first_fact,
    )
    second_state = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT + timedelta(minutes=1),
        built_at=OBSERVED_AT + timedelta(minutes=1),
        facts=(second_fact,),
    )

    delta = build_fact_material_delta(
        previous=first_state,
        current=second_state,
        current_facts=(second_fact,),
        policy=DELTA_POLICY,
    )

    assert delta is not None
    assert delta.fact_revision_ids == (second_fact.revision_id,)
    assert delta.horizons_minutes == (60, 240)
    assert delta.materiality == Materiality.NORMAL
    assert delta.expires_at == second_fact.observed_at + timedelta(hours=1)


def test_fact_delta_fails_closed_when_policy_does_not_classify_fact() -> None:
    fact = project_fomc_calendar_fact(
        _meeting("15-16", observed_at=OBSERVED_AT),
        policy=FACT_POLICY,
    )
    previous = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT - timedelta(seconds=1),
        built_at=OBSERVED_AT,
        facts=(),
    )
    current = build_state_snapshot(
        projection_version="state-v1",
        analysis_scope="crypto-macro",
        as_of=OBSERVED_AT,
        built_at=OBSERVED_AT,
        facts=(fact,),
    )
    incomplete_policy = FactDeltaPolicy(
        version="fact-delta-v1",
        validity_seconds=3_600,
        horizons_minutes=(60,),
        rules=(
            FactDeltaRule(
                fact_type=FED_MONETARY_RELEASE_FACT_TYPE,
                materiality=Materiality.HIGH,
                reason_code="FED_RELEASE_REVISION",
            ),
        ),
    )

    with pytest.raises(ValueError, match="缺少规则"):
        build_fact_material_delta(
            previous=previous,
            current=current,
            current_facts=(fact,),
            policy=incomplete_policy,
        )
