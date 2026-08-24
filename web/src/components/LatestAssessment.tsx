import { api } from "../api/client";
import { useLive } from "../hooks";
import { hhmm } from "../lib/format";
import { Card } from "./Card";
import styles from "./LatestAssessment.module.css";

const RELATIONSHIP: Record<string, string> = {
  SUPPORTS: "强化",
  OFFSETS: "抵消",
  THREATENS: "反转威胁",
  ALTERNATIVE: "竞争解释",
};

const STAGE: Record<string, string> = {
  PENDING: "尚待传导",
  PROPAGATING: "正在传导",
  PRICED: "已被主要计价",
  REVERSING: "正在反转",
};

const EVIDENCE_KIND: Record<string, string> = {
  FIRST_PARTY_FACT: "一手事实",
  STRUCTURED_FACT: "结构化事实",
  INTELLIGENCE_EVENT: "事件",
  MARKET_STRUCTURE: "市场结构",
  MATERIAL_DELTA: "重大变化",
  MARKET_FEATURE: "市场特征",
  PREVIOUS_CONTEXT: "上一轮认知",
};

export function LatestAssessment() {
  const latest = useLive(() => api.latestAssessment(), "cycles");
  const row = latest?.assessments[0] ?? null;
  const quality = latest?.quality ?? null;
  const detail = useLive(
    () => row ? api.assessmentRecord(row.assessment_id) : Promise.resolve(null),
    "cycles",
    [row?.assessment_id],
  );
  const updateFailed = quality?.latest_attempt_status === "FAILED"
    || quality?.latest_attempt_status === "REJECTED";
  const activeEvents = detail?.event_references.filter(
    (item) => item.impact_state === "ACTIVE",
  ) ?? [];

  return (
    <Card title="最新世界认知" aside={row ? `${hhmm(row.at)} UTC` : "尚未建立"} bodyPadded>
      {updateFailed ? (
        <p className={styles.warning}>
          最近一次更新失败；下方保留上一份有效认知，不把失败伪装成新判断
          {quality?.latest_attempt_reason ? `：${quality.latest_attempt_reason}` : "。"}
        </p>
      ) : null}
      {row ? (
        <div className={styles.layout}>
          <div>
            <p className={styles.synthesis}>{detail?.synthesis ?? row.synthesis}</p>
            <div className={styles.meta}>
              {row.synthesis_horizon_hours} 小时观察窗 · {row.driver_count} 个机制 · {row.evidence_count} 条引用
            </div>
            <div className={styles.drivers}>
              {(detail?.mechanisms ?? []).map((mechanism) => (
                <div key={mechanism.mechanism_id} className={styles.driver}>
                  <b>
                    {RELATIONSHIP[mechanism.relationship] ?? mechanism.relationship} · {STAGE[mechanism.transmission_stage] ?? mechanism.transmission_stage} · {mechanism.horizon_hours} 小时
                  </b>
                  <span>{mechanism.claim}</span>
                  <small>下次自然复核：{mechanism.next_review_at}</small>
                </div>
              ))}
            </div>
          </div>
          <div>
            {activeEvents.length ? (
              <div className={styles.evidence}>
                <b>仍影响未来的事件</b>
                {activeEvents.map((item) => (
                  <span key={item.evidence_id}>
                    {hhmm(item.event_time)} · {item.source} · {item.title}
                  </span>
                ))}
              </div>
            ) : null}
            {detail?.cited_evidence.length ? (
              <div className={styles.evidence}>
                <b>实际引用 · {detail.cited_evidence.length} 条</b>
                {detail.cited_evidence.map((item) => (
                  <span key={item.evidence_id}>
                    {hhmm(item.at)} · {EVIDENCE_KIND[item.kind] ?? item.kind} · {item.title}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        </div>
      ) : (
        <p className={styles.empty}>
          尚无成功的世界认知。系统会保留执行失败原因，并在下一个事件或复核时点重试。
        </p>
      )}
    </Card>
  );
}
