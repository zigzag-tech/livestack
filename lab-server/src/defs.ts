import { z } from "zod";

export const summarizedTitleDef = z.object({
  summarizedTitle: z.string(),
});
