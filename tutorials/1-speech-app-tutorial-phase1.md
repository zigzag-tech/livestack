## Phase 1: Get the Frontend's Raw PCM Recorder to Work

In this phase, we'll focus on setting up the frontend to record audio using the `usePCMRecorder` hook from the `@livestack/transcribe/client` package. 

By completing this phase, your frontend will be able to capture raw PCM audio data and display a volume indicator as you speak into the microphone, which will be useful for the subsequent phases of the tutorial.

### Step 1: Set Up the Project

Start by creating a new Livestack app:

```bash
npx create-livestack my-livestack-app
cd my-livestack-app
```

### Step 2: Implement the Frontend

#### 2.1 Create `SpeechComponents.tsx`

Create a new file named `SpeechComponents.tsx` in the `src/client` directory with the following code:

```tsx
"use client";
import React from "react";
import { usePCMRecorder, encodeToB64 } from "@livestack/transcribe/client";
import { FaStop, FaMicrophone } from "react-icons/fa";

export const SpeechComponents: React.FC = () => {
  const [volume, setVolume] = React.useState<number>(0);

  const { startRecording, stopRecording, isRecording, cumulativeDataSent } =
    usePCMRecorder({
      onDataAvailable: async (data) => {
        const encoded = encodeToB64(data);
        console.log(encoded.slice(0, 10), "length: ", encoded.length);
      },
      onVolumeChange: (volume) => {
        setVolume(volume);
      },
    });

  const handleRecording = isRecording ? stopRecording : startRecording;

  return (
    <div className="m-4">
      <h2 className="text-red-800">1. Click on "Start Recording" button</h2>
      <br />
      <button
        className="btn w-fit rounded border border-gray-800 bg-gray-200 p-2"
        onClick={handleRecording}
      >
        <span style={{ display: "inline-block" }}>
          {isRecording ? "Stop Recording" : "Start Recording"}
        </span>
        <span style={{ display: "inline-block" }}>
          {isRecording ? <FaStop /> : <FaMicrophone />}
        </span>
      </button>
      <div>
        Volume: <span>{volume.toFixed(1)}</span>
        <br />
        <progress value={volume} max={100} style={{ width: "100px" }}></progress>
        <br />
        {typeof cumulativeDataSent !== "undefined" && (
          <>Total data sent: {cumulativeDataSent} bytes</>
        )}
      </div>
    </div>
  );
};

export default SpeechComponents;
```

Explanation:
- The `usePCMRecorder` hook is used to handle audio recording. It provides functions to start and stop recording, as well as the current recording state and the cumulative data sent.
- When data is available (`onDataAvailable` callback), the PCM data is encoded to base64 using the `encodeToB64` function, and the length of the encoded data is logged to the console.
- The volume level is updated using the `onVolumeChange` callback and stored in the `volume` state variable.
- The "Start Recording" button toggles between starting and stopping the recording based on the `isRecording` state.
- The volume level and cumulative data sent are displayed below the button.

#### 2.2 Update `index.tsx`

Update the `src/client/index.tsx` file to render the `SpeechComponents`:

```tsx
import React, { Suspense } from "react";
import ReactDOM from "react-dom/client";
import SpeechComponents from "./SpeechComponents";
import "./globals.css";

const root = ReactDOM.createRoot(
  document.getElementById("root") as HTMLElement
);

root.render(
  <SpeechComponents />
);
```

Explanation:
- We import the `SpeechComponents` component and render it within a `Suspense` component.
- We import `global.css` to apply global tailwind styles.

### Step 3: Set Up the Backend

#### 3.1 Create `index.ts`

Create a new file named `index.ts` in the `src/server` directory with the following code:

```ts
import express from "express";
import ViteExpress from "vite-express";

async function main() {
  const app = express();
  const PORT = 4700;

  ViteExpress.listen(app, PORT, () => {
    console.info(`Server running on http://localhost:${PORT}.`);
  });
}

main();
```

Explanation:
- We create an Express app and configure it with CORS, JSON body parsing, and serving static files from the `public` directory.
- We set the port to `4700` and start the server using `ViteExpress.listen()`.

### Step 4: Run the App

Now that we have set up the backend, let's run the app:

```bash
npm run dev
```

Note that `dev` script is defined in the `scripts` section in the `package.json` file.

```json
{
  "scripts": {
    "dev": "ts-node -r dotenv/config --project tsconfig.json src/server/index.ts"
  }
}
```

Open your browser and navigate to `http://localhost:4700`. You should see the "Start Recording" button. When you click on it, the volume indicator will show a green bar as you speak to the microphone, and the length of the audio (PCM) chunks generated will be logged to the console.


That's it for Phase 1! You have successfully set up the frontend to record audio and display the volume indicator. In the next phase, we'll add the transcription functionality.