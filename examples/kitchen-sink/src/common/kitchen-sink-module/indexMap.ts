import { MemoryVectorStore } from "langchain/vectorstores/memory";
import pLimit from "p-limit";
import { provisionLangChainEmbeddings } from "./provisionedLangChainModel";
// Assuming pLimitedFn is a function you've defined elsewhere for rate limiting
// and it's imported or available in this context.

const GLOBAL_INDEX_CACHE = new Map<string, MemoryVectorStore>();

const embeddings = provisionLangChainEmbeddings({
  embeddingType: "ollama",
  model: "llama3:instruct",
});

function pLimitedFn<T extends any[], R>(
  limit: number,
  fn: (...arg: T) => Promise<R>
) {
  const limiter = pLimit(limit);
  return async (...arg: T) => limiter(() => fn(...arg));
}

export const getIndexOrCreate = pLimitedFn(
  1,
  async ({
    indexName,
  }: // forceReset = false,
  {
    indexName: string;
    // forceReset?: boolean;
  }) => {
    // if (forceReset) {
    //   const vectorStore = new MemoryVectorStore(embeddings);
    //   indexMap.set(indexName, vectorStore);
    //   return;
    // }

    if (GLOBAL_INDEX_CACHE.has(indexName)) {
      return GLOBAL_INDEX_CACHE.get(indexName)!;
    } else {
      const vectorStore = new MemoryVectorStore(embeddings);
      GLOBAL_INDEX_CACHE.set(indexName, vectorStore);
      return vectorStore;
    }
  }
);

export const resetIndex = async ({ indexName }: { indexName: string }) => {
  if (GLOBAL_INDEX_CACHE.has(indexName)) {
    const vectorStore = new MemoryVectorStore(embeddings);
    GLOBAL_INDEX_CACHE.set(indexName, vectorStore);
  }
  return;
};
