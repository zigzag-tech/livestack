import { JobManager } from "@livestack/core";
import { llmSelectorSpec } from "./llmUtils";

const queuedJobs = new Map<string, JobManager<any, any, any, any, any>>();

export const getQueuedJobOrCreate = async ({
  llmType,
}: {
  llmType: "openai" | "ollama";
}) => {
  if (!queuedJobs.has(llmType)) {
    const newJ = await llmSelectorSpec.enqueueJob({
      jobOptions: { llmType },
    });
    queuedJobs.set(llmType, newJ);
    return newJ;
  } else {
    return queuedJobs.get(llmType)!;
  }
};
