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
  SUPPORT: "支持程序动作",
  NEUTRAL: "不改变程序动作",
  CAUTION: "谨慎执行程序动作",
  OPPOSE: "反对本次程序动作",
  INSUFFICIENT: "增量证据不足",
};

const HYPOTHESIS_ROLE: Record<string, string> = {
  PRIMARY: "主假设",
  ALTERNATIVE: "替代假设",
  TAIL_RISK: "尾部风险",
};

const MECHANISM_RELATIONSHIP: Record<string, string> = {
  SUPPORTS: "强化",
  OFFSETS: "抵消",
  THREATENS: "反转威胁",
  ALTERNATIVE: "竞争解释",
};

const TRANSMISSION_STAGE: Record<string, string> = {
  PENDING: "尚待传导",
  PROPAGATING: "正在传导",
  PRICED: "已被主要计价",
  REVERSING: "正在反转",
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
            <p className={styles.mechanism}>
              {detail?.mechanism ?? currentRow.mechanism}
            </p>
            {detail && detail.mechanisms.length > 0 ? (
              <div className={styles.drivers}>
                {detail.mechanisms.map((mechanism) => (
                  <div key={mechanism.mechanism_id} className={styles.driver}>
                    <b>
                      {MECHANISM_RELATIONSHIP[mechanism.relationship]} · {TRANSMISSION_STAGE[mechanism.transmission_stage]} · {mechanism.horizon_hours} 小时
                    </b>
                    <span>{mechanism.claim}</span>
                    <small>复核时间：{mechanism.next_review_at}</small>
                  </div>
                ))}
              </div>
            ) : detail && detail.hypotheses.length > 0 ? (
              <div className={styles.drivers}>
                {detail.hypotheses.map((hypothesis) => (
                  <div key={hypothesis.hypothesis_id} className={styles.driver}>
                    <b>
                      {HYPOTHESIS_ROLE[hypothesis.role]} · {hypothesis.horizon_hours} 小时
                    </b>
                    <span>{hypothesis.claim}</span>
                    <small>下一观测：{hypothesis.next_observation}</small>
                  </div>
                ))}
              </div>
            ) : detail && detail.drivers.length > 0 ? (
              <div className={styles.drivers}>
                {detail.drivers.slice(0, 3).map((driver) => (
                  <div key={driver.statement} className={styles.driver}>
                    <b>{driver.statement}</b>
                    <span>{driver.transmission}</span>
                  </div>
                ))}
              </div>
            ) : null}
            {detail?.capital_relevance ? (
              <div className={styles.capital}>
                <b>
                  当前产品相关性 · {CAPITAL_STATUS[detail.capital_relevance.status]
                    ?? detail.capital_relevance.status}
                </b>
                <span>{detail.capital_relevance.thesis}</span>
                <span>{detail.capital_relevance.transmission}</span>
                <small>研究旁路 · 资本权限：无</small>
              </div>
            ) : null}
            {detail?.capital_implication ? (
              <div className={styles.capital}>
                <b>
                  对当前程序策略 · {CAPITAL_STATUS[detail.capital_implication.effect]
                    ?? detail.capital_implication.effect}
                </b>
                <span>{detail.capital_implication.incremental_reason}</span>
                <span>{detail.capital_implication.transmission}</span>
                <small>研究旁路 · 通过配对评估前资本权限为无</small>
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
