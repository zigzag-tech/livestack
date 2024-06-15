Certainly! Let's move on to Phase 4, where we'll add the summarization functionality to the app. We'll modify the existing code and add new code to both the frontend and backend.

## Phase 4: Get Summarization to Work

### Step 1: Update the Frontend

#### 1.1 Update `SpeechComponents.tsx`

Update the `SpeechComponents.tsx` file in the `src/client` directory to include the summarized title output:

```tsx
// ...
import { z } from "zod";
// ...

export const SpeechComponents: React.FC = () => {
  // ...

  // [START] Add summarized title output
  const { last: summarizedTitle } = useOutput({
    tag: "summarized-title",
    def: z.object({
      summarizedTitle: z.string(),
    }),
  });
  // [END] Add summarized title output

  // ...

  return (
    <div className="m-4 grid grid-cols-5 gap-2 divide-x">
      {/* ... */}
      <div className="col-span-2">
        <div className="ml-4">
          {/* ... */}
          {/* [START] Display summarized title output */}
          <h2 className="text-blue-800">
            3. Periodically, a one-liner short summary is generated
          </h2>
          <br />
          <p>{summarizedTitle?.data.summarizedTitle}</p>
          <br />
          {/* [END] Display summarized title output */}
          {/* ... */}
        </div>
      </div>
    </div>
  );
};

// ...
```

### Step 2: Update the Backend

#### 2.1 Update `liveflow.speech.ts`

Update the `liveflow.speech.ts` file in the `src/server` directory to include the summarization spec:

```ts
// [START] Import summarization and text splitting specs
import { titleSummarizerSepc } from "@livestack/summarizer/server";
import { textSplittingSpec } from "@livestack/lab-internal-server";
// [END] Import summarization and text splitting specs

// ...

export const speechLiveflow = Liveflow.define({
  name: SPEECH_LIVEFLOW_NAME,
  connections: [
    // ...
    // [START] Add text splitting connection
    conn({
      from: speechChunkToTextSpec,
      transform: ({ transcript }) => transcript,
      to: textSplittingSpec,
    }),
    // [END] Add text splitting connection
    // [START] Add summarization connection
    conn({
      from: textSplittingSpec,
      transform: (chunkText) => ({ transcript: chunkText, llmType: "openai" }),
      to: titleSummarizerSepc,
    }),
    // [END] Add summarization connection
    // ...
  ],
  exposures: [
    // ...
    // [START] Expose summarized title output
    expose(titleSummarizerSepc.output.default, "summarized-title"),
    // [END] Expose summarized title output
    // ...
  ],
});
```

### Step 3: Run the App

Start/restart the development server:

```bash
npm run dev
```

Open your browser and navigate to `http://localhost:4700`. You should now see the summarized title output displayed in the UI periodically as you speak, in addition to the transcription and translation.

That's it for Phase 4! You have successfully added the summarization functionality to the app.

Congratulations! You have completed all four phases of the tutorial and built a real-time speech app with transcription, translation, and summarization features using the Livestack framework.