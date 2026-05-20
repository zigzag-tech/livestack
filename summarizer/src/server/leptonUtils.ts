import { fewShotExamples } from "./ollamaUtils";
import { generateLivestackText } from "./llmCatalog";

export const LLAMA2_13B_MODEL = "llama2-13b";
export const LLAMA2_70B_MODEL = "llama2-70b";

export const genLeptonAIConfig = (modelId: string) => ({
  apiKey: process.env.LEPTONAI_API_KEY,
  baseURL: `https://${modelId}.lepton.run/api/v1`,
});

export async function executeOpenAILikeLLMAPIChat({
  prompt,
  modelIds,
}: {
  prompt: string;
  modelIds: string[];
}) {
  void modelIds;
  const message = await generateLivestackText({
    purpose: "title-lepton",
    messages: [
      ...fewShotExamples,
      {
        role: "user",
        content: `ORIGINAL TEXT:
\`\`\`
${prompt}
\`\`\`

JSON TITLE:
`,
      },
    ],
    parameters: {
      temperature: 0.5,
    },
  });

  if (message) {
    return message;
  }

  throw new Error("None of the models managed to generate a summary.");
}
