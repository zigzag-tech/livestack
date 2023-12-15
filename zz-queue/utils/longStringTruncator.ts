export default function longStringTruncator(k: string, v: unknown) {
  // truncate long strings
  if (typeof v === "string" && v.length > 100) {
    return `${v.slice(0, 100)}...`;
  }
  return v;
}
