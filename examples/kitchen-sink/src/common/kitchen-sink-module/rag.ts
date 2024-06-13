import {
  UserMessage,
  BotStatusMessage,
  BotMessage,
  TAGS,
  IndexContent,
} from "./defs";
import { JobSpec, LiveWorker } from "@livestack/core";
import { z } from "zod";
import { parseURLMarkdownText } from "./parseURLMarkdownText";
import { getRecursiveCharacterTextSplitter } from "@livestack/lab-internal-server";
import { getIndexOrCreate } from "./indexMap";
import { formatDocumentsAsString } from "langchain/util/document";
import { StringOutputParser } from "@langchain/core/output_parsers";
import { v4 } from "uuid";
import { provisionedLangChainModel } from "./provisionedLangChainModel";
import { Document } from "langchain/document";
import { HumanMessage } from "@langchain/core/messages";

const systemPrompt = `Your task is to assist users by providing accurate, informative, and engaging responses to their questions based on the context provided.`;

export const splitTextToChunks = async (text: string, chunkSize = 200) => {
  // split body text into chunks and index each chunk
  const splitter = getRecursiveCharacterTextSplitter({ chunkSize });
  const chunks = await splitter.createDocuments([text]);
  return chunks;
};

export function getRAGWorkerDefs() {
  const indexJobSpec = JobSpec.define({
    name: "index-job",
    input: IndexContent,
    output: {
      default: z.string(),
      "text-status": z.string(),
      "audio-status": z.string(),
    },
  });

  const queryJobSpec = JobSpec.define({
    name: "query-job",
    input: { [TAGS.USER_MESSAGE]: UserMessage },
    output: {
      [TAGS.BOT_MESSAGE]: BotMessage,
      [TAGS.BOT_STATUS]: BotStatusMessage,
    },
  });

  const indexWorkerDef = LiveWorker.define({
    jobSpec: indexJobSpec,
    processor: async ({ input, output, logger }) => {
      const index = await getIndexOrCreate({ indexName: "rag_index" });
      const audioStreamIndex = await getIndexOrCreate({
        indexName: "audio_index",
      });

      for await (const data of input) {
        if (data.source === "urls") {
          for (const url of data.urls) {
            logger.info("Indexing: " + url);
            // await output("text-status").emit(`indexing: ${url}`);
            // parse URL to body and title
            try {
              const parsedContent = await parseURLMarkdownText(url);
              const chunks = await splitTextToChunks(parsedContent.body);

              await index.addDocuments(
                chunks.map((chunk, idx) => ({
                  pageContent: chunk.pageContent,
                  metadata: {
                    ...chunk.metadata,
                    url,
                    title: parsedContent.title,
                    id: `${url}__${idx}`,
                  },
                }))
              );
              chunks.forEach(async ({ pageContent }) => {
                output.emit(pageContent);
              });
              await output("text-status").emit(`indexed: ${url}`);
              logger.info("Indexed: " + url + ".");
            } catch (e) {
              logger.error(`error indexing url ${url}: ${e}`);
            }
          }
        } else if (data.source === "text") {
          logger.info("Indexing: " + data.content.slice(0, 10));
          try {
            const chunks = await splitTextToChunks(data.content);
            await index!.addDocuments(chunks);
            chunks.forEach(async ({ pageContent }) => {
              output.emit(pageContent);
            });
            await output("text-status").emit(
              `indexed: ${data.content.slice(0, 10)}...`
            );
          } catch (e) {
            logger.error(`error indexing text: ${e}`);
          }
        } else {
          // data.source === "audio-stream"
          try {
            await audioStreamIndex!.addDocuments([
              new Document({ pageContent: data.content }),
            ]);

            await output("audio-status").emit(
              `indexed: ${data.content.slice(0, 10)}`
            );
          } catch (e) {
            logger.error(`error indexing content: ${e}`);
          }
        }
      }
    },
  });

  const queryWorkerDef = LiveWorker.define({
    jobSpec: queryJobSpec,
    processor: async ({ input, output, logger, invoke }) => {
      const model = provisionedLangChainModel({
        model: "llama3:instruct",
        llmType: "ollama",
        invoke,
      });

      const index = await getIndexOrCreate({ indexName: "rag_index" });
      const audioStreamIndex = await getIndexOrCreate({
        indexName: "audio_index",
      });

      await output(TAGS.BOT_MESSAGE).emit({
        sender: "bot",
        content: "Hello! How can I help you with your provided content?",
        complete: true,
        botMsgGroupId: v4(),
      });

      for await (const data of input) {
        logger.info("chat received: " + data.content.substring(0, 100));
        await output(TAGS.BOT_STATUS).emit({
          sender: "bot-status",
          content: "querying",
        });
        const retriever =
          data.target === "text"
            ? index!.asRetriever()
            : audioStreamIndex!.asRetriever();

        const context = await retriever
          .pipe(formatDocumentsAsString)
          .invoke(data.content);
        const prompt = `${systemPrompt}
CONTEXT:
${context}

QUESTION:
${data.content}`;

        const botMsgGroupIdForCurrData = v4();
        console.log(prompt);
        const response = await model.invoke([new HumanMessage(prompt)]);
        // for await (const chunk of await model.stream(data.content)) {
        //   await output(TAGS.BOT_MESSAGE).emit({
        //     sender: "bot",
        //     content: chunk,
        //     complete: false,
        //     botMsgGroupId: botMsgGroupIdForCurrData,
        //   });
        // }
        const responseStr = await new StringOutputParser().invoke(response);
        await output(TAGS.BOT_MESSAGE).emit({
          sender: "bot",
          content: responseStr,
          complete: true,
          botMsgGroupId: botMsgGroupIdForCurrData,
        });

        await output(TAGS.BOT_STATUS).emit({
          sender: "bot-status",
          content: "standby",
        });
      }
    },
  });

  return { indexJobSpec, queryJobSpec, indexWorkerDef, queryWorkerDef };
}
