export function money(value: string | number, digits = 2): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

export function pct(value: string | number, digits = 4): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return `${number.toFixed(digits)}%`;
}

export function qty(value: string | number): string {
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  return number.toLocaleString("en-US", { maximumFractionDigits: 8 });
}

export function dateTime(value: string | null): string {
  if (!value) return "-";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

export function dateTimeMs(value: string | null): string {
  if (!value) return "-";
  const date = new Date(value);
  const base = new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(date);
  return `${base}.${String(date.getMilliseconds()).padStart(3, "0")}`;
}

export function valueTone(value: string | number): "positive" | "negative" | "" {
  const number = Number(value);
  if (number > 0) return "positive";
  if (number < 0) return "negative";
  return "";
}

export function takeProfitRemaining(current: string | number, target: string | number): number {
  const currentNumber = Number(current);
  const targetNumber = Number(target);
  if (!Number.isFinite(currentNumber) || !Number.isFinite(targetNumber) || targetNumber <= 0) return NaN;
  return Math.max(targetNumber - currentNumber, 0);
}

export function takeProfitProgress(current: string | number, target: string | number): number {
  const currentNumber = Number(current);
  const targetNumber = Number(target);
  if (!Number.isFinite(currentNumber) || !Number.isFinite(targetNumber) || targetNumber <= 0) return NaN;
  return Math.min(Math.max((currentNumber / targetNumber) * 100, 0), 999);
}
