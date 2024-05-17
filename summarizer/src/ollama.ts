import { wrapWithTransientStdout } from "@livestack/shared";
import { Message } from "ollama";
const CONVO_MODEL = "llama3:instruct";
export async function generateSimpleResponseOllama(
  messages: Message[],
  modelName?: string
): Promise<string> {
  const { Ollama } = await import("ollama");
  const OLLAMA_HOST = process.env.OLLAMA_HOST || "http://localhost:11434";
  const ollama = new Ollama({ host: OLLAMA_HOST });

  try {
    const response = await ollama.chat({
      stream: true,
      model: modelName || CONVO_MODEL,
      messages,
    });
    const message = wrapWithTransientStdout(response);

    return message;
  } catch (e) {
    console.log(e);
    return "Sorry, I am not able to respond right now. Please try again later.";
  }
}
