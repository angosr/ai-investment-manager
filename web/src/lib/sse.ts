// 单个共享 EventSource：所有面板订阅同一「该刷新了」信号，避免多条长连接。
// EventSource 自带断线重连；这里只广播刷新与连接状态。

type Listener = () => void;
type StatusListener = (connected: boolean) => void;
export type RefreshTopic =
  | "health"
  | "capital"
  | "cycles"
  | "events"
  | "equity"
  | "accounts"
  | "resources";

const refreshListeners = new Map<RefreshTopic, Set<Listener>>();
const statusListeners = new Set<StatusListener>();
const RECONNECT_DELAY_MS = 3000;
let source: EventSource | null = null;
let connected = false;
let reconnectTimer: number | null = null;

function setConnected(value: boolean): void {
  if (connected === value) return;
  connected = value;
  statusListeners.forEach((listener) => listener(connected));
}

function scheduleReconnect(): void {
  if (reconnectTimer !== null) return;
  reconnectTimer = window.setTimeout(() => {
    reconnectTimer = null;
    ensureSource();
  }, RECONNECT_DELAY_MS);
}

function ensureSource(): void {
  if (source) return;
  source = new EventSource("/api/stream");
  source.addEventListener("refresh", (event) => {
    const topics = parseTopics((event as MessageEvent<string>).data);
    topics.forEach((topic) => refreshListeners.get(topic)?.forEach((listener) => listener()));
  });
  source.onopen = () => setConnected(true);
  source.onerror = () => {
    setConnected(false);
    // EventSource 只自动重连「瞬时」错误；服务端致命断流（重启期间 5xx/CLOSED）会永久停摆。
    // 检测到 CLOSED 时主动销毁并安排重建，否则实时刷新会静默卡死直到手动刷新页面。
    if (source && source.readyState === EventSource.CLOSED) {
      source.close();
      source = null;
      scheduleReconnect();
    }
  };
}

export function subscribeRefresh(topic: RefreshTopic, listener: Listener): () => void {
  ensureSource();
  const listeners = refreshListeners.get(topic) ?? new Set<Listener>();
  listeners.add(listener);
  refreshListeners.set(topic, listeners);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) refreshListeners.delete(topic);
  };
}

export function subscribeStatus(listener: StatusListener): () => void {
  ensureSource();
  listener(connected);
  statusListeners.add(listener);
  return () => statusListeners.delete(listener);
}

function parseTopics(data: string): RefreshTopic[] {
  try {
    const payload = JSON.parse(data) as { topics?: unknown };
    if (!Array.isArray(payload.topics)) return [];
    return payload.topics.filter(isRefreshTopic);
  } catch {
    return [];
  }
}

function isRefreshTopic(value: unknown): value is RefreshTopic {
  return (
    typeof value === "string" &&
    ["health", "capital", "cycles", "events", "equity", "accounts", "resources"].includes(
      value,
    )
  );
}
