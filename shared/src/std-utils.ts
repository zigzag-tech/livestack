export async function wrapWithTransientStdout(
  response: AsyncIterable<{
    message: { content: string };
  }>
) {
  let message = "";
  for await (const part of response) {
    process.stdout.write(
      part.message.content.replace("\n", " ").replace("\r", " ")
    );
    message += part.message.content;
  }
  // erase all of what was written
  // Move the cursor to the beginning of the line
  process.stdout.write("\r");
  // process.stdout.write("\r\n");
  // Clear the entire line
  process.stdout.write("\x1b[2K");
  return message;
}
