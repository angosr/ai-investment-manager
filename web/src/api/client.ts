// 只读 API 客户端：全部 GET，无写操作。

import type {
  Accounts,
  AssessmentFeed,
  AssessmentRecordDetail,
  CapitalAction,
  CapitalOverview,
  CycleDetail,
  CycleRow,
  Equity,
  Health,
  Position,
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

function pagePath(path: string, before?: string, limit = 30): string {
  const query = new URLSearchParams({ limit: String(limit) });
  if (before) query.set("before", before);
  return `${path}?${query.toString()}`;
}

export const api = {
  health: () => getJson<Health>("/api/health"),
  capital: () => getJson<CapitalOverview>("/api/capital"),
  capitalActivity: (before?: string, limit = 30) =>
    getJson<{ actions: CapitalAction[] }>(pagePath("/api/capital/activity", before, limit)),
  assessmentCycles: (before?: string, limit = 30) =>
    getJson<{ cycles: CycleRow[] }>(pagePath("/api/assessment/cycles", before, limit)),
  assessmentCycle: (id: string) =>
    getJson<CycleDetail>(`/api/assessment/cycles/${encodeURIComponent(id)}`),
  assessmentRecords: (before?: string, limit = 30) =>
    getJson<AssessmentFeed>(pagePath("/api/assessment/records", before, limit)),
  latestAssessment: () =>
    getJson<AssessmentFeed>("/api/assessment/records?limit=1"),
  assessmentRecord: (id: string) =>
    getJson<AssessmentRecordDetail>(
      `/api/assessment/records/${encodeURIComponent(id)}`,
    ),
  cycles: (before?: string, limit = 30) =>
    getJson<{ cycles: CycleRow[] }>(pagePath("/api/cycles", before, limit)),
  cycle: (id: string) => getJson<CycleDetail>(`/api/cycles/${encodeURIComponent(id)}`),
  events: (before?: string, limit = 30) =>
    getJson<{ events: WorldEvent[] }>(pagePath("/api/events", before, limit)),
  positions: () => getJson<{ positions: Position[] }>("/api/positions"),
  equity: (window: string) => getJson<Equity>(`/api/equity?window=${encodeURIComponent(window)}`),
  accounts: () => getJson<Accounts>("/api/accounts"),
  resources: () => getJson<Resources>("/api/resources"),
};
