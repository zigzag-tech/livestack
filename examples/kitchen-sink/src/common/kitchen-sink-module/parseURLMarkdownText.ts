import { Readability } from "@mozilla/readability";
import { JSDOM } from "jsdom";
// A simple Markdown to plain text converter in TypeScript

export async function parseURLMarkdownText(url: string) {
  // first obtain the html
  const response = await fetch(url);
  const html = await response.text();
  const doc = new JSDOM(html);
  const reader = new Readability(doc.window.document);
  const result = reader.parse();
  let title: string |null| undefined = result?.title;
  let markdown: string | null|undefined = result?.textContent;

  if (!title || !markdown) {
    ({ title, markdown } = await extractViaExtractorAPI(url));
  }

  if (!title) {
    title = "No title";
  }

  if (!markdown) {
    markdown = "No content";
  }

  const body = SimpleMarkdownConverter.convert(markdown);
  return { body, title };
}

class SimpleMarkdownConverter {
  private static readonly markdownRegex: RegExp[] = [
    /\*\*(.*?)\*\*/g, // Bold
    /\*(.*?)\*/g, // Italic
    /__(.*?)__/g, // Underline
    /~~(.*?)~~/g, // Strikethrough
    /\[(.*?)\]\((.*?)\)/g, // Links
    /!\[(.*?)\]\((.*?)\)/g, // Images
    /```[\s\S]+?```/g, // Code blocks
    /`(.*?)`/g, // Inline code
    /^#(.*?)$/gm, // Headers
    /^>(.*?)$/gm, // Blockquotes
    /^\s*\* (.*?)$/gm, // Unordered list items
    /^\s*\d+\. (.*?)$/gm, // Ordered list items
  ];

  public static convert(markdown: string): string {
    let plainText: string = markdown;

    // Strip all rich text elements
    for (const regex of SimpleMarkdownConverter.markdownRegex) {
      plainText = plainText.replace(regex, "$1");
    }

    // Replace multiple newlines with a single newline
    plainText = plainText.replace(/\n{2,}/g, "\n\n");

    // Remove leading and trailing spaces on each line
    plainText = plainText
      .split("\n")
      .map((line) => line.trim())
      .join("\n");

    return plainText;
  }
}
const extractViaExtractorAPI = async (url: string) => {
  const { title, markdown } = await (
    await fetch(
      `https://extractorapi.com/api/v1/extractor/?apikey=95adc645fe4aa392b86b14544f6352353cf43a92&url=${url}`
    )
  ).json();
  return { title, markdown };
};

// const extractViaExtractorAPI = redisCacheFn({
//   fn: _extractViaExtractorAPI,
//   idFn: (url) => `extractViaExtractorAPI:${url}`,
//   expiry: 60 * 60 * 24,
// });
