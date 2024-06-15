## Phase 3: Get Translation to Work

### Step 1: Update the Frontend

#### 1.1 Update `SpeechComponents.tsx`

Update the `SpeechComponents.tsx` file in the `src/client` directory to include the translation output:

```tsx
// ...
import { translationOutputSchema } from "@livestack/lab-internal-common";
// ...

export const SpeechComponents: React.FC = () => {
  // ...

  // [START] Add translation output
  const translation = useOutput({
    tag: "translation",
    def: translationOutputSchema,
    query: { type: "lastN", n: 10 },
  });
  // [END] Add translation output

  // ...

  return (
    <div className="m-4 grid grid-cols-5 gap-2 divide-x">
      {/* ... */}
      <div className="col-span-2">
        <div className="ml-4">
          {/* ... */}
          {/* [START] Display translation output */}
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
          {/* [END] Display translation output */}
        </div>
      </div>
    </div>
  );
};

// ...
```

### Step 2: Update the Backend

#### 2.1 Update `liveflow.speech.ts`

Update the `liveflow.speech.ts` file in the `src/server` directory to include the translation spec:

```ts
// [START] Import translation spec
import { translationSpec } from "@livestack/translate-server";
// [END] Import translation spec

// ...

export const speechLiveflow = Liveflow.define({
  name: SPEECH_LIVEFLOW_NAME,
  connections: [
    // ...
    // [START] Add translation connection
    conn({
      from: speechChunkToTextSpec,
      transform: ({ transcript }) => ({
        toLang: "French",
        text: transcript,
        llmType: "openai",
      }),
      to: translationSpec,
    }),
    // [END] Add translation connection
  ],
  exposures: [
    // ...
    // [START] Expose translation output
    expose(translationSpec.output.default, "translation"),
    // [END] Expose translation output
  ],
});
```

### Step 3: Run the App

Start the development server:

```bash
npm run dev
```

Open your browser and navigate to `http://localhost:4700`. You should now see the translation output displayed in the UI as you speak, in addition to the transcription.

That's it for Phase 3! You have successfully added the translation functionality to the app. In the next phase, we'll add the summarization feature.