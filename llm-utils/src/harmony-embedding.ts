import { EmbeddingCache } from './EmbeddingCache';

/**
 * THE embedder for the fleet: google/embeddinggemma-2 at 512 dimensions,
 * served by the Livestack Harmony gateway. Nothing else (Ollama, TEI, cloud
 * providers, MiniLM) is supported; vectors from different spaces must never be
 * mixed in one store, so model and dimension are fixed here.
 *
 * Request shape (see HARMONY.md): the `model` field must START with
 * `require:` -- a `harmony/` prefix or a bare `class=embed` makes the gateway
 * answer 400 ("N embedding models meet this need").
 */
export const HARMONY_EMBED_MODEL = 'google/embeddinggemma-2';
export const HARMONY_EMBED_DIM = 512;
export const HARMONY_EMBED_REQUIRE = `require:class=embed,model=${HARMONY_EMBED_MODEL},dim=${HARMONY_EMBED_DIM}`;
/** Gateway slot is 2048 tokens; longer inputs are refused with HTTP 413. */
export const HARMONY_EMBED_MAX_CHARS = 1800;
export const HARMONY_EMBED_BATCH = 16;
const DEFAULT_HARMONY_URL = 'http://100.64.0.18:8188';
const ATTEMPTS = 3;

export class HarmonyEmbeddingError extends Error {
    constructor(message: string) {
        super(message);
        this.name = 'HarmonyEmbeddingError';
    }
}

function harmonyUrl(): string {
    return (process.env.HARMONY_URL || DEFAULT_HARMONY_URL).replace(/\/+$/, '').replace(/\/v1$/, '');
}

async function postBatch(url: string, input: string[]): Promise<number[][]> {
    let lastError: unknown;
    for (let attempt = 1; attempt <= ATTEMPTS; attempt++) {
        try {
            const response = await fetch(`${url}/v1/embeddings`, {
                method: 'POST',
                headers: { 'content-type': 'application/json', authorization: 'Bearer harmony' },
                body: JSON.stringify({ model: HARMONY_EMBED_REQUIRE, input }),
                signal: AbortSignal.timeout(Number(process.env.HARMONY_EMBED_TIMEOUT_MS ?? 1_800_000)),
            });
            if (response.status === 503) {
                lastError = new HarmonyEmbeddingError(`harmony /v1/embeddings returned 503`);
                continue;
            }
            if (!response.ok) {
                const text = await response.text().catch(() => '');
                throw new HarmonyEmbeddingError(`harmony /v1/embeddings returned ${response.status}: ${text.slice(-500)}`);
            }
            const body = (await response.json()) as {
                model?: string;
                data?: Array<{ embedding?: number[]; index?: number }>;
            };
            if (body.model !== HARMONY_EMBED_MODEL) {
                throw new HarmonyEmbeddingError(`harmony served model ${JSON.stringify(body.model)}, expected ${HARMONY_EMBED_MODEL}`);
            }
            const vectors = [...(body.data ?? [])]
                .sort((a, b) => (a.index ?? 0) - (b.index ?? 0))
                .map((d) => d.embedding ?? []);
            if (vectors.length !== input.length || vectors.some((v) => v.length !== HARMONY_EMBED_DIM)) {
                throw new HarmonyEmbeddingError(
                    `expected ${input.length} vectors of dim ${HARMONY_EMBED_DIM}, got ${vectors.length} of dims [${vectors.map((v) => v.length).join(',')}]`,
                );
            }
            return vectors;
        } catch (error) {
            // Named errors (4xx, wrong model, wrong shape) are final; socket/timeout errors are retried.
            if (error instanceof HarmonyEmbeddingError && !/returned 503/.test(error.message)) throw error;
            lastError = error;
        }
    }
    throw new HarmonyEmbeddingError(
        `harmony embeddings failed after ${ATTEMPTS} attempts: ${lastError instanceof Error ? lastError.message : String(lastError)}`,
    );
}

/**
 * Embed texts through the Harmony gateway: one 512-d vector per text, order
 * preserved. Texts are clipped to HARMONY_EMBED_MAX_CHARS and sent in batches
 * of HARMONY_EMBED_BATCH. Throws HarmonyEmbeddingError on any failure; never
 * returns empty or zero vectors.
 */
export async function embedTexts(texts: string[]): Promise<number[][]> {
    const url = harmonyUrl();
    const out: number[][] = [];
    for (let i = 0; i < texts.length; i += HARMONY_EMBED_BATCH) {
        const batch = texts.slice(i, i + HARMONY_EMBED_BATCH).map((t) => t.slice(0, HARMONY_EMBED_MAX_CHARS));
        out.push(...(await postBatch(url, batch)));
    }
    return out;
}

// Own cache directory: the default `.embedding-cache` may hold vectors of the
// retired embedders (different dimension/space), which must never be read back.
const cache = new EmbeddingCache({
    embeddingFn: async (text) => (await embedTexts([text]))[0],
    cacheDir: '.embedding-cache/embeddinggemma-2-512',
});

/** Cached single-text embedding (Harmony, embeddinggemma-2, 512-d). */
export async function generateEmbedding(text: string): Promise<number[]> {
    return cache.getEmbedding(text);
}

export function getCosineSimilarity(embedding1: number[], embedding2: number[]): number {
    return cache.getCosineSimilarity(embedding1, embedding2);
}
