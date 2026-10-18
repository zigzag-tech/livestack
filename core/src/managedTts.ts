/** Actual managed Harmony TTS job controls. A transport timeout is unknown, never cancellation. */
export type IndexTts25Controls = {
  /** Acoustic length-regulator multiplier; >1 is slower. Not exact seconds. */
  durationFactor: number;
  emotionAlpha: number;
  /** happy, angry, sad, afraid, disgusted, melancholic, surprised, calm */
  emotionVector?: [number, number, number, number, number, number, number, number];
  /** Between native internal segments only. Timeline pauses are separate. */
  intervalSilenceMs: number;
  maxTextTokensPerSegment: number;
  seed: number;
};
export type ManagedTtsRequest = {
  jobId: string; ownerId: string; text: string;
  language: "en" | "zh" | "ja" | "es" | "ar";
  voiceId: string; referenceSha256: string; controls: IndexTts25Controls;
};
export type ManagedTtsStatus = {
  jobId: string;
  state: "queued" | "admitting" | "waiting_capacity" | "running" | "succeeded" | "failed" | "cancelled" | "held";
  requestSha256: string;
  receipt: Record<string, unknown>;
};
export function createManagedHarmonyTtsClient(input: {
  baseUrl: string; readToken: () => Promise<string>; controlTimeoutMs?: number;
  fetch?: typeof globalThis.fetch;
}) {
  const base = new URL(input.baseUrl);
  if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.search || base.hash)
    throw new Error("Explicit managed TTS endpoint required");
  const timeout = input.controlTimeoutMs ?? 20000;
  if (!Number.isInteger(timeout) || timeout < 1 || timeout > 20000) throw new Error("Invalid control deadline");
  const transport = input.fetch ?? globalThis.fetch;
  const route = (id: string) => {
    if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,100}$/.test(id)) throw new Error("Invalid owned TTS job ID");
    return `/v1/tts/jobs/${id}`;
  };
  async function call(path: string, method: string, body: unknown, signal?: AbortSignal, binary = false) {
    const controller = new AbortController();
    const abort = () => controller.abort(signal?.reason);
    if (signal?.aborted) abort(); else signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(() => controller.abort(new Error("Managed TTS control deadline; execution state unknown")), timeout);
    try {
      const token = await input.readToken();
      if (controller.signal.aborted) throw controller.signal.reason;
      if (typeof token !== "string" || token.trim().length < 32) throw new Error("Managed TTS credential absent");
      const response = await transport(new URL(path, base), {
        method, signal: controller.signal,
        headers: { Authorization: `Bearer ${token.trim()}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
      if (!response.ok) throw new Error(`Managed TTS ${method} ${path} refused HTTP ${response.status}`);
      return binary ? new Uint8Array(await response.arrayBuffer()) : await response.json();
    } finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
  }
  return {
    capabilities: (signal?: AbortSignal) => call("/v1/tts/capabilities", "GET", undefined, signal),
    submit: (request: ManagedTtsRequest, signal?: AbortSignal) => call("/v1/tts/jobs", "POST", request, signal),
    status: async (id: string, signal?: AbortSignal): Promise<ManagedTtsStatus> => {
      const value = await call(route(id), "GET", undefined, signal);
      if (!value || value.jobId !== id || typeof value.requestSha256 !== "string" || !value.receipt)
        throw new Error("Managed TTS status identity refused");
      return value as ManagedTtsStatus;
    },
    cancel: (id: string, signal?: AbortSignal) => call(`${route(id)}/cancel`, "POST", undefined, signal),
    audio: async (id: string, signal?: AbortSignal): Promise<Uint8Array> =>
      await call(`${route(id)}/artifacts/audio.wav`, "GET", undefined, signal, true) as Uint8Array,
  };
}
