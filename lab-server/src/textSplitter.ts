/**
 * Text splitter implementation based on LangChain's text splitter
 * Function-based implementation with no classes
 * Adapted from: https://github.com/langchain-ai/langchainjs/blob/main/libs/langchain-textsplitters/src/text_splitter.ts
 */

export type TextSplitterOptions = {
  chunkSize?: number;
  chunkOverlap?: number;
  keepSeparator?: boolean;
  lengthFunction?: (text: string) => number;
};

/**
 * Split text on a separator
 */
export function splitOnSeparator(
  text: string, 
  separator: string, 
  keepSeparator: boolean = false
): string[] {
  let splits;
  if (separator) {
    if (keepSeparator) {
      const regexEscapedSeparator = separator.replace(
        /[/\-\\^$*+?.()|[\]{}]/g,
        "\\$&"
      );
      splits = text.split(new RegExp(`(?=${regexEscapedSeparator})`));
    } else {
      splits = text.split(separator);
    }
  } else {
    splits = text.split("");
  }
  return splits.filter((s) => s !== "");
}

/**
 * Join text chunks with a separator
 */
function joinDocs(docs: string[], separator: string): string | null {
  const text = docs.join(separator).trim();
  return text === "" ? null : text;
}

/**
 * Merge text chunks to respect a maximum chunk size
 */
export function mergeSplits(
  splits: string[], 
  separator: string,
  options: TextSplitterOptions = {}
): string[] {
  const chunkSize = options.chunkSize ?? 1000;
  const chunkOverlap = options.chunkOverlap ?? 200;
  const lengthFunction = options.lengthFunction ?? ((text: string) => text.length);

  if (chunkOverlap >= chunkSize) {
    throw new Error("Cannot have chunkOverlap >= chunkSize");
  }

  const docs: string[] = [];
  const currentDoc: string[] = [];
  let total = 0;
  
  for (const d of splits) {
    const _len = lengthFunction(d);
    if (
      total + _len + currentDoc.length * separator.length >
      chunkSize
    ) {
      if (total > chunkSize) {
        console.warn(
          `Created a chunk of size ${total}, which is longer than the specified ${chunkSize}`
        );
      }
      if (currentDoc.length > 0) {
        const doc = joinDocs(currentDoc, separator);
        if (doc !== null) {
          docs.push(doc);
        }
        // Keep on popping if:
        // - we have a larger chunk than in the chunk overlap
        // - or if we still have any chunks and the length is long
        while (
          total > chunkOverlap ||
          (total + _len + currentDoc.length * separator.length >
            chunkSize &&
            total > 0)
        ) {
          total -= lengthFunction(currentDoc[0]);
          currentDoc.shift();
        }
      }
    }
    currentDoc.push(d);
    total += _len;
  }
  
  const doc = joinDocs(currentDoc, separator);
  if (doc !== null) {
    docs.push(doc);
  }
  
  return docs;
}

/**
 * Basic text splitter that splits on a character
 */
export function splitText(
  text: string, 
  options: TextSplitterOptions & { separator?: string } = {}
): string[] {
  const separator = options.separator ?? "\n\n";
  const keepSeparator = options.keepSeparator ?? false;
  
  // First we naively split the large input into a bunch of smaller ones
  const splits = splitOnSeparator(text, separator, keepSeparator);
  return mergeSplits(
    splits, 
    keepSeparator ? "" : separator,
    options
  );
}

/**
 * Recursively split text using multiple separators in order
 */
export function splitTextRecursively(
  text: string,
  options: TextSplitterOptions & { separators?: string[] } = {}
): string[] {
  const separators = options.separators ?? ["\n\n", "\n", " ", ""];
  const keepSeparator = options.keepSeparator ?? true;
  const chunkSize = options.chunkSize ?? 1000;
  const lengthFunction = options.lengthFunction ?? ((text: string) => text.length);
  
  return _splitTextRecursively(text, separators, {
    ...options,
    keepSeparator
  });
}

/**
 * Internal recursive text splitting function
 */
function _splitTextRecursively(
  text: string, 
  separators: string[],
  options: TextSplitterOptions
): string[] {
  const finalChunks: string[] = [];
  const chunkSize = options.chunkSize ?? 1000;
  const keepSeparator = options.keepSeparator ?? true;
  const lengthFunction = options.lengthFunction ?? ((text: string) => text.length);

  // Get appropriate separator to use
  let separator: string = separators[separators.length - 1];
  let newSeparators;
  for (let i = 0; i < separators.length; i += 1) {
    const s = separators[i];
    if (s === "") {
      separator = s;
      break;
    }
    if (text.includes(s)) {
      separator = s;
      newSeparators = separators.slice(i + 1);
      break;
    }
  }

  // Now that we have the separator, split the text
  const splits = splitOnSeparator(text, separator, keepSeparator);

  // Now go merging things, recursively splitting longer texts.
  let goodSplits: string[] = [];
  const _separator = keepSeparator ? "" : separator;
  for (const s of splits) {
    if (lengthFunction(s) < chunkSize) {
      goodSplits.push(s);
    } else {
      if (goodSplits.length) {
        const mergedText = mergeSplits(goodSplits, _separator, options);
        finalChunks.push(...mergedText);
        goodSplits = [];
      }
      if (!newSeparators) {
        finalChunks.push(s);
      } else {
        const otherInfo = _splitTextRecursively(s, newSeparators, options);
        finalChunks.push(...otherInfo);
      }
    }
  }
  if (goodSplits.length) {
    const mergedText = mergeSplits(goodSplits, _separator, options);
    finalChunks.push(...mergedText);
  }
  return finalChunks;
}

/**
 * Get language-specific separators
 */
export function getSeparatorsForLanguage(language: string): string[] {
  switch (language.toLowerCase()) {
    case "js":
    case "javascript":
      return [
        "\nfunction ",
        "\nconst ",
        "\nlet ",
        "\nvar ",
        "\nclass ",
        "\nif ",
        "\nfor ",
        "\nwhile ",
        "\nswitch ",
        "\ncase ",
        "\ndefault ",
        "\n\n",
        "\n",
        " ",
        "",
      ];
    case "python":
      return [
        "\nclass ",
        "\ndef ",
        "\n\tdef ",
        "\n\n",
        "\n",
        " ",
        "",
      ];
    case "markdown":
      return [
        "\n## ",
        "\n### ",
        "\n#### ",
        "\n##### ",
        "\n###### ",
        "```\n\n",
        "\n\n***\n\n",
        "\n\n---\n\n",
        "\n\n___\n\n",
        "\n\n",
        "\n",
        " ",
        "",
      ];
    case "html":
      return [
        "<body>",
        "<div>",
        "<p>",
        "<br>",
        "<li>",
        "<h1>",
        "<h2>",
        "<h3>",
        "<h4>",
        "<h5>",
        "<h6>",
        "<span>",
        "<table>",
        "<tr>",
        "<td>",
        "<th>",
        "<ul>",
        "<ol>",
        "<header>",
        "<footer>",
        "<nav>",
        "<head>",
        "<style>",
        "<script>",
        "<meta>",
        "<title>",
        " ",
        "",
      ];
    default:
      return ["\n\n", "\n", " ", ""];
  }
}

/**
 * Split text using language-specific separators
 */
export function splitTextByLanguage(
  text: string,
  language: string,
  options: TextSplitterOptions = {}
): string[] {
  return splitTextRecursively(text, {
    ...options,
    separators: getSeparatorsForLanguage(language),
  });
} 