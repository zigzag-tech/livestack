# LiveStack Tutorial: Creating a Real-Time Speech to Transcription, Summary, and Translation App

Ever wished you could turn spoken words into text, give them a quick summary, and even translate them into different languages, all in real-time? Well, buckle up because we're about to make that happen, and we promise it's easier than it sounds!

In this tutorial, we'll guide you through the exciting process of creating your very own real-time speech app. We'll keep things simple, no tech mumbo-jumbo here, just clear explanations and easy-to-follow steps.

We'll tackle all those tricky questions you might have, like how to capture speech on the frontend (spoiler alert: it's a breeze!), how to handle those lengthy audio files on the backend, and most importantly, how to turn speech into text, summarize it, and switch languages seamlessly.

Whether you're a coding pro or a complete beginner, this tutorial is your roadmap to speech-to-text success. So, grab a cup of coffee, get comfy, and let's dive into the wonderful world of LiveStack. We promise it'll be a rewarding journey! 🚀

## Prerequisites

- Node.js and npm installed
- Basic knowledge of TypeScript and Express.js
- LiveStack packages installed

## Step 1: Setting Up the Project

First, create a new directory for your project and initialize it with npm:

```sh
mkdir livestack-speech-app
cd livestack-speech-app
npm init -y
```

Install the necessary LiveStack packages:

```sh
npm install @livestack/core @livestack/gateway @livestack/transcribe-server @livestack/lab-internal-server @livestack/summarizer
```

The folder structure should follow:
```md
- public/
- src/
  - server/
  - common/ (optional: any common definitions that are shared between front end and back end)
  - client/
- index.html
- package.json
- tsconfig.json
- vite.config.ts
- postcss.config.cjs (optional: tailwindcss related)
- tailwind.config.ts (optional: tailwindcss related)
```

## Step 2: Setting Up the Server

Create a new file `src/server/index.ts` and add the following code:

```typescript
import { LiveEnv } from "@livestack/core";
import { initJobBinding } from "@livestack/gateway";
import express from "express";
import path from "path";
import bodyParser from "body-parser";
import cors from "cors";
import ViteExpress from "vite-express";
import { liveEnvP } from "./liveEnv";
import { speechLiveflow } from "./workers/liveflow.speech";

async function main() {
  LiveEnv.setGlobal(liveEnvP);

  const app = express();
  app.use(cors());
  app.use(bodyParser.json());
  app.use(express.static(path.join(__dirname, "..", "public")));

  const PORT = 4700;
  const server = ViteExpress.listen(app, PORT, () => {
    console.info(`Server running on http://localhost:${PORT}.`);
  });

  initJobBinding({
    liveEnv: await liveEnvP,
    httpServer: server,
    allowedSpecsForBinding: [speechLiveflow],
  });
}

main();
```

## Step 3: Configuring the Live Environment

Create a new file `src/server/liveEnv.ts` and add the following code:

```typescript
import { LiveEnv, getLocalTempFileStorageProvider } from "@livestack/core";

export const liveEnvP = LiveEnv.create({
  projectId: "LIVE_TEST_ONE",
  storageProvider: getLocalTempFileStorageProvider("/tmp/zzlive"),
});
```

## Step 4: Defining the Liveflow

Create a new file `src/server/workers/liveflow.speech.ts` and add the following code:

```typescript
import {
  rawPCMToWavSpec,
  speechChunkToTextSpec,
} from "@livestack/transcribe-server";
import { Liveflow, conn, expose } from "@livestack/core";
import { SPEECH_LIVEFLOW_NAME } from "../../common/defs";
import { translationSpec } from "@livestack/translate-server";
import { titleSummarizerSepc } from "@livestack/lab-internal-server";
import { textSplittingSpec } from "@livestack/lab-internal-server";

