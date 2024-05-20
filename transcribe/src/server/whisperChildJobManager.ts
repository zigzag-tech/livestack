import { JobManager } from "@livestack/core";
import { transcriptionSelectorSpec } from "./llmUtils";

const queuedJobs = new Map<string, JobManager<any, any, any, any, any>>();

export const getQueuedJobOrCreate = async ({
  whisperType,
}: {
  whisperType: "openai" | "local";
}) => {
  if (!queuedJobs.has(whisperType)) {
    const newJ = await transcriptionSelectorSpec.enqueueJob({
      jobOptions: { whisperType },
    });
    queuedJobs.set(whisperType, newJ);
    return newJ;
  } else {
    return queuedJobs.get(whisperType)!;
  }
};
