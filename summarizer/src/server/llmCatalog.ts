import {
  createCatalogResolver,
  createProviderChain,
  generateJson,
  generateText,
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

export type LivestackLLMPurpose =
  | "title-local"
  | "title-openai"
  | "title-lepton"
  | "translation-local"
  | "translation-openai"
  | "topics-local"
  | "topics-openai";

const OPENAI_CHAT_MODEL = process.env.OPENAI_CHAT_MODEL || "gpt-3.5-turbo";
const OLLAMA_TITLE_MODEL = process.env.OLLAMA_TITLE_MODEL || "llama3:instruct";
const OLLAMA_TRANSLATION_MODEL =
  process.env.OLLAMA_TRANSLATION_MODEL || "gemma3:27b";
const OLLAMA_BASE_URL = toOpenAICompatibleBaseUrl(
  process.env.OLLAMA_HOST || "http://localhost:11434"
);

function toOpenAICompatibleBaseUrl(host: string): string {
  return `${host.replace(/\/+$/, "")}/v1`.replace(/\/v1\/v1$/, "/v1");
}

function createLivestackCatalog() {
  return parseProviderCatalog({
    llmProviders: [
      {
        name: "ollama",
        baseUrl: OLLAMA_BASE_URL,
        adapter: "openai-compatible",
        models: [
          { modelName: OLLAMA_TITLE_MODEL, contextWindow: 128000 },
          { modelName: OLLAMA_TRANSLATION_MODEL, contextWindow: 128000 },
        ],
        metadata: { local: true },
      },
      {
        name: "openai",
        baseUrl: process.env.OPENAI_BASE_URL || "https://api.openai.com/v1",
        adapter: "openai-compatible",
        models: [{ modelName: OPENAI_CHAT_MODEL, contextWindow: 16385 }],
      },
      {
        name: "lepton-llama2-70b",
        baseUrl: "https://llama2-70b.lepton.run/api/v1",
        adapter: "openai-compatible",
        models: [{ modelName: "llama2-70b", contextWindow: 4096 }],
      },
      {
        name: "lepton-llama2-13b",
        baseUrl: "https://llama2-13b.lepton.run/api/v1",
        adapter: "openai-compatible",
        models: [{ modelName: "llama2-13b", contextWindow: 4096 }],
      },
    ],
    llmModels: [
      {
        id: "livestack-title",
        contextWindow: 128000,
        providers: [
          { provider: "ollama", modelName: OLLAMA_TITLE_MODEL },
          { provider: "openai", modelName: OPENAI_CHAT_MODEL },
          { provider: "lepton-llama2-70b", modelName: "llama2-70b" },
          { provider: "lepton-llama2-13b", modelName: "llama2-13b" },
        ],
      },
      {
        id: "livestack-translation",
        contextWindow: 128000,
        providers: [
          { provider: "ollama", modelName: OLLAMA_TRANSLATION_MODEL },
          { provider: "openai", modelName: OPENAI_CHAT_MODEL },
        ],
      },
      {
        id: "livestack-topics",
        contextWindow: 128000,
        providers: [
          { provider: "ollama", modelName: OLLAMA_TITLE_MODEL },
          { provider: "openai", modelName: OPENAI_CHAT_MODEL },
        ],
      },
    ],
    embeddingProviders: [],
  });
}

export const livestackLLMResolver = createCatalogResolver({
  loadCatalog: createLivestackCatalog,
  getProviderApiKeys: () => {
    const keys: Record<string, Record<string, string>> = {
      ollama: { local: "ollama" },
    };
    if (process.env.OPENAI_API_KEY) {
      keys.openai = { zigzag: process.env.OPENAI_API_KEY };
    }
    if (process.env.LEPTONAI_API_KEY) {
      keys["lepton-llama2-70b"] = { zigzag: process.env.LEPTONAI_API_KEY };
      keys["lepton-llama2-13b"] = { zigzag: process.env.LEPTONAI_API_KEY };
    }
    return keys;
  },
  getDefaultApiKeyTag: () => "local",
  getBaseLLMConfig: () => ({
    model: "livestack-title",
    provider: "ollama",
    tag: "local",
  }),
  getBaseEmbeddingConfig: () => ({
    provider: "ollama",
    modelName: "nomic-embed-text",
    tag: "local",
  }),
  getScopeConfig: ({ purpose }) => {
    switch (purpose as LivestackLLMPurpose | undefined) {
      case "title-openai":
        return { llm: { model: "livestack-title", provider: "openai", tag: "zigzag" } };
      case "title-lepton":
        return {
          llmResolver: createProviderChain([
            { model: "livestack-title", provider: "lepton-llama2-70b", tag: "zigzag" },
            { model: "livestack-title", provider: "lepton-llama2-13b", tag: "zigzag" },
          ]),
        };
      case "translation-local":
        return { llm: { model: "livestack-translation", provider: "ollama", tag: "local" } };
      case "translation-openai":
        return { llm: { model: "livestack-translation", provider: "openai", tag: "zigzag" } };
      case "topics-local":
        return { llm: { model: "livestack-topics", provider: "ollama", tag: "local" } };
      case "topics-openai":
        return { llm: { model: "livestack-topics", provider: "openai", tag: "zigzag" } };
      case "title-local":
      default:
        return { llm: { model: "livestack-title", provider: "ollama", tag: "local" } };
    }
  },
});

function configsWithFallbacks(llm: LLMConfig & { fallbackConfigs?: LLMConfig[] }) {
  return [llm, ...(llm.fallbackConfigs || [])];
}

export async function generateLivestackText(options: {
  purpose: LivestackLLMPurpose;
  messages: LivestackChatMessage[];
  parameters?: Record<string, unknown>;
}): Promise<string> {
  const llm = await livestackLLMResolver.resolveLLMConfig({
    purpose: options.purpose,
  });
  let lastError: unknown;
  for (const config of configsWithFallbacks(llm)) {
    try {
      return await generateText({
        llm: config,
        messages: options.messages as ChatMessage[],
        parameters: options.parameters,
        executor: openAICompatibleChatExecutor,
      });
    } catch (error) {
      lastError = error;
    }
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
}

export async function generateLivestackJson<T>(options: {
  purpose: LivestackLLMPurpose;
  messages: LivestackChatMessage[];
  schema?: z.ZodType<T>;
  parameters?: Record<string, unknown>;
}): Promise<T> {
  const llm = await livestackLLMResolver.resolveLLMConfig({
    purpose: options.purpose,
  });
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
