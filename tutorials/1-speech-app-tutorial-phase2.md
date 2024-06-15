## Phase 2: Get the Transcription to Work

In this phase, we'll get the transcription functionality to work. We'll first define the Liveflow for speech transcription and then update the frontend to display the transcription output.

### Step 1: Update the Backend

#### 1.1 Create `liveflow.speech.ts`

Create a new file named `liveflow.speech.ts` in the `src/server` directory with the following code:

```ts
// Import dependencies
import {
  rawPCMToWavSpec,
  speechChunkToTextSpec,
} from "@livestack/transcribe/server";
import { Liveflow, conn, expose } from "@livestack/core";

// [START] Define speech Liveflow

export const speechLiveflow = Liveflow.define({
  name: "speech_liveflow",
  connections: [
    conn({
      from: rawPCMToWavSpec,
      transform: ({ wavb64Str }) => ({ wavb64Str, whisperType: "openai" }),
      to: speechChunkToTextSpec,
    }),
  ],
  exposures: [
    expose(rawPCMToWavSpec.input.default, "input-default"),
    expose(speechChunkToTextSpec.output.default, "transcription"),
  ],
});
```

Explanation:
- A Liveflow is a realtime DAG (Directed Acyclic Graph) that defines the workflow for processing data.
- We define a Liveflow named `speech_liveflow` that takes raw PCM audio data (to be connected to the frontend), converts it to WAV format, and then transcribes it to text.
- A Spec is a node in the Liveflow DAG that defines the input/output data structure and processing logic.
- Specs are connected using `conn` to form the Liveflow DAG.
- We expose the input and output of the Specs to make them accessible to other parts of the application (in our case, the frontend).

#### 1.2 Update `index.ts`

Update the `index.ts` file in the `src/server` directory to initialize the Liveflow and set up the job binding:

```ts
// ...
// [START] Import dependencies
import { LiveEnv } from "@livestack/core";
import { getLocalTempFileStorageProvider } from "@livestack/core";
import { initJobBinding } from "@livestack/gateway";
import { speechLiveflow } from "./liveflow.speech";
// [END] Import dependencies

// [START] Initialize LiveEnv
const liveEnvP = LiveEnv.create({
  projectId: "MY_LIVE_SPEECH_APP",
  storageProvider: getLocalTempFileStorageProvider("/tmp/zzlive"),
});
// [END] Initialize LiveEnv

async function main() {
  // [START] Set global LiveEnv
  LiveEnv.setGlobal(liveEnvP);
  // [END] Set global LiveEnv

  // ...

  const httpServer = ViteExpress.listen(app, PORT, () => {
    console.info(`Server running on http://localhost:${PORT}.`);
  });

  // [START] Initialize job binding
  initJobBinding({
    httpServer,
    allowedSpecsForBinding: [speechLiveflow],
  });
  // [END] Initialize job binding
}

// ...
```

### Step 2: Update the Frontend

#### 2.1 Update `SpeechComponents.tsx`

Update the `SpeechComponents.tsx` file in the `src/client` directory to include the transcription output:

```tsx
// ...
import { useJobBinding, useOutput } from "@livestack/client";
import { speechChunkToTextOutput } from "@livestack/transcribe/client";
// ...

export const SpeechComponents: React.FC = () => {
  // ...

  // [START] Add liveflow job binding and transcription output

  const job = useJobBinding({
    specName: "speech_liveflow",
  });

  const transcription = useOutput({
    job,
    tag: "transcription",
    def: speechChunkToTextOutput,
    query: { type: "lastN", n: 10 },
  });
  // [END] Add transcription output

  // ...

  return (
    <div className="m-4 grid grid-cols-5 gap-2 divide-x">
      {/* ... */}
      {/* [START] Display transcription output */}
      <div className="col-span-2">
        <div className="ml-4">
          <h2 className="text-green-800">
            2. Speech transcripts will pop up here
          </h2>
          <br />
          <article style={{ maxWidth: "100%" }}>
            {transcription.map((transcript, i) => (
              <span key={i} className="text-sm">
                {transcript.data.transcript}
              </span>
            ))}
          </article>
        </div>
      </div>
      {/* [END] Display transcription output */}
      {/* ... */}
    </div>
  );
};

// ...
```

Explanation:
- We use `useOutput` to get the transcription output.


### Step 3: Run the App

Start the development server:

```bash
npm run dev
```

Open your browser and navigate to `http://localhost:4700`. You should now see the transcription output displayed in the UI as you speak.

That's it for Phase 2! You have successfully added the transcription functionality to the app. In the next phase, we'll add the translation feature.