// 单个共享 EventSource：所有面板订阅同一「该刷新了」信号，避免多条长连接。
// EventSource 自带断线重连；这里只广播刷新与连接状态。

type Listener = () => void;
type StatusListener = (connected: boolean) => void;

const refreshListeners = new Set<Listener>();
const statusListeners = new Set<StatusListener>();
let source: EventSource | null = null;
let connected = false;

function setConnected(value: boolean): void {
  if (connected === value) return;
  connected = value;
  statusListeners.forEach((listener) => listener(connected));
}

function ensureSource(): void {
  if (source) return;
  source = new EventSource("/api/stream");
  source.addEventListener("refresh", () => refreshListeners.forEach((listener) => listener()));
  source.onopen = () => setConnected(true);
  source.onerror = () => setConnected(false);
}

export function subscribeRefresh(listener: Listener): () => void {
  ensureSource();
  refreshListeners.add(listener);
  return () => refreshListeners.delete(listener);
}

export function subscribeStatus(listener: StatusListener): () => void {
  ensureSource();
  listener(connected);
  statusListeners.add(listener);
  return () => statusListeners.delete(listener);
}
