import { z } from "zod";

export const incrementInput = z.object({ action: z.literal("increment") });
export const incrementOutput = z.object({ count: z.number() });
export const INCREMENTER = "incrementer";
