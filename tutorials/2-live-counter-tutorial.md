### Real-Time Simple Counter App Tutorial

Hopefully by following the previous tutorial, you have successfully got your own real-time speech app up and running. Now this tutorial will guide you through the creation of another real-time full-stack application using Livestack. We will dive into the details of the server, client, and common components to understand how they work together to provide a seamless real-time experience.

### Quick Start

As always, you can run the following commands to see how the end result is going to look like

```bash
npx create-livestack my-livestack-counter --template typescript-live-counter
cd my-livestack-counter
npm install
npm run dev
```

### Project Structure

Here's a brief overview of our project's structure:
```
my-livestack-counter/
├── src/
│   ├── server/
│   │   └── index.ts
│   ├── client/
│   │   ├── index.tsx
│   │   └── App.tsx
│   └── common/
│       └── defs.ts
├── package.json
├── tsconfig.json
├── index.html
├── .gitignore
└── vite.config.ts
```

### Common Definitions: `src/common/defs.ts`

We define the structure or schema of the data stream used in our application here. This helps in maintaining consistency between the client and server.

```typescript
import { z } from "zod";

// Define the input schema for the increment action
export const incrementInput = z.object({ action: z.literal("increment") });

// Define the output schema for the increment result
export const incrementOutput = z.object({ count: z.number() });

// Define a constant for the incrementer job name
export const INCREMENTER = "incrementer";
```

### Server: `src/server/index.ts`

This is where we set up the backend of our application using Vite-Express and Livestack.

```typescript
import { LiveEnv, JobSpec } from "@livestack/core";
import express from "express";
import ViteExpress from "vite-express";
import { INCREMENTER, incrementInput, incrementOutput } from "../common/defs";
import { initJobBinding } from "@livestack/gateway";
import bodyParser from "body-parser";

// Create a LiveEnv environment with a specified project ID
const liveEnvP = LiveEnv.create({
  projectId: "MY_LIVE_SPEECH_APP",
});

// Define the job specification for the incrementer
const incrementSpec = JobSpec.define({
  name: INCREMENTER,
  input: incrementInput,
  output: incrementOutput,
});

// Define the worker that will process the increment job
const incrementWorker = incrementSpec.defineWorker({
  processor: async ({ input, output }) => {
    let counter = 0;
    for await (const _ of input) {
      counter += 1;
      await output.emit({
        count: counter,
      });
    }
  },
});

async function main() {
  // Set the global LiveEnv instance
  LiveEnv.setGlobal(liveEnvP);

  // Initialize an Express application
  const app = express();
  app.use(bodyParser.json());

  // Define the server port
  const PORT = 3000;

  // Start the ViteExpress server
  const server = ViteExpress.listen(app, PORT, () =>
    console.log(`Live counter server listening on http://localhost:${PORT}.`)
  );

  // Initialize job binding with the server and allowed specs
  initJobBinding({
    httpServer: server,
    allowedSpecsForBinding: [incrementSpec],
  });
}

// Start the server if this file is run directly
if (require.main === module) {
  main();
}
```

#### Key Components of the Server

1. **LiveEnv**: This initializes the Livestack environment, which is crucial for running our real-time jobs.
2. **JobSpec**: Defines the job's specifications, including the input and output schemas.
3. **LiveWorker**: Processes the job by incrementing a counter and emitting the result.
4. **Express and ViteExpress**: Sets up an Express server integrated with Vite for seamless development and production builds.
5. **initJobBinding**: Binds the job to the server, enabling the real-time processing of tasks.

### Client: `src/client/index.tsx`

This file initializes the React application.

```typescript
import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";

// Render the App component into the root element
ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

### Client Application Component: `src/client/App.tsx`

This is where the client-side logic resides. It uses Livestack hooks to interact with the server.

```typescript
"use client";
import React from "react";
import { useInput, useJobBinding, useOutput } from "@livestack/client";
import { INCREMENTER, incrementInput, incrementOutput } from "../common/defs";

export function App() {
  // Bind to the incrementer job
  const job = useJobBinding({
    specName: INCREMENTER,
  });

  // Get the latest count from the output
  const { last: currCount } = useOutput({
    tag: "default",
    def: incrementOutput,
    job,
  });

  // Feed increment actions to the input
  const { feed } = useInput({ tag: "default", def: incrementInput, job });

  return (
    <div className="App">
      <button onClick={() => feed && feed({ action: "increment" })}>
        Click me
      </button>
      <div>{currCount?.data.count || `No count, click the button!`}</div>
    </div>
  );
}
```

#### Key Components of the Client

1. **useJobBinding**: Binds to a specific job defined by its name. This hook allows the client to interact with the job on the server.
2. **useInput**: Sends input data to the job. Here, it feeds the increment action to the server.
3. **useOutput**: Receives output data from the job. It listens for updates and displays the current count.

### Putting It All Together

In this tutorial, we have built a real-time full-stack application using Livestack. Let's summarize the key components of Livestack that we used and how they fit into our application:

1. **LiveEnv**:
   - **Purpose**: Initializes the Livestack environment, which is essential for setting up the context for running real-time jobs.
   - **Usage**: We created a LiveEnv instance with a project ID and set it globally to be accessible throughout the application.

2. **JobSpec**:
   - **Purpose**: Defines the specification for a job, including its name, input schema, and output schema.
   - **Usage**: We defined an increment job (INCREMENTER) with specific input (incrementInput) and output (incrementOutput) schemas using Zod for validation.

3. **LiveWorker**:
   - **Purpose**: Processes the job as per the defined specification. It handles the logic of the job, such as processing input and emitting output.
   - **Usage**: We defined a worker for the increment job that increments a counter each time it receives an input and emits the updated count as output.

4. **initJobBinding**:
   - **Purpose**: Binds jobs to the server, enabling the real-time processing of tasks and facilitating communication between the client and server.
   - **Usage**: We used initJobBinding to bind our increment job specification to the server, allowing it to process incoming job requests.

5. **useJobBinding (Client-side)**:
   - **Purpose**: Binds a specific job on the client-side, enabling the client to interact with the job on the server.
   - **Usage**: In the React application, we used useJobBinding to bind to the incrementer job, enabling the client to send input and receive output from the server.

6. **useInput (Client-side)**:
   - **Purpose**: Sends input data to the bound job on the server.
   - **Usage**: We used useInput to send increment actions from the client to the server.

7. **useOutput (Client-side)**:
   - **Purpose**: Receives output data from the bound job, allowing the client to react to real-time updates from the server.
   - **Usage**: We used useOutput to listen for updates from the server and display the current count in the React application.

### Summary

By leveraging these Livestack components, we were able to build a robust real-time counter application. Livestack provided the infrastructure for defining, processing, and binding jobs, while ensuring seamless communication between the client and server. This tutorial demonstrated how to:
- Define common data schemas using Zod.
- Set up a real-time server with Express and ViteExpress.
- Implement job processing logic on the server.
- Create a responsive React client that interacts with the server in real-time.

With these building blocks, you can extend this application to more complex real-time scenarios, ensuring a scalable and maintainable codebase. Happy coding with Livestack!
