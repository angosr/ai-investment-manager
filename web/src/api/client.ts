// 只读 API 客户端：全部 GET，无写操作。

import type {
  Accounts,
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

export const api = {
  health: () => getJson<Health>("/api/health"),
  capital: () => getJson<CapitalOverview>("/api/capital"),
  capitalActivity: () =>
    getJson<{ actions: CapitalAction[] }>("/api/capital/activity?limit=100"),
  assessmentCycles: () =>
    getJson<{ cycles: CycleRow[] }>("/api/assessment/cycles?limit=100"),
  assessmentCycle: (id: string) =>
    getJson<CycleDetail>(`/api/assessment/cycles/${encodeURIComponent(id)}`),
  cycles: () => getJson<{ cycles: CycleRow[] }>("/api/cycles"),
  cycle: (id: string) => getJson<CycleDetail>(`/api/cycles/${encodeURIComponent(id)}`),
  events: () => getJson<{ events: WorldEvent[] }>("/api/events"),
  positions: () => getJson<{ positions: Position[] }>("/api/positions"),
  equity: (window: string) => getJson<Equity>(`/api/equity?window=${encodeURIComponent(window)}`),
  accounts: () => getJson<Accounts>("/api/accounts"),
  resources: () => getJson<Resources>("/api/resources"),
};
