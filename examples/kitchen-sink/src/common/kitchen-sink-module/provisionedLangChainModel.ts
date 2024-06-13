import { OllamaEmbeddings } from "@langchain/community/embeddings/ollama";
import { JobSpec, LiveJob } from "@livestack/core";
import { ChatOllama } from "@langchain/community/chat_models/ollama";
import {
  SimpleChatModel,
  type BaseChatModelParams,
} from "@langchain/core/language_models/chat_models";
import { CallbackManagerForLLMRun } from "@langchain/core/callbacks/manager";
import { Embeddings, EmbeddingsParams } from "@langchain/core/embeddings";
import { z } from "zod";
import {
  HumanMessage,
  SystemMessage,
  AIMessage,
} from "@langchain/core/messages";
import type { BaseMessage } from "@langchain/core/messages";

export function provisionedLangChainModel({
  model: modelName,
  llmType,
  invoke,
}: {
  model: string;
  llmType: "ollama";
  invoke: typeof LiveJob.prototype.invoke;
}) {
  const model = new LivestackProvisionedChatModel({
    invoke,
    llmType,
    model: modelName,
  });

  return model;
}

export function provisionLangChainEmbeddings({
  model: modelName,
  embeddingType,
}: // invoke,
{
  model: string;
  embeddingType: "ollama";
  // invoke: typeof LiveJob.prototype.invoke;
}) {
  const model = new LivestackProvisionedEmbeddingModel({
    // invoke,
    embeddingType: embeddingType,
    model: modelName,
  });

  return model;
}

class LivestackProvisionedEmbeddingModel extends Embeddings {
  // private readonly _livestackInvoke: typeof LiveJob.prototype.invoke;
  private readonly __embeddingType: string;
  private readonly _model: string;

  constructor(fields: {
    // invoke: typeof LiveJob.prototype.invoke;
    embeddingType: string;
    model: string;
  }) {
    super({});
    // this._livestackInvoke = fields.invoke;
    this.__embeddingType = fields.embeddingType as "ollama";
    this._model = fields.model;
  }

  async embedDocuments(documents: string[]): Promise<number[][]> {
    const { input, output } = await langchainEmbeddingsSpec.enqueueJob({
      jobOptions: {
        embeddingType: this.__embeddingType as "ollama",
        model: this._model,
      },
    });
    input("documents").feed(documents);
    const data = await output("documents").nextValue();
    if (!data) {
      console.error("Output is null. Input: ", JSON.stringify(documents));
      throw new Error("Output is null");
    }

    return data.data;
  }
  async embedQuery(document: string): Promise<number[]> {
    const { input, output } = await langchainEmbeddingsSpec.enqueueJob({
      jobOptions: {
        embeddingType: this.__embeddingType as "ollama",
        model: this._model,
      },
    });
    input("query").feed(document);
    const data = await output("query").nextValue();
    if (!data) {
      console.error("Output is null. Input: ", JSON.stringify(document));
      throw new Error("Output is null");
    }

    return data.data;
  }
}

const langchainEmbeddingsSpec = new JobSpec({
  name: "langchain-embeddings",
  input: {
    documents: z.array(z.string()),
    query: z.string(),
  },
  output: {
    documents: z.array(z.array(z.number())),
    query: z.array(z.number()),
  },
  jobOptions: z.object({
    embeddingType: z.literal("ollama"),
    model: z.string(),
  }),
});

export const langchainEmbeddingsWorkerDef =
  langchainEmbeddingsSpec.defineWorker({
    autostartWorker: false,
    processor: async ({ input, output, jobOptions }) => {
      const { model: modelName, embeddingType } = jobOptions;
      if (embeddingType !== "ollama") {
        throw new Error("Invalid embeddingType");
      }

      const model = new OllamaEmbeddings({
        model: modelName,
        baseUrl: "http://localhost:11434",
      });

      for await (const val of input.merge("documents", "query")) {
        if (val.tag === "documents") {
          const result = await model.embedDocuments(val.data);
          await output("documents").emit(result);
        } else if (val.tag === "query") {
          const result = await model.embedQuery(val.data);
          await output("query").emit(result);
        } else {
          throw new Error("Invalid tag: " + (val as any).tag);
        }

        // break for now
        break;
      }
    },
  });

class LivestackProvisionedChatModel extends SimpleChatModel<BaseChatModelParams> {
  private readonly _livestackInvoke: typeof LiveJob.prototype.invoke;
  private readonly __llmType: string;
  private readonly _model: string;

  constructor(fields: {
    invoke: typeof LiveJob.prototype.invoke;
    llmType: string;
    model: string;
  }) {
    super({});
    this._livestackInvoke = fields.invoke;
    this.__llmType = fields.llmType;
    this._model = fields.model;
  }
  async _call(
    messages: BaseMessage[],
    options: this["ParsedCallOptions"],
    runManager?: CallbackManagerForLLMRun | undefined
  ): Promise<string> {
    console.log("invoking", messages, options);
    const serializedMessages = messages.map((m) => m.toDict());

    const r = await this._livestackInvoke({
      spec: langchainChatModelSpec,
      jobOptions: {
        llmType: this._llmType() as "ollama",
        model: this._model,
      },
      inputTag: "default",
      inputData: {
        messages: serializedMessages,
        options,
        // runManager,
      },
      outputTag: "default",
    });

    return r;
  }

  _llmType(): string {
    return this.__llmType;
  }
}

const langchainChatModelSpec = JobSpec.define({
  name: "langchain-chat-model",
  input: z.object({
    messages: z.array(z.any()),
    options: z.record(z.string(), z.any()),
  }),
  output: z.string(),
  jobOptions: z.object({
    llmType: z.literal("ollama"),
    model: z.string(),
  }),
});

export const langchainChatModelWorkerDef = langchainChatModelSpec.defineWorker({
  autostartWorker: false,
  processor: async ({ input, output, jobOptions, invoke }) => {
    const { model: modelName, llmType } = jobOptions;
    if (llmType !== "ollama") {
      throw new Error("Invalid llmType");
    }

    const model = new ChatOllama({
      model: modelName,
      baseUrl: "http://localhost:11434",
    });

    for await (const { messages, options } of input) {
      const deserializedMessages = messages.map((m: any) =>
        m.type === "human"
          ? new HumanMessage(m.data)
          : m.type === "ai"
          ? new AIMessage(m.data)
          : new SystemMessage(m.data)
      );
      // console.log(deserializedMessages);
      const result = await model._call(deserializedMessages, options);
      await output.emit(result);

      // break for now
      break;
    }
  },
});
