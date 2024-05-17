import {
  CharacterTextSplitter,
  RecursiveCharacterTextSplitter,
} from "langchain/text_splitter";
import { genManuallyFedIterator } from "@livestack/shared";



export const getCharacterTextSplitter = ({
  chunkSize,
  chunkOverlap = 0,
  separator = " ",
}: {
  chunkSize: number;
  chunkOverlap?: number;
  separator?: string;
}) => {
  return new CharacterTextSplitter({
    separator,
    chunkSize,
    chunkOverlap,
  });
};

export const getRecursiveCharacterTextSplitter = ({
  chunkSize,
  chunkOverlap = 0,
}: {
  chunkSize: number;
  chunkOverlap?: number;
}) => {
  return new RecursiveCharacterTextSplitter({
    chunkSize,
    chunkOverlap,
  });
};

export function genSplitterIterable({
  chunkSize,
  chunkOverlap = 0,
}: {
  chunkSize: number;
  chunkOverlap?: number;
}) {
  const splitter = new RecursiveCharacterTextSplitter({
    chunkSize,
    chunkOverlap,
  });
  let text = "";

  const { iterator, resolveNext, terminate } = genManuallyFedIterator<string>();
  let cursor = 0; // Track the position where the last split ended

  let maybeSplit = async () => {
    // when enough text has been fed, maybe take a chunk out of the text and set it to the remaining text
    // determine the next chunk with the splitter
    if (text.length > chunkSize) {
      const [firstChunk, ...remainingChunks] = await splitter.createDocuments([
        text,
      ]);
      // console.debug("current text", text);
      // console.debug("firstChunk", firstChunk.pageContent);
      // console.debug(
      //   "remainingChunks",
      //   remainingChunks.map(({ pageContent: c }) => c)
      // );
      text = remainingChunks.map(({ pageContent: c }) => c).join("");

      await resolveNext(firstChunk.pageContent);
    }
  };

  const flushRemaining = async () => {
    console.log("flushing remaining text", text);
    if (text.length > 0) {
      const chunks = await splitter.createDocuments([text]);
      for (const chunk of chunks) {
        await resolveNext(chunk.pageContent);
      }
    }
    terminate();
  };

  const feed = async (t: string) => {
    text += t;
    await maybeSplit();
    // return number of characters accumulated so far
    return {
      numCharsInCache: text.length,
    };
  };

  return { ...iterator, feed, flushRemaining };
}

// if (require.main === module) {
//   (async () => {
//     const text = `Hi.

//   I'm Harrison.
  
//   How? Are? You? Okay then f f f f.
//   This is a weird text to write, but gotta test the splittingggg some how.
  
//   Bye!
  
//   -H.`;
//     const ipsum = new LoremIpsum({
//       wordsPerSentence: {
//         max: 20,
//         min: 4,
//       },
//     });

//     // const output = await splitter.createDocuments([text]);
//     const splitter = genSplitterIterable({ chunkSize: 200 });

//     (async () => {
//       while (true) {
//         const n = ipsum.generateSentences(1);
//         splitter.feed(text);
//         await sleep(500);
//       }
//     })();

//     for await (const chunk of splitter) {
//       console.log("---------");
//       console.log(chunk);
//     }
//   })();
// }
