import { z } from "zod";
import { supportedLangs } from "../../examples/speech-app/src/common/supportedLangs";

export const translationInputSchema = z.object({
  text: z.string(),
  toLang: supportedLangs,
});

export const translationOutputSchema = z.object({
  translated: z.string(),
});
