import { z } from "zod";

export const supportedLangs = z.enum([
  "English",
  "Chinese",
  "French",
  "Spanish",
  "German",
  "Japanese",
]);

export const translationInputSchema = z.object({
  text: z.string(),
  toLang: supportedLangs,
});

export const translationOutputSchema = z.object({
  translated: z.string(),
});
