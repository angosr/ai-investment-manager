// 通用 React 钩子：实时数据、时钟、主题、连接状态。

import { useCallback, useEffect, useRef, useState } from "react";
import type { Page } from "./api/types";
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

export interface PagedLive<T> {
  items: T[];
  page: number;
  hasPrevious: boolean;
  hasNext: boolean;
  loading: boolean;
  previous: () => void;
  next: () => void;
}

/**
 * 事实时间线的游标分页。每页只保留少量 DOM，但历史始终从服务端事实库读取；
 * SSE 只刷新第一页，不会把用户正在查看的旧页清空。
 */
export function usePagedLive<T>(
  fetchPage: (cursor?: string) => Promise<Page<T>>,
  topic: RefreshTopic,
): PagedLive<T> {
  const [pages, setPages] = useState<T[][]>([]);
  const [nextCursors, setNextCursors] = useState<(string | null)[]>([]);
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(false);
  const fetchRef = useRef(fetchPage);
  const pagesRef = useRef(pages);
  const cursorsRef = useRef(nextCursors);
  const pageRef = useRef(page);
  fetchRef.current = fetchPage;
  pagesRef.current = pages;
  cursorsRef.current = nextCursors;
  pageRef.current = page;

  useEffect(() => {
    let disposed = false;
    let running = false;
    let rerun = false;
    const refreshFirst = async () => {
      // Older pages belong to one immutable cursor chain. Do not splice a newly
      // refreshed first page into that chain while the user is reading history.
      if (pageRef.current !== 0) return;
      if (running) {
        rerun = true;
        return;
      }
      do {
        running = true;
        rerun = false;
        try {
          const first = await fetchRef.current();
          if (!disposed) {
            setPages([first.items]);
            setNextCursors([first.nextCursor]);
          }
        } catch (err) {
          console.error("时间线首页获取失败", err);
        } finally {
          running = false;
        }
      } while (rerun && !disposed);
    };
    void refreshFirst();
    const unsubscribe = subscribeRefresh(topic, () => void refreshFirst());
    return () => {
      disposed = true;
      unsubscribe();
    };
  }, [topic]);

  const previous = useCallback(() => setPage((current) => Math.max(0, current - 1)), []);
  const next = useCallback(() => {
    if (loading) return;
    const currentPage = pageRef.current;
    const loaded = pagesRef.current;
    if (loaded[currentPage + 1]) {
      setPage(currentPage + 1);
      return;
    }
    const cursor = cursorsRef.current[currentPage];
    if (!cursor) return;
    setLoading(true);
    void fetchRef.current(cursor)
      .then((result) => {
        setPages((existing) => [
          ...existing.slice(0, currentPage + 1),
          result.items,
        ]);
        setNextCursors((existing) => [
          ...existing.slice(0, currentPage + 1),
          result.nextCursor,
        ]);
        if (result.items.length > 0) setPage(currentPage + 1);
      })
      .catch((err) => console.error("更早的时间线记录获取失败", err))
      .finally(() => setLoading(false));
  }, [loading]);

  const items = pages[page] ?? [];
  return {
    items,
    page,
    hasPrevious: page > 0,
    hasNext: Boolean(pages[page + 1]) || Boolean(nextCursors[page]),
    loading,
    previous,
    next,
  };
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
