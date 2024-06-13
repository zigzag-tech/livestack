import { z } from "zod";

export const QueryResponse = z.object({
  type: z.union([z.literal("responses"), z.literal("bot_greetings")]),
  content: z.string(),
});
export type QueryResponse = z.infer<typeof QueryResponse>;

const MessageBase = z.object({ content: z.string() });
export const UserMessage = MessageBase.extend({
  sender: z.literal("user"),
  target: z.union([z.literal("text"), z.literal("audio")]),
});
export type UserMessage = z.infer<typeof UserMessage>;

export const BotMessage = MessageBase.extend({
  sender: z.literal("bot"),
  complete: z.boolean(),
  botMsgGroupId: z.string(),
});
export type BotMessage = z.infer<typeof BotMessage>;

export const BotStatusMessage = MessageBase.extend({
  sender: z.literal("bot-status"),
  content: z.union([z.literal("querying"), z.literal("standby")]),
});
export type BotStatusMessage = z.infer<typeof BotStatusMessage>;

export const TAGS = {
  BOT_MESSAGE: "bot-message" as const,
  BOT_STATUS: "bot-status" as const,
  USER_MESSAGE: "user-message" as const,
};

export const IndexContent = z.union([
  z.object({ source: z.literal("urls"), urls: z.array(z.string()) }),
  z.object({ source: z.literal("audio-stream"), content: z.string() }),
  z.object({ source: z.literal("text"), content: z.string() }),
]);
export type IndexContent = z.infer<typeof IndexContent>;

export type TimestampWrappedMessage = {
  data: UserMessage | BotMessage | BotStatusMessage;
  timestamp: number;
};
