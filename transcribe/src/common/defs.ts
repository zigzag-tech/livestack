import { z } from "zod";

export const speechChunkToTextInput = z.object({
  wavb64Str: z.string(),
});

export const speechChunkToTextOutput = z.object({
  transcript: z.string(),
});

export const rawPCMInput = z.object({
  rawPCM64Str: z.string(),
});
