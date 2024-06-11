import { z } from "zod";

export const supportedLangs = z.enum([
  "English",
  "Chinese",
  "French",
  "Spanish",
  "German",
  "Japanese",
]);
