import { GenerateResponse } from "ollama";
export async function wrapWithTransientStdout(
  response: AsyncGenerator<GenerateResponse>
) {
  let message = "";
  for await (const part of response) {
    process.stdout.write(part.response.replace("\n", " ").replace("\r", " "));
    message += part.response;
  }
  // erase all of what was written
  // Move the cursor to the beginning of the line
  process.stdout.write("\r");
  // process.stdout.write("\r\n");
  // Clear the entire line
  process.stdout.write("\x1b[2K");
  return message;
}
