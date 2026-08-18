// 展示层格式化：只把后端字符串/数字转成好看的显示，不做业务计算。

export function hhmm(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return [date.getUTCHours(), date.getUTCMinutes()]
    .map((part) => String(part).padStart(2, "0"))
    .join(":");
}

export function signed(value: string | null): string {
  if (value === null || value === "") return "—";
  const num = Number(value);
  if (Number.isNaN(num)) return value;
  return num > 0 ? `+${value}` : value;
}

export function isPositive(value: string | null): boolean {
  return value !== null && Number(value) >= 0;
}

export function gib(bytes: number): string {
  return (bytes / 1024 ** 3).toFixed(1);
}

export function pct(value: number | null): string {
  return value === null ? "—" : `${value}%`;
}

export function fixed(value: string | null, digits = 2): string {
  if (value === null || value === "") return "—";
  const num = Number(value);
  return Number.isNaN(num) ? value : num.toFixed(digits);
}

export function countdown(iso: string): string {
  const remaining = new Date(iso).getTime() - Date.now();
  if (remaining <= 0) return "已到期";
  const minutes = Math.floor(remaining / 60000);
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours} 时 ${minutes % 60} 分` : `${minutes} 分`;
}
