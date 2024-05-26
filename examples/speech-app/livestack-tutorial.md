# LiveStack Tutorial: Creating a Real-Time Speech to Transcription, Summary, and Translation App

In this tutorial, we'll guide you to create your very own real-time speech app using simple livestack back-end and front-end components.

## Prerequisites

- Node.js (version 18+, 20+) and npm/yarn installed
- Basic knowledge of TypeScript and React

## Step 1: Setting Up the Project

We will use `vite` to scaffold our project:

```sh
npm create vite@latest my-speech-app -- --template react-ts
cd my-speech-app
npm install
```

Install the necessary LiveStack packages:

```sh
npm install @livestack/core @livestack/gateway @livestack/transcribe @livestack/lab-internal-server @livestack/summarizer
```

## Step 2: Setting Up Client-side and Server-side Code

Remove all files under `src/` and create the following folder structure under `src/`:

```md
- src/
  - server/
  - common/ (optional: any common definitions that are shared between front end and back end)
  - client/
```

### Step 2.1: Create a Back-end `Liveflow` Workflow

Create a new file `src/server/liveflow.speech.ts` and add the following code:

```typescript
import {
  rawPCMToWavSpec,
  speechChunkToTextSpec,
} from "@livestack/transcribe/server";
import { Liveflow, conn, expose } from "@livestack/core";
import { SPEECH_LIVEFLOW_NAME } from "../common/defs";
import { translationSpec } from "@livestack/translate-server";
import { titleSummarizerSepc } from "@livestack/summarizer/client";
import { textSplittingSpec } from "@livestack/lab-internal-server";

// Define the speech liveflow
export const speechLiveflow = Liveflow.define({
  name: SPEECH_LIVEFLOW_NAME,
  connections: [
    // Connection from raw PCM to WAV
    conn({
      from: rawPCMToWavSpec,
      to: speechChunkToTextSpec,
    }),
    // Connection from speech chunk to text
    conn({
      from: speechChunkToTextSpec,
      transform: ({ transcript }) => transcript,
      to: textSplittingSpec,
    }),
    // Connection from text splitting to title summarizer
    conn({
      from: textSplittingSpec,
      transform: (chunkText) => ({ transcript: chunkText }),
      to: titleSummarizerSepc,
    }),
    // Connection from text splitting to translation
    conn({
      from: textSplittingSpec,
      transform: (chunkText) => ({
        toLang: "French",
        text: chunkText,
      }),
      to: translationSpec,
    }),
  ],
  exposures: [
    // Expose the default input for raw PCM to WAV
    expose(rawPCMToWavSpec.input.default, "input-default"),
    // Expose the default output for speech chunk to text
    expose(speechChunkToTextSpec.output.default, "transcription"),
    // Expose the default output for title summarizer
    expose(titleSummarizerSepc.output.default, "summarized-title"),
    // Expose the default output for translation
    expose(translationSpec.output.default, "translation"),
  ],
});
```

The above file will be the main workflow powering our real-time speech app.

### Step 2.2: Create the Back-end Server

Create a new file `src/server/index.ts` and add the following code:

```typescript
import { LiveEnv } from "@livestack/core";
import { getLocalTempFileStorageProvider } from "@livestack/core";
import { initJobBinding } from "@livestack/gateway";
import express from "express";
import path from "path";
import bodyParser from "body-parser";
import cors from "cors";
import ViteExpress from "vite-express";
import { speechLiveflow } from "./liveflow.speech";

const liveEnvP = LiveEnv.create({
  projectId: "MY_LIVE_SPEECH_APP",
  storageProvider: getLocalTempFileStorageProvider("/tmp/zzlive"),
});

// Main function
async function main() {
  // Set the global LiveEnv
  LiveEnv.setGlobal(liveEnvP);

  // Create an Express app
  const app = express();
  app.use(cors());
  app.use(bodyParser.json());
  app.use(express.static(path.join(__dirname, "..", "public")));

  // Define the port for the server
  const PORT = 4700;

  // Start the server
  const httpServer = ViteExpress.listen(app, PORT, () => {
    console.info(`Server running on http://localhost:${PORT}.`);
  });

  // Initialize job binding for any incoming requests from the client
  initJobBinding({
    httpServer,
    allowedSpecsForBinding: [speechLiveflow],
  });
}

