import { api } from "../api/client";
import { useLive } from "../hooks";
import { hhmm } from "../lib/format";
import { Card } from "./Card";
import styles from "./LatestAssessment.module.css";

const DIRECTION: Record<string, string> = {
  UP: "看涨",
  DOWN: "看跌",
  UNCERTAIN: "方向不明",
};

const EVIDENCE_KIND: Record<string, string> = {
  FIRST_PARTY_FACT: "一手事实",
  STRUCTURED_FACT: "结构化聚合事实",
  INTELLIGENCE_EVENT: "事件",
  MARKET_STRUCTURE: "市场结构",
  MATERIAL_DELTA: "重大变化",
  MARKET_FEATURE: "市场特征",
  PREVIOUS_CONTEXT: "上一轮认知",
};

const CAPITAL_STATUS: Record<string, string> = {
  BASE_UNCHANGED: "程序基线不变",
  ENTRY_VETO_CANDIDATE: "入场否决研究候选",
  INSUFFICIENT_EVIDENCE: "证据不足，不改变基线",
};

/** Latest immutable world-cognition snapshot plus the current update status. */
export function LatestAssessment() {
  const latest = useLive(() => api.latestAssessment(), "cycles");
  const row = latest?.assessments[0] ?? null;
  const quality = latest?.quality ?? null;
  const currentSnapshot = Boolean(
    row && quality?.latest_valid_at && row.at === quality.latest_valid_at,
  );
  const currentRow = currentSnapshot ? row : null;
  const detail = useLive(
    () => currentRow
      ? api.assessmentRecord(currentRow.assessment_id)
      : Promise.resolve(null),
    "cycles",
    [currentRow?.assessment_id],
  );
  const activeEvents = detail
    ? detail.event_references
      .filter((item) => item.impact_state === "ACTIVE")
      .map((item) => ({
        evidence_id: item.evidence_id,
        at: item.event_time,
        source: item.source,
        title: item.title,
      }))
    : [];
  const citedEvidence = detail?.cited_evidence ?? [];
  const attemptMessage = quality?.latest_attempt_status === "NO_ATTEMPT"
    ? "当前分析版本尚未执行世界认知分析；这不是质量筛选结果。"
    : quality?.latest_attempt_status === "REJECTED"
      ? `最近一次分析输出无法形成可解析快照，失败已保留用于继续改进${quality.latest_attempt_reason ? `：${quality.latest_attempt_reason}。` : "。"}`
      : quality?.latest_attempt_status === "FAILED"
        ? `最近一次分析执行失败，失败已保留用于继续改进${quality.latest_attempt_reason ? `：${quality.latest_attempt_reason}。` : "。"}`
        : "当前分析版本尚未产生世界认知快照。";
  return (
    <Card
      title="最新世界认知"
      aside={currentRow
        ? `${hhmm(currentRow.at)} UTC`
        : "尚未建立"}
      bodyPadded
    >
      {quality && !currentSnapshot ? (
        <p className={styles.warning}>
          {attemptMessage}{row
            ? ` ${hhmm(row.at)} UTC 的旧版本快照仅保留在 AI 分析历史，不作为当前判断。`
            : ""}
        </p>
      ) : null}
      {currentRow ? (
        <div className={styles.layout}>
          <div>
            <div className={styles.summary}>
              {currentRow.summary}
            </div>
            {detail?.capital_relevance ? (
              <div className={styles.capital}>
                <b>
                  {CAPITAL_STATUS[detail.capital_relevance.status]
                    ?? detail.capital_relevance.status}
                </b>
                <span>{detail.capital_relevance.thesis}</span>
                <span>{detail.capital_relevance.transmission}</span>
                <small>研究旁路 · 资本权限：无</small>
              </div>
            ) : null}
            <p className={styles.mechanism}>
              {detail?.mechanism ?? currentRow.mechanism}
            </p>
            {detail && detail.drivers.length > 0 ? (
              <div className={styles.drivers}>
                {detail.drivers.slice(0, 3).map((driver) => (
                  <div key={driver.statement} className={styles.driver}>
                    <b>{driver.statement}</b>
                    <span>{driver.transmission}</span>
                  </div>
                ))}
              </div>
            ) : null}
          </div>
          <div>
            <div className={styles.views}>
              {(detail?.views ?? []).map((view) => (
                <span key={`${view.asset}-${view.horizon_minutes}`} data-direction={view.direction}>
                  {view.asset} {view.horizon_minutes}m · {DIRECTION[view.direction] ?? view.direction}
                </span>
              ))}
            </div>
            {detail && detail.data_gaps.length > 0 ? (
              <div className={styles.gaps}>仍缺：{detail.data_gaps.join("；")}</div>
            ) : null}
            {activeEvents.length > 0 ? (
              <div className={styles.evidence}>
                <b>当前仍影响未来的事件</b>
                {activeEvents.map((item) => (
                  <span key={item.evidence_id}>
                    {hhmm(item.at)} · {item.source} · {item.title}
                  </span>
                ))}
              </div>
            ) : null}
            {citedEvidence.length > 0 ? (
              <div className={styles.evidence}>
                <b>本次世界认知实际引用 · {citedEvidence.length} 条</b>
                {citedEvidence.map((item) => (
                  <span key={item.evidence_id}>
                    {hhmm(item.at)} · {EVIDENCE_KIND[item.kind] ?? item.kind} · {item.title}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : (
        <p className={styles.empty}>{attemptMessage}</p>
      )}
    </Card>
  );
}
