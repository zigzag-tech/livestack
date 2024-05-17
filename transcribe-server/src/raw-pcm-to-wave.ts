import { z } from "zod";
import { JobSpec } from "@livestack/core";
import fs from "fs";

export const inputDef = z.object({
  rawPCM64Str: z.string(),
});

export const rawPCMToWavSpec = new JobSpec({
  name: "raw-pcm-to-wave",
  input: inputDef,
  output: z.object({
    wavb64Str: z.string(),
  }),
});

export const rawPCMToWavWorker = rawPCMToWavSpec.defineWorker({
  processor: async ({ input, output }) => {
    // const fileNamePrefix = "rawPCM-" + Date.now();
    // let fileIndex = 0;
    for await (const data of input) {
      const { rawPCM64Str } = data;
      const rawPCM = await decode(rawPCM64Str);
      const wavRawData = [
        getWAVHeader({
          channel: 1,
          sampleRate: 16000,
          chunks: [rawPCM.buffer],
        }),
        ...[rawPCM.buffer],
      ];

      const blob = new Blob(wavRawData, { type: "audio/wav" });

      const wavb64Str = Buffer.from(await blob.arrayBuffer()).toString(
        "base64"
      );
      await output.emit({ wavb64Str });

      // Write the blob to a file
      // const fileIndexPadded = fileIndex.toString().padStart(4, "0");
      // const filePath = `${fileNamePrefix}-${fileIndexPadded}.wav`;
      // fs.writeFileSync(filePath, Buffer.from(await blob.arrayBuffer()));

      // fileIndex++;
    }
  },
});

/**
 * Writes a string into a DataView object.
 *
 * @param {DataView} dataView - The DataView object to write the string into.
 * @param {number} offset - The offset in bytes.
 * @param {string} string - The string to write.
 */
function writeString(dataView: DataView, offset: number, string: string): void {
  for (let i = 0; i < string.length; i++) {
    dataView.setUint8(offset + i, string.charCodeAt(i));
  }
}

/**
 * Gets the WAV file header information.
 */
function getWAVHeader({
  chunks,
  channel,
  sampleRate,
}: {
  chunks: ArrayBufferLike[];
  channel: number;
  sampleRate: number;
}): ArrayBuffer {
  const BYTES_PER_SAMPLE = Int16Array.BYTES_PER_ELEMENT;
  /**
   * Get stored encoding result with Wave file format header
   * Reference: http://www-mmsp.ece.mcgill.ca/Documents/AudioFormats/WAVE/WAVE.html
   */
  // Create header data
  const dataLength = chunks.reduce((acc, cur) => acc + cur.byteLength, 0);
  const header = new ArrayBuffer(44);
  const view = new DataView(header);
  // RIFF identifier 'RIFF'
  writeString(view, 0, "RIFF");
  // file length minus RIFF identifier length and file description length
  view.setUint32(4, 36 + dataLength, true);
  // RIFF type 'WAVE'
  writeString(view, 8, "WAVE");
  // format chunk identifier 'fmt '
  writeString(view, 12, "fmt ");
  // format chunk length
  view.setUint32(16, 16, true);
  // sample format (raw)
  view.setUint16(20, 1, true);
  // channel count
  view.setUint16(22, channel, true);
  // sample rate
  view.setUint32(24, sampleRate, true);
  // byte rate (sample rate * block align)
  view.setUint32(28, sampleRate * BYTES_PER_SAMPLE * channel, true);
  // block align (channel count * bytes per sample)
  view.setUint16(32, BYTES_PER_SAMPLE * channel, true);
  // bits per sample
  view.setUint16(34, 8 * BYTES_PER_SAMPLE, true);
  // data chunk identifier 'data'
  writeString(view, 36, "data");
  // data chunk length
  view.setUint32(40, dataLength, true);

  return header;
}

function decode(base64: string): Int16Array {
  const uint8Array = new Uint8Array(Buffer.from(base64, "base64"));
  const int16Array = new Int16Array(uint8Array.buffer);
  return int16Array;
}