// Call the main function
main();
```

### Step 2.3: Create the Front-end Client

Create a new file `src/client/index.tsx` and add the following code:

```typescript
import React, { Suspense } from "react";
import ReactDOM from "react-dom/client";
import SpeechComponents from "./SpeechComponents";

const root = ReactDOM.createRoot(
  document.getElementById("root") as HTMLElement
);

root.render(
  <Suspense fallback={<div>Loading...</div>}>
    <SpeechComponents />
  </Suspense>
);
```

### Step 2.4: Create the Necessary Front-end Components

Create a new file `src/client/SpeechComponents.tsx` and add the following code:

```typescript
"use client";
import React from "react";
import {
  usePCMRecorder,
  encodeToB64,
  rawPCMInput,
  speechChunkToTextOutput,
} from "@livestack/transcribe/client";
import { useJobBinding, useOutput, useInput } from "@livestack/client";
import { SPEECH_LIVEFLOW_NAME } from "../common/defs";
import { translationOutputSchema } from "@livestack/lab-internal-common";
import { FaStop, FaMicrophone } from "react-icons/fa";
import { z } from "zod";
import prettyBytes from "pretty-bytes";

// SpeechComponents component
export const SpeechComponents: React.FC = () => {
  // Bind to a job using the specified live flow name
  const job = useJobBinding({
    specName: SPEECH_LIVEFLOW_NAME,
  });

  // Set up input feed for raw PCM data
  const { feed } = useInput({
    tag: "input-default",
    def: rawPCMInput,
    job,
  });

  // Set up output for translation data
  const translation = useOutput({
    tag: "translation",
    def: translationOutputSchema,
    job,
    query: { type: "lastN", n: 10 },
  });

  const { last: summarizedTitle } = useOutput({
    tag: "summarized-title",
    def: z.object({
      summarizedTitle: z.string(),
    }),
    job,
  });
  const transcription = useOutput({
    tag: "transcription",
    def: speechChunkToTextOutput,
    job,
    query: { type: "lastN", n: 10 },
  });

  // State for volume level
  const [volume, setVolume] = React.useState<number>(0);

  // Set up PCM recorder with handlers for data and volume changes
  const { startRecording, stopRecording, isRecording, cumulativeDataSent } =
    usePCMRecorder({
      onDataAvailable: async (data) => {
        const encoded = encodeToB64(data);
        if (feed) {
          await feed({ rawPCM64Str: encoded });
          console.log(encoded.slice(0, 10), "length: ", encoded.length);
        }
      },
      onVolumeChange: (volume) => {
        setVolume(volume);
      },
    });

  // Toggle recording state
  const handleRecording = isRecording ? stopRecording : startRecording;

  return (
    <div className="m-4 grid grid-cols-5 gap-2 divide-x">
      <div>
        <h2 className="text-red-800">1. Click on "Start Recording" button</h2>
        <br />
        {job.jobId && (
          <button
            className="btn w-fit rounded border border-gray-800 bg-gray-200 p-2"
            onClick={handleRecording}
          >
            <span style={{ display: "inline-block" }}>
              {isRecording ? "Stop Recording" : "Start Recording"}
            </span>
            &nbsp;
            <span style={{ display: "inline-block" }}>
              {isRecording ? <FaStop /> : <FaMicrophone />}
            </span>
          </button>
        )}
        <div>
          Volume: <span>{volume.toFixed(1)}</span>
          <br />
          <progress
            value={volume}
            max={100}
            style={{ width: "100px" }}
          ></progress>
          <br />
          {typeof cumulativeDataSent !== "undefined" && (
            <>Total data sent: {prettyBytes(cumulativeDataSent)}</>
          )}
        </div>{" "}
      </div>
      <div className="col-span-2">
        <div className="ml-4">
          <h2 className="text-green-800">
            2. Speech transcripts will pop up here
          </h2>
          <br />
          <>
            <h2>Transcript</h2>
            <article
              style={{
                maxWidth: "100%",
              }}
            >
              {transcription.map((transcript, i) => (
                <span key={i} className="text-sm">
                  {transcript.data.transcript}
                </span>
              ))}
            </article>
          </>
        </div>
      </div>
      <div className="col-span-2">
        <div className="ml-4">
          <h2 className="text-blue-800">
            3. Periodically, a one-liner short summary is generated
          </h2>
          <br />
          <>
            <h2>Title</h2>
            <p>{summarizedTitle?.data.summarizedTitle}</p>
          </>
          <br />
          {translation && (
            <div>
              <h2 className="text-indigo-800">
                4. Your speech translated to French
              </h2>
              <br />
              {translation.map((t, idx) => (
                <div key={idx}>{t.data.translated}</div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
};
export default SpeechComponents;
```

### Step 2.5: Add Necessary Front-end Files to Enable Silence-aware Speech Recorder

Navigate to `public/`, create a new folder `public/vad/` and create the following 2 files:

Create the file `public/vad/fft.js` and add the following code:

```js
"use strict";

function FFT(size) {
  this.size = size | 0;
  if (this.size <= 1 || (this.size & (this.size - 1)) !== 0)
    throw new Error("FFT size must be a power of two and bigger than 1");

  this._csize = size << 1;

  // NOTE: Use of `var` is intentional for old V8 versions
  var table = new Array(this.size * 2);
  for (var i = 0; i < table.length; i += 2) {
    const angle = (Math.PI * i) / this.size;
    table[i] = Math.cos(angle);
    table[i + 1] = -Math.sin(angle);
  }
  this.table = table;

  // Find size's power of two
  var power = 0;
  for (var t = 1; this.size > t; t <<= 1) power++;

  // Calculate initial step's width:
  //   * If we are full radix-4 - it is 2x smaller to give inital len=8
  //   * Otherwise it is the same as `power` to give len=4
  this._width = power % 2 === 0 ? power - 1 : power;

  // Pre-compute bit-reversal patterns
  this._bitrev = new Array(1 << this._width);
  for (var j = 0; j < this._bitrev.length; j++) {
    this._bitrev[j] = 0;
    for (var shift = 0; shift < this._width; shift += 2) {
      var revShift = this._width - shift - 2;
      this._bitrev[j] |= ((j >>> shift) & 3) << revShift;
    }
  }

  this._out = null;
  this._data = null;
  this._inv = 0;
}
export default FFT;

FFT.prototype.fromComplexArray = function fromComplexArray(complex, storage) {
  var res = storage || new Array(complex.length >>> 1);
  for (var i = 0; i < complex.length; i += 2) res[i >>> 1] = complex[i];
  return res;
};

FFT.prototype.createComplexArray = function createComplexArray() {
  const res = new Array(this._csize);
  for (var i = 0; i < res.length; i++) res[i] = 0;
  return res;
};

FFT.prototype.toComplexArray = function toComplexArray(input, storage) {
  var res = storage || this.createComplexArray();
  for (var i = 0; i < res.length; i += 2) {
    res[i] = input[i >>> 1];
    res[i + 1] = 0;
  }
  return res;
};

FFT.prototype.completeSpectrum = function completeSpectrum(spectrum) {
  var size = this._csize;
  var half = size >>> 1;
  for (var i = 2; i < half; i += 2) {
    spectrum[size - i] = spectrum[i];
    spectrum[size - i + 1] = -spectrum[i + 1];
  }
};

FFT.prototype.transform = function transform(out, data) {
  if (out === data)
    throw new Error("Input and output buffers must be different");

  this._out = out;
  this._data = data;
  this._inv = 0;
  this._transform4();
  this._out = null;
  this._data = null;
};

FFT.prototype.realTransform = function realTransform(out, data) {
  if (out === data)
    throw new Error("Input and output buffers must be different");

  this._out = out;
  this._data = data;
  this._inv = 0;
  this._realTransform4();
  this._out = null;
  this._data = null;
};

FFT.prototype.inverseTransform = function inverseTransform(out, data) {
  if (out === data)
    throw new Error("Input and output buffers must be different");

  this._out = out;
  this._data = data;
  this._inv = 1;
  this._transform4();
  for (var i = 0; i < out.length; i++) out[i] /= this.size;
  this._out = null;
  this._data = null;
};

// radix-4 implementation
//
// NOTE: Uses of `var` are intentional for older V8 version that do not
// support both `let compound assignments` and `const phi`
FFT.prototype._transform4 = function _transform4() {
  var out = this._out;
  var size = this._csize;

  // Initial step (permute and transform)
  var width = this._width;
  var step = 1 << width;
  var len = (size / step) << 1;

  var outOff;
  var t;
  var bitrev = this._bitrev;
  if (len === 4) {
    for (outOff = 0, t = 0; outOff < size; outOff += len, t++) {
      const off = bitrev[t];
      this._singleTransform2(outOff, off, step);
    }
  } else {
    // len === 8
    for (outOff = 0, t = 0; outOff < size; outOff += len, t++) {
      const off = bitrev[t];
      this._singleTransform4(outOff, off, step);
    }
  }

  // Loop through steps in decreasing order
  var inv = this._inv ? -1 : 1;
  var table = this.table;
  for (step >>= 2; step >= 2; step >>= 2) {
    len = (size / step) << 1;
    var quarterLen = len >>> 2;

    // Loop through offsets in the data
    for (outOff = 0; outOff < size; outOff += len) {
      // Full case
      var limit = outOff + quarterLen;
      for (var i = outOff, k = 0; i < limit; i += 2, k += step) {
        const A = i;
        const B = A + quarterLen;
        const C = B + quarterLen;
        const D = C + quarterLen;

        // Original values
        const Ar = out[A];
        const Ai = out[A + 1];
        const Br = out[B];
        const Bi = out[B + 1];
        const Cr = out[C];
        const Ci = out[C + 1];
        const Dr = out[D];
        const Di = out[D + 1];

        // Middle values
        const MAr = Ar;
        const MAi = Ai;

        const tableBr = table[k];
        const tableBi = inv * table[k + 1];
        const MBr = Br * tableBr - Bi * tableBi;
        const MBi = Br * tableBi + Bi * tableBr;

        const tableCr = table[2 * k];
        const tableCi = inv * table[2 * k + 1];
        const MCr = Cr * tableCr - Ci * tableCi;
        const MCi = Cr * tableCi + Ci * tableCr;

        const tableDr = table[3 * k];
        const tableDi = inv * table[3 * k + 1];
        const MDr = Dr * tableDr - Di * tableDi;
        const MDi = Dr * tableDi + Di * tableDr;

        // Pre-Final values
        const T0r = MAr + MCr;
        const T0i = MAi + MCi;
        const T1r = MAr - MCr;
        const T1i = MAi - MCi;
        const T2r = MBr + MDr;
        const T2i = MBi + MDi;
        const T3r = inv * (MBr - MDr);
        const T3i = inv * (MBi - MDi);

        // Final values
        const FAr = T0r + T2r;
        const FAi = T0i + T2i;

        const FCr = T0r - T2r;
        const FCi = T0i - T2i;

        const FBr = T1r + T3i;
        const FBi = T1i - T3r;

        const FDr = T1r - T3i;
        const FDi = T1i + T3r;

        out[A] = FAr;
        out[A + 1] = FAi;
        out[B] = FBr;
        out[B + 1] = FBi;
        out[C] = FCr;
        out[C + 1] = FCi;
        out[D] = FDr;
        out[D + 1] = FDi;
      }
    }
  }
};

// radix-2 implementation
//
// NOTE: Only called for len=4
FFT.prototype._singleTransform2 = function _singleTransform2(
  outOff,
  off,
  step
) {
  const out = this._out;
  const data = this._data;

  const evenR = data[off];
  const evenI = data[off + 1];
  const oddR = data[off + step];
  const oddI = data[off + step + 1];

  const leftR = evenR + oddR;
  const leftI = evenI + oddI;
  const rightR = evenR - oddR;
  const rightI = evenI - oddI;

  out[outOff] = leftR;
  out[outOff + 1] = leftI;
  out[outOff + 2] = rightR;
  out[outOff + 3] = rightI;
};

// radix-4
//
// NOTE: Only called for len=8
FFT.prototype._singleTransform4 = function _singleTransform4(
  outOff,
  off,
  step
) {
  const out = this._out;
  const data = this._data;
  const inv = this._inv ? -1 : 1;
  const step2 = step * 2;
  const step3 = step * 3;

  // Original values
  const Ar = data[off];
  const Ai = data[off + 1];
  const Br = data[off + step];
  const Bi = data[off + step + 1];
  const Cr = data[off + step2];
  const Ci = data[off + step2 + 1];
  const Dr = data[off + step3];
  const Di = data[off + step3 + 1];

  // Pre-Final values
  const T0r = Ar + Cr;
  const T0i = Ai + Ci;
  const T1r = Ar - Cr;
  const T1i = Ai - Ci;
  const T2r = Br + Dr;
  const T2i = Bi + Di;
  const T3r = inv * (Br - Dr);
  const T3i = inv * (Bi - Di);

  // Final values
  const FAr = T0r + T2r;
  const FAi = T0i + T2i;

  const FBr = T1r + T3i;
  const FBi = T1i - T3r;

  const FCr = T0r - T2r;
  const FCi = T0i - T2i;

  const FDr = T1r - T3i;
  const FDi = T1i + T3r;

  out[outOff] = FAr;
  out[outOff + 1] = FAi;
  out[outOff + 2] = FBr;
  out[outOff + 3] = FBi;
  out[outOff + 4] = FCr;
  out[outOff + 5] = FCi;
  out[outOff + 6] = FDr;
  out[outOff + 7] = FDi;
};

// Real input radix-4 implementation
FFT.prototype._realTransform4 = function _realTransform4() {
  var out = this._out;
  var size = this._csize;

  // Initial step (permute and transform)
  var width = this._width;
  var step = 1 << width;
  var len = (size / step) << 1;

  var outOff;
  var t;
  var bitrev = this._bitrev;
  if (len === 4) {
    for (outOff = 0, t = 0; outOff < size; outOff += len, t++) {
      const off = bitrev[t];
      this._singleRealTransform2(outOff, off >>> 1, step >>> 1);
    }
  } else {
    // len === 8
    for (outOff = 0, t = 0; outOff < size; outOff += len, t++) {
      const off = bitrev[t];
      this._singleRealTransform4(outOff, off >>> 1, step >>> 1);
    }
  }

  // Loop through steps in decreasing order
  var inv = this._inv ? -1 : 1;
  var table = this.table;
  for (step >>= 2; step >= 2; step >>= 2) {
    len = (size / step) << 1;
    var halfLen = len >>> 1;
    var quarterLen = halfLen >>> 1;
    var hquarterLen = quarterLen >>> 1;

    // Loop through offsets in the data
    for (outOff = 0; outOff < size; outOff += len) {
      for (var i = 0, k = 0; i <= hquarterLen; i += 2, k += step) {
        var A = outOff + i;
        var B = A + quarterLen;
        var C = B + quarterLen;
        var D = C + quarterLen;

        // Original values
        var Ar = out[A];
        var Ai = out[A + 1];
        var Br = out[B];
        var Bi = out[B + 1];
        var Cr = out[C];
        var Ci = out[C + 1];
        var Dr = out[D];
        var Di = out[D + 1];

        // Middle values
        var MAr = Ar;
        var MAi = Ai;

        var tableBr = table[k];
        var tableBi = inv * table[k + 1];
        var MBr = Br * tableBr - Bi * tableBi;
        var MBi = Br * tableBi + Bi * tableBr;

        var tableCr = table[2 * k];
        var tableCi = inv * table[2 * k + 1];
        var MCr = Cr * tableCr - Ci * tableCi;
        var MCi = Cr * tableCi + Ci * tableCr;

        var tableDr = table[3 * k];
        var tableDi = inv * table[3 * k + 1];
        var MDr = Dr * tableDr - Di * tableDi;
        var MDi = Dr * tableDi + Di * tableDr;

        // Pre-Final values
        var T0r = MAr + MCr;
        var T0i = MAi + MCi;
        var T1r = MAr - MCr;
        var T1i = MAi - MCi;
        var T2r = MBr + MDr;
        var T2i = MBi + MDi;
        var T3r = inv * (MBr - MDr);
        var T3i = inv * (MBi - MDi);

        // Final values
        var FAr = T0r + T2r;
        var FAi = T0i + T2i;

        var FBr = T1r + T3i;
        var FBi = T1i - T3r;

        out[A] = FAr;
        out[A + 1] = FAi;
        out[B] = FBr;
        out[B + 1] = FBi;

        // Output final middle point
        if (i === 0) {
          var FCr = T0r - T2r;
          var FCi = T0i - T2i;
          out[C] = FCr;
          out[C + 1] = FCi;
          continue;
        }

        // Do not overwrite ourselves
        if (i === hquarterLen) continue;

        // In the flipped case:
        // MAi = -MAi
        // MBr=-MBi, MBi=-MBr
        // MCr=-MCr
        // MDr=MDi, MDi=MDr
        var ST0r = T1r;
        var ST0i = -T1i;
        var ST1r = T0r;
        var ST1i = -T0i;
        var ST2r = -inv * T3i;
        var ST2i = -inv * T3r;
        var ST3r = -inv * T2i;
        var ST3i = -inv * T2r;

        var SFAr = ST0r + ST2r;
        var SFAi = ST0i + ST2i;

        var SFBr = ST1r + ST3i;
        var SFBi = ST1i - ST3r;

        var SA = outOff + quarterLen - i;
        var SB = outOff + halfLen - i;

        out[SA] = SFAr;
        out[SA + 1] = SFAi;
        out[SB] = SFBr;
        out[SB + 1] = SFBi;
      }
    }
  }
};

// radix-2 implementation
//
// NOTE: Only called for len=4
FFT.prototype._singleRealTransform2 = function _singleRealTransform2(
  outOff,
  off,
  step
) {
  const out = this._out;
  const data = this._data;

  const evenR = data[off];
  const oddR = data[off + step];

  const leftR = evenR + oddR;
  const rightR = evenR - oddR;

  out[outOff] = leftR;
  out[outOff + 1] = 0;
  out[outOff + 2] = rightR;
  out[outOff + 3] = 0;
};

// radix-4
//
// NOTE: Only called for len=8
FFT.prototype._singleRealTransform4 = function _singleRealTransform4(
  outOff,
  off,
  step
) {
  const out = this._out;
  const data = this._data;
  const inv = this._inv ? -1 : 1;
  const step2 = step * 2;
  const step3 = step * 3;

  // Original values
  const Ar = data[off];
  const Br = data[off + step];
  const Cr = data[off + step2];
  const Dr = data[off + step3];

  // Pre-Final values
  const T0r = Ar + Cr;
  const T1r = Ar - Cr;
  const T2r = Br + Dr;
  const T3r = inv * (Br - Dr);

  // Final values
  const FAr = T0r + T2r;

  const FBr = T1r;
  const FBi = -T3r;

  const FCr = T0r - T2r;

  const FDr = T1r;
  const FDi = T3r;

  out[outOff] = FAr;
  out[outOff + 1] = 0;
  out[outOff + 2] = FBr;
  out[outOff + 3] = FBi;
  out[outOff + 4] = FCr;
  out[outOff + 5] = 0;
  out[outOff + 6] = FDr;
  out[outOff + 7] = FDi;
};
```

Create the file `public/vad/vad-audio-worklet.js` and add the following code:

```js
import FFT from "./fft.js";

/**
 * AudioWorkletProcessor for Voice Activity Detection (VAD).
 *
 * From: Moattar, Mohammad & Homayoonpoor, Mahdi. (2010). A simple but efficient real-time voice activity detection algorithm. European Signal Processing Conference.
 * @see https://www.researchgate.net/publication/255667085_A_simple_but_efficient_real-time_voice_activity_detection_algorithm
 */
class AudioVADProcessor extends AudioWorkletProcessor {
  primThresh_e = 40;
  primThresh_f_hz = 185;
  primThresh_sfm = 5;
  frame_size_ms = 10;

  is_speech_frame_counter = 0;
  is_silent_frame_counter = 0;

  e_min = null;
  f_min = null;
  sfm_min = null;

  sample_rate;
  fft_size = 128;
  fft; // FFT.js instance

  buffer = [];
  frame_size;
  frame_counter = 0;

  last_command_was_speech = true;

  debug = false;

  constructor(options) {
    super(options);

    this.debug = options.processorOptions.debug ?? this.debug;

    this.last_command_was_speech =
      options.processorOptions.lastCommandWasSpeech ??
      this.last_command_was_speech;
    this.sample_rate = options.processorOptions.sampleRate;
    this.fft_size = options.processorOptions.fftSize ?? this.fft_size;
    this.fft = new FFT(this.fft_size);
    this.frame_size = (this.sample_rate * this.frame_size_ms) / 1000;
  }

  post(cmd, data) {
    this.port.postMessage({
      cmd,
      data,
    });
  }

  getSpectrum(data) {
    // zero pad data
    while (data.length < this.fft_size) {
      data.push(0);
    }

    // calculate fft
    const input = this.fft.toComplexArray(data);
    const out = this.fft.createComplexArray();

    this.fft.realTransform(out, input);

    // get amplitude array
    var res = new Array(out.length >>> 1);
    for (var i = 0; i < out.length; i += 2) {
      let real = out[i];
      let imag = out[i + 1];
      res[i >>> 1] = Math.sqrt(real * real + imag * imag);
    }

    return res.slice(0, res.length / 2 - 1);
  }

  process(inputs, outputs, parameters) {
    if (!inputs || !inputs[0] || !inputs[0][0]) {
      return false;
    }
    // buffer input data
    if (this.buffer.length < this.frame_size) {
      this.buffer.push(...inputs[0][0]);
      return true;
    }

    // get time and frequency data
    const timeData = new Float32Array(this.buffer);
    const frequencyData = this.getSpectrum(this.buffer);

    // set dc offset to 0
    frequencyData[0] = 0;

    // reset buffer
    this.buffer = [];

    // increment frame counter
    this.frame_counter++;

    // calculate energy of the frame
    let energy = 0;

    for (let i = 0; i < timeData.length; i++) {
      energy += timeData[i] * timeData[i];
    }

    // get frequency with highest amplitude...
    let f_max = 0;
    let f_max_index = 0;

    // ...and spectral flatness
    let sfm = 0;
    let sfm_sum_geo = 0;
    let sfm_sum_ari = 0;

    // calc both in one loop
    for (let i = 0; i < frequencyData.length; i++) {
      // find frequency with highest amplitude
      if (frequencyData[i] > f_max) {
        f_max = frequencyData[i];
        f_max_index = i;
      }

      // spectral flatness (geometric mean, arithmetic mean)
      const f_geo = frequencyData[i] > 0 ? frequencyData[i] : 1;
      sfm_sum_geo += Math.log(f_geo);
      sfm_sum_ari += f_geo;
    }

    // get frequency in Hz for highest amplitude
    const f_max_hz = (f_max_index * this.sample_rate) / this.fft_size;

    // calculate spectral flatness
    sfm =
      -10 *
      Math.log10(
        Math.exp(sfm_sum_geo / frequencyData.length) /
          (sfm_sum_ari / frequencyData.length)
      );

    // just safety check
    sfm = isFinite(sfm) ? sfm : 0;

    // set initial min values from first 30 frames
    if (this.e_min === null || this.frame_counter < 30) {
      this.e_min = this.e_min > energy && energy !== 0 ? this.e_min : energy;
      this.f_min = this.f_min > f_max_hz ? f_max_hz : this.f_min;
      this.sfm_min = this.sfm_min > sfm ? sfm : this.sfm_min;
    }

    // frame vad counter
    let count = 0;

    // calculate current energy threshold
    const current_thresh_e = this.primThresh_e * Math.log10(this.e_min);

    // check energy threshold
    if (energy - this.e_min >= current_thresh_e) {
      count++;
    }

    // check frequency threshold
    if (f_max > 1 && f_max_hz - this.f_min >= this.primThresh_f_hz) {
      count++;
    }

    // check spectral flatness threshold
    if (sfm > 0 && sfm - this.sfm_min <= this.primThresh_sfm) {
      count++;
    }

    if (count > 1) {
      // is speech
      this.is_speech_frame_counter++;
      this.is_silent_frame_counter = 0;
    } else {
      // is silence, so update min energy value
      this.is_silent_frame_counter++;
      this.e_min =
        (this.is_silent_frame_counter * this.e_min + energy) /
        (this.is_silent_frame_counter + 1);
      this.is_speech_frame_counter = 0;
    }

    // debug
    if (this.debug) {
      this.post("log", {
        size: inputs[0][0].length,
        sampleRate: this.sample_rate,
        e: energy,
        e_true: energy - this.e_min >= current_thresh_e,
        f: f_max_hz,
        f_true: f_max > 1 && f_max_hz - this.f_min >= this.primThresh_f_hz,
        sfm: sfm,
        sfm_true: sfm - this.sfm_min <= this.primThresh_sfm,
        plot: sfm_sum_ari / frequencyData.length,
      });
    }

    // ignore silence if less than 10 frames
    if (this.is_silent_frame_counter > 10 && this.last_command_was_speech) {
      if (this.debug) {
        this.post("silence", { signal: sfm_sum_ari / frequencyData.length });
      } else {
        this.post("silence");
      }

      this.last_command_was_speech = false;
    }

    // ignore speech if less than 5 frames
    if (this.is_speech_frame_counter > 4 && !this.last_command_was_speech) {
      if (this.debug) {
        this.post("speech", { signal: sfm_sum_ari / frequencyData.length });
      } else {
        this.post("speech");
      }

      this.last_command_was_speech = true;
    }

    // return true to keep processor alive
    return true;
  }
}

registerProcessor("vad", AudioVADProcessor);
```

## Step 3: Running the Application

To run the application, navigate to your `package.json` and update `scripts` section to the following:

```md
{
  "name": "my-speech-app",
  "private": true,
  "version": "0.0.0",
  "scripts": {
    "dev": "npx -q tsx --tsconfig tsconfig.json src/server/index.ts"
  },
  "dependencies": { ... },
  "devDependencies": { ... }
}
```

Then run the following command once you've updated your dev scripts:

```sh
npm run dev
```

Your server should now be running on `http://localhost:4700`.

## Conclusion

You have successfully created a real-time backend application that converts speech to text, summarizes the text, and can be extended to translate it into another language using LiveStack packages. You can now build upon this foundation to add more features and improve the application.
