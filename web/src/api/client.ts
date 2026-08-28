// 只读 API 客户端：全部 GET，无写操作。

import type {
  Accounts,
  AssessmentFeed,
  AssessmentRecordDetail,
  CapitalAction,
  CapitalActionDetail,
  CapitalEquityPoint,
  CapitalOverview,
  ForecastEvaluationEvidence,
  Health,
  Page,
  Resources,
  WorldEvent,
} from "./types";

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    throw new Error(`请求失败 ${response.status}: ${path}`);
  }
  return (await response.json()) as T;
}

function pagePath(path: string, cursor?: string, limit = 30): string {
  const query = new URLSearchParams({ limit: String(limit) });
  if (cursor) query.set("cursor", cursor);
  return `${path}?${query.toString()}`;
}

async function unseenEquityPoints(
  knownSnapshotIds: ReadonlySet<string>,
): Promise<CapitalEquityPoint[]> {
  const items: CapitalEquityPoint[] = [];
  const seenCursors = new Set<string>();
  let cursor: string | undefined;
  for (;;) {
    const page = await api.capitalEquity(cursor, 100);
    for (const point of page.items) {
      if (knownSnapshotIds.has(point.snapshot_id)) return items;
      items.push(point);
    }
    if (page.nextCursor === null) return items;
    if (seenCursors.has(page.nextCursor)) {
      throw new Error("分页游标重复，拒绝展示不完整权益历史");
    }
    seenCursors.add(page.nextCursor);
    cursor = page.nextCursor;
  }
}

let equityHistoryCache: CapitalEquityPoint[] | null = null;

export const api = {
  health: () => getJson<Health>("/api/health"),
  capital: () => getJson<CapitalOverview>("/api/capital"),
  capitalEquity: async (cursor?: string, limit = 100): Promise<Page<CapitalEquityPoint>> => {
    const result = await getJson<{
      points: CapitalEquityPoint[];
      next_cursor: string | null;
    }>(pagePath("/api/capital/equity", cursor, limit));
    return { items: result.points, nextCursor: result.next_cursor };
  },
  capitalEquityHistory: async () => {
    const known = new Set(
      (equityHistoryCache ?? []).map((point) => point.snapshot_id),
    );
    const additions = await unseenEquityPoints(known);
    equityHistoryCache = [...additions, ...(equityHistoryCache ?? [])];
    return equityHistoryCache;
  },
  capitalActivity: async (cursor?: string, limit = 30): Promise<Page<CapitalAction>> => {
    const result = await getJson<{ actions: CapitalAction[]; next_cursor: string | null }>(
      pagePath("/api/capital/activity", cursor, limit),
    );
    return { items: result.actions, nextCursor: result.next_cursor };
  },
  capitalActivityDetail: (id: string) =>
    getJson<CapitalActionDetail>(
      `/api/capital/activity/${encodeURIComponent(id)}`,
    ),
  assessmentRecords: (cursor?: string, limit = 30) =>
    getJson<AssessmentFeed>(pagePath("/api/assessment/records", cursor, limit)),
  latestAssessment: () =>
    getJson<AssessmentFeed>("/api/assessment/records?limit=1"),
  forecastEvaluation: () =>
    getJson<ForecastEvaluationEvidence>("/api/evaluation/forecast"),
  assessmentRecord: (id: string) =>
    getJson<AssessmentRecordDetail>(
      `/api/assessment/records/${encodeURIComponent(id)}`,
    ),
  events: async (cursor?: string, limit = 30): Promise<Page<WorldEvent>> => {
    const result = await getJson<{ events: WorldEvent[]; next_cursor: string | null }>(
      pagePath("/api/events", cursor, limit),
    );
    return { items: result.events, nextCursor: result.next_cursor };
  },
  accounts: () => getJson<Accounts>("/api/accounts"),
  resources: () => getJson<Resources>("/api/resources"),
};
