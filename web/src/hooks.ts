// 通用 React 钩子：实时数据、时钟、主题、连接状态。

import { useCallback, useEffect, useRef, useState } from "react";
import { subscribeRefresh, subscribeStatus } from "./lib/sse";
import type { RefreshTopic } from "./lib/sse";

/**
 * 首次拉取 + 每次 SSE 刷新信号重取；`deps` 变化时立即重取（如切换权益窗口）。
 * 出错时保留上次数据（失败关闭：不清空、不假装最新），全局连接状态由 useConnected 呈现。
 */
export function useLive<T>(
  fetcher: () => Promise<T>,
  topic: RefreshTopic,
  deps: unknown[] = [],
): T | null {
  const [data, setData] = useState<T | null>(null);
  const fetcherRef = useRef(fetcher);
  const lastErrorRef = useRef<string | null>(null);
  fetcherRef.current = fetcher;

  useEffect(() => {
    let disposed = false;
    let loading = false;
    let rerun = false;
    const load = async () => {
      if (loading) {
        rerun = true;
        return;
      }
      do {
        loading = true;
        rerun = false;
        try {
          const next = await fetcherRef.current();
          if (!disposed) setData(next);
          lastErrorRef.current = null;
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          if (message !== lastErrorRef.current) console.error("观测台数据获取失败", err);
          lastErrorRef.current = message;
        } finally {
          loading = false;
        }
      } while (rerun && !disposed);
    };
    void load();
    const unsubscribe = subscribeRefresh(topic, () => void load());
    return () => {
      disposed = true;
      unsubscribe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topic, ...deps]);

  return data;
}

export function useClock(): string {
  const [text, setText] = useState(() => formatUtc(new Date()));
  useEffect(() => {
    const id = window.setInterval(() => setText(formatUtc(new Date())), 1000);
    return () => window.clearInterval(id);
  }, []);
  return text;
}

function formatUtc(date: Date): string {
  return [date.getUTCHours(), date.getUTCMinutes(), date.getUTCSeconds()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

export type Theme = "system" | "light" | "dark";

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>("system");
  useEffect(() => {
    const root = document.documentElement;
    if (theme === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", theme);
  }, [theme]);

  const toggle = useCallback(() => {
    setTheme((current) => {
      const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
      const isDark = current === "dark" || (current === "system" && prefersDark);
      return isDark ? "light" : "dark";
    });
  }, []);

  return [theme, toggle];
}

export function useConnected(): boolean {
  const [connected, setConnected] = useState(false);
  useEffect(() => subscribeStatus(setConnected), []);
  return connected;
}
