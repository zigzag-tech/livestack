import { z } from "zod";

export const splittingStatsSchema = z.object({
  numCharsInCache: z.number(),
  total: z.number(),
});