export const speechLiveflow = Liveflow.define({
  name: SPEECH_LIVEFLOW_NAME,
  connections: [
    conn({
      from: {
        spec: rawPCMToWavSpec,
      },
      to: {
        spec: speechChunkToTextSpec,
      },
    }),
    conn({
      from: {
        spec: speechChunkToTextSpec,
      },
      transform: async ({ transcript }) => transcript,
      to: {
        spec: textSplittingSpec,
      },
    }),
    conn({
      from: {
        spec: textSplittingSpec,
      },
      transform: async (chunkText) => ({ transcript: chunkText }),
      to: {
        spec: titleSummarizerSepc,
      },
    }),
    conn({
      from: {
        spec: textSplittingSpec,
      },
      transform: async (chunkText) => ({
        toLang: "French",
        text: chunkText,
      }),
      to: {
        spec: translationSpec,
      },
    }),
  ],
  exposures: [
    expose(rawPCMToWavSpec.input.default, "input-default"),
    expose(speechChunkToTextSpec.output.default, "transcription"),
    expose(titleSummarizerSepc.output.default, "summarized-title"),
    expose(translationSpec.output.default, "translation"),
  ],
});
```

## Step 5: Setting Up the Front End

Create a new file `src/client/index.tsx` and add the following code:

```typescript
import React, { useState, Suspense } from "react";
import ReactDOM from "react-dom/client";
import {
  createBrowserRouter,
  RouterProvider,
} from "react-router-dom";
import "./globals.scss";

const AudioInputDashboard = React.lazy(() => import("./input/page"));

const router = createBrowserRouter([
  {
    path: "/",
    element: <AudioInputDashboard />,
  }
]);

const root = ReactDOM.createRoot(
  document.getElementById("root") as HTMLElement
);

root.render(
  <Suspense fallback={<div>Loading...</div>}>
    <RouterProvider router={router} />
  </Suspense>
);
```

Create a new file `src/client/input/page.tsx` and add the following code:

```typescript
"use client";
import React from "react";
import {
  usePCMRecorder,
  encodeToB64,
  RecordButton,
  VolumeBar,
  LiveTitle,
  rawPCMInput,
  Transcripts,
} from "@livestack/transcribe-client";
import { useJobBinding, useOutput, useInput } from "@livestack/client";
import { SPEECH_LIVEFLOW_NAME } from "../../common/defs";
import { translationOutputSchema } from "@livestack/lab-internal-common";

export const AudioInput: React.FC = () => {
  const job = useJobBinding({
    specName: SPEECH_LIVEFLOW_NAME,
  });

  const { feed } = useInput({
    tag: "input-default",
    def: rawPCMInput,
    job,
  });

  const translation = useOutput({
    tag: "translation",
    def: translationOutputSchema,
    job,
    query: { type: "lastN", n: 10 },
  });

  const [volume, setVolume] = React.useState<number>(0);

  const { startRecording, stopRecording, isRecording, cumulativeDataSent } =
    usePCMRecorder({
      onDataAvailable: async (data) => {
        console.log(Math.max(...data), Math.min(...data));
        const encoded = encodeToB64(data);
        if (feed) {
          await feed({ rawPCM64Str: encoded });
          console.log(encoded.slice(0, 10), "length: ", encoded.length);
        }
      },
      onVolumeChange: (volume) => {
        setVolume(volume);
      },
      maxInterval: 7 * 1000,
      silenceDuration: 300,
      silentThreshold: -50,
      minDecibels: -100,
    });

  const handleRecording = isRecording ? stopRecording : startRecording;

  return (
    <div className="flex flex-col m-4">
      <div>
        {job.jobId && (
          <RecordButton
            isRecording={isRecording}
            handleRecording={handleRecording}
            className="w-fit rounded"
          />
        )}
        <VolumeBar volume={volume} cumulativeDataSent={cumulativeDataSent} />
      </div>
      <br />
      <Transcripts
        tag="transcription"
        job={job}
        query={{ type: "lastN", n: 10 }}
      />
      <br />
      <LiveTitle tag="summarized-title" job={job} />
      <br />
      {translation && (
        <div>
          <h2>Translated Sentences</h2>
          {translation.map((t, idx) => (
            <div key={idx}>{t.data.translated}</div>
          ))}
        </div>
      )}
    </div>
  );
};
export default AudioInput;
```

## Step 6: Running the Application

To run the application, use the following command:

```sh
npx ts-node src/server/index.ts
```

Your server should now be running on `http://localhost:4700`.

## Conclusion

You have successfully created a real-time backend application that converts speech to text, summarizes the text, and can be extended to translate it into another language using LiveStack packages. You can now build upon this foundation to add more features and improve the application.
