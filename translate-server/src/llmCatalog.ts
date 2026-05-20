import {
  createCatalogResolver,
  generateJson,
  openAICompatibleChatExecutor,
  parseProviderCatalog,
  type ChatMessage,
  type LLMConfig,
} from "@zigzag-tech/llm-catalog";
import type { z } from "zod";

type LivestackChatMessage = {
  role: string;
  content: string;
  images?: unknown[];
};

const OPENAI_CHAT_MODEL = process.env.OPENAI_CHAT_MODEL || "gpt-3.5-turbo";
const OLLAMA_TRANSLATION_MODEL =
  process.env.OLLAMA_TRANSLATION_MODEL || "gemma3:27b";
const OLLAMA_BASE_URL = `${(process.env.OLLAMA_HOST || "http://localhost:11434").replace(
  /\/+$/,
  ""
)}/v1`.replace(/\/v1\/v1$/, "/v1");

const resolver = createCatalogResolver({
  loadCatalog: () =>
    parseProviderCatalog({
      llmProviders: [
        {
          name: "ollama",
          baseUrl: OLLAMA_BASE_URL,
          adapter: "openai-compatible",
          models: [{ modelName: OLLAMA_TRANSLATION_MODEL, contextWindow: 128000 }],
          metadata: { local: true },
        },
        {
          name: "openai",
          baseUrl: process.env.OPENAI_BASE_URL || "https://api.openai.com/v1",
          adapter: "openai-compatible",
          models: [{ modelName: OPENAI_CHAT_MODEL, contextWindow: 16385 }],
        },
      ],
      llmModels: [
        {
          id: "livestack-translation",
          contextWindow: 128000,
          providers: [
            { provider: "ollama", modelName: OLLAMA_TRANSLATION_MODEL },
            { provider: "openai", modelName: OPENAI_CHAT_MODEL },
          ],
        },
      ],
      embeddingProviders: [],
    }),
  getProviderApiKeys: () => {
    const keys: Record<string, Record<string, string>> = {
      ollama: { local: "ollama" },
    };
    if (process.env.OPENAI_API_KEY) {
      keys.openai = { zigzag: process.env.OPENAI_API_KEY };
    }
    return keys;
  },
  getDefaultApiKeyTag: () => "local",
  getBaseLLMConfig: () => ({
    model: "livestack-translation",
    provider: "ollama",
    tag: "local",
  }),
  getBaseEmbeddingConfig: () => ({
    provider: "ollama",
    modelName: "nomic-embed-text",
    tag: "local",
  }),
  getScopeConfig: ({ purpose }) =>
    purpose === "translation-openai"
      ? { llm: { model: "livestack-translation", provider: "openai", tag: "zigzag" } }
      : { llm: { model: "livestack-translation", provider: "ollama", tag: "local" } },
});

function configsWithFallbacks(llm: LLMConfig & { fallbackConfigs?: LLMConfig[] }) {
  return [llm, ...(llm.fallbackConfigs || [])];
}

export async function generateTranslationJson<T>(options: {
  purpose: "translation-local" | "translation-openai";
  messages: LivestackChatMessage[];
  schema?: z.ZodType<T>;
  parameters?: Record<string, unknown>;
}): Promise<T> {
  const llm = await resolver.resolveLLMConfig({ purpose: options.purpose });
  let lastError: unknown;
  for (const config of configsWithFallbacks(llm)) {
    const response = await generateJson({
      llm: config,
      messages: options.messages as ChatMessage[],
      schema: options.schema,
      parameters: options.parameters,
      executor: openAICompatibleChatExecutor,
    });
    if (response.result.status === "success") {
      return response.result.content;
    }
    lastError = response.result.error;
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}
