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
  const rendered = money(num);
  return num > 0 ? `+${rendered}` : rendered;
}

export function isPositive(value: string | null): boolean {
  return value !== null && Number(value) >= 0;
}

export function gib(bytes: number): string {
  return (bytes / 1024 ** 3).toFixed(1);
}

export function pct(value: number | null): string {
  return value === null ? "—" : `${decimal(value, 1)}%`;
}

export function fixed(value: string | null, digits = 2): string {
  return decimal(value, digits);
}

type NumericValue = string | number | null | undefined;

/** Human-readable decimal; API/audit values remain untouched. */
export function decimal(
  value: NumericValue,
  maximumFractionDigits = 2,
  minimumFractionDigits = 0,
): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return String(value);
  const normalized = Object.is(parsed, -0) ? 0 : parsed;
  return normalized.toLocaleString("zh-CN", {
    maximumFractionDigits,
    minimumFractionDigits,
  });
}

export function money(value: NumericValue): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return decimal(value);
  return decimal(parsed, Math.abs(parsed) > 0 && Math.abs(parsed) < 1 ? 4 : 2);
}

export function price(value: NumericValue): string {
  return decimal(value, 2);
}

export function quantity(value: NumericValue): string {
  return decimal(value, 6);
}

export function bps(value: NumericValue): string {
  return decimal(value, 2);
}

export function probability(value: NumericValue): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${decimal(parsed * 100, 1)}%` : decimal(value);
}

export function fractionPercent(value: NumericValue, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—";
  const parsed = Number(value);
  return Number.isFinite(parsed) ? `${decimal(parsed * 100, digits)}%` : decimal(value);
}

export function ratio(value: NumericValue): string {
  return decimal(value, 3);
}

export function countdown(iso: string): string {
  const remaining = new Date(iso).getTime() - Date.now();
  if (remaining <= 0) return "已到期";
  const minutes = Math.floor(remaining / 60000);
  const hours = Math.floor(minutes / 60);
  return hours > 0 ? `${hours} 时 ${minutes % 60} 分` : `${minutes} 分`;
}
