export default function longStringTruncator(k: string, v: unknown) {
  // truncate long strings
  if ((v as any).length && (v as any).length > 100) {
    return `${(v as any).toString().slice(0, 100)}...`;
  }
  return v;
}
