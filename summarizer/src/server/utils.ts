import { OpenAI } from "openai";
import { generateSimpleResponseOllama } from "./ollamaUtils";
import { Message } from "ollama";
import { generateLivestackText } from "./llmCatalog";

export const fewShotExamples = [
  {
    role: "system",
    content: `
You are a helpful assistant. Your job is to provide a list of topics based on the provided CONTENT.
Instructions:
- Write each topic in a new line.
- Keep each topic under 5 words.
- Response with a JSON object with a single key "topics" and an array of topics as the value, and nothing else.
`,
  },
  {
    role: "user",
    content: `
CONTENT:
\`\`\`
The recent landmark election, marked by record voter turnout and diverse candidate fields, signaled a pivotal moment in the nation's democratic history. With pressing issues at the forefront, voters delivered a decisive mandate for change, unseating incumbents and ushering in new leadership committed to transparency, accountability, and progress.
\`\`\`
`,
  },
  {
    role: "assistant",
    content: `
{
"topics": [
"Record turnout and diversity",
"Decisive mandate for change",
"New leadership's commitment"
]
}
`,
  },
  {
    role: "user",
    content: `
CONTENT:
\`\`\`
Dr. Chen highlights AI's exciting advancements, especially in healthcare, where it's revolutionizing diagnostics. He emphasizes the need to address ethical concerns like privacy and bias as AI integrates further into daily life, stressing transparency and regulation.
\`\`\`
`,
  },
  {
    role: "assistant",
    content: `
{
"topics": [
"AI advancements in healthcare",
"Ethical concerns in AI integration",
"Emphasis on transparency and regulation"
]
}
`,
  },
];

export const summarize = async (
  input: { messages: Message[]; format?: string } & (
    | { useCloudSummarizer: true; openai: OpenAI }
    | {
        useCloudSummarizer: false;
      }
  )
) => {
  const { messages, useCloudSummarizer, format } = input;
  if (useCloudSummarizer) {
    const r = await generateLivestackText({
      purpose: "topics-openai",
      messages: messages as any,
      parameters: { temperature: 1 },
    });
    return r;
  } else {
    const r = await generateSimpleResponseOllama({ messages, format });
    return r;
  }
};
