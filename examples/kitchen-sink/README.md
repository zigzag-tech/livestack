# Livestack Project Setup Guide

This README details the setup process for a Livestack project, focusing on the initialization of environment configurations, job specifications, worker definitions, liveflow definitions, starting workers, and initializing job bindings for frontend connections.

## Core Components

### LiveEnv Configuration

`LiveEnv` is a central configuration object in Livestack that holds environment-specific settings such as project IDs and storage providers. It is initialized at the start of your application.

```javascript
const liveEnv = await LiveEnv.create({
  projectUuid: "example-project-id", // Unique project ID
  storageProvider: getLocalTempFileStorageProvider("/tmp/example-project"), // Local storage provider (optional, used for large files)
});
LiveEnv.setGlobal(liveEnv); // Setting the global environment
```

### Job Specifications (JobSpec) and Worker Definitions (WorkerDef)

In a Livestack project, defining job specifications (`JobSpec`) and worker definitions (`WorkerDef`) is essential for orchestrating the job processing liveflow. These configurations detail the input and output expectations for each job, as well as the processing logic to be executed by workers.

```javascript
const { jobSpec, workerDef } = getJobSpecAndWorkerDef({ liveEnv }); // Define your job spec and worker def
```

#### Job Specifications (JobSpec)

Job Specifications define the structure and requirements of jobs that your application can execute, specifying the inputs they accept and the outputs they produce.

```javascript
const jobSpec = new JobSpec({
  liveEnv,
  name: "example-job",
  input: z.string(),
  output: {
    "output-stream-1": z.string(),
    "output-stream-2": z.object({ status: z.string() }),
  },
});
```

- **liveEnv**: The environment configuration object that provides context and settings for the job.
- **name**: A unique identifier for the job specification.
- **input**: An object mapping tags to their respective input data types, defining what types of input the job can receive.
- **output**: An object mapping tags to their output data types, specifying the types of output the job will produce.

This configuration enables the Livestack system to understand what data is expected to be processed and what results will be generated, facilitating communication between different parts of your application.

#### Worker Definitions (`WorkerDef`)

Worker Definitions (`WorkerDef`) encapsulate the logic for processing jobs based on their specifications. Each worker is associated with a specific `JobSpec` and contains the processing instructions.

```javascript
const workerDef = LiveWorker.define({
  jobSpec: jobSpec,
  processor: async ({ input, output, logger }) => {
    // Processing logic here
    for await (const data of input) {
      // do something
      await output.emit("Processed some input");
    }
  }
});
```

Each worker is started by calling `workerDef.startWorker()` on the worker definition. This method initiates the worker and begins listening for jobs matching its specification.

```javascript
workerDef.startWorker();
```

### Advanced Usage: Liveflow Definitions (LiveflowDef)

Liveflow Definitions orchestrate a series of job specs and workers to accomplish complex tasks. Liveflows define the connections between different job specs and outline the data flow between them.

```javascript
const liveflow = Liveflow.define({
  name: "example-liveflow",
  connections: [
    [
      step1JobSpec,
      // Optional, used when the output shape of Step 1 Job doesn't match the input shape of Step 2 Job
      (step1Output) => ({ someStep2InputAttribute: someTransformationFunc(step2Input) }),
      step2JobSpec,
    ],
  ],
});

liveflow.startWorker();
```

Liveflows also have workers that need to be started to process jobs according to the liveflow definition.

### Initializing Job Bindings (initJobBinding)

`initJobBinding` connects the backend job processing and liveflows with the frontend, enabling real-time job execution and management from client-side applications.

```javascript
initJobBinding({
  liveEnv,
  httpServer: server,
  allowedSpecsForBinding: [
    exampleJobSpec
  ],
});
```

This method is crucial for applications that require interaction between the frontend and the job processing backend, such as initiating jobs from the UI and monitoring their progress.

## Setup Process

1. **Initialize your project environment** by configuring `LiveEnv` with project-specific settings.
2. **Define job specifications** for each task your application needs to perform.
3. **Create worker definitions** for processing the jobs specified in your job specs.
4. **Optional: Define liveflows** to orchestrate complex processes involving multiple jobs.
5. **Start all workers and liveflow workers (if applicable)** to begin processing jobs.
6. **Initialize job bindings** to connect your backend processing with the frontend.

# Livestack Frontend Integration Guide

This section of the documentation focuses on integrating Livestack's backend services with a React frontend application. It specifically details the use of Livestack client hooks such as `useJobBinding`, `useInput`, `useOutput`, `useStream`, and `useCumulative` to interact with backend job processing systems in real-time.

## Overview

Livestack provides a suite of React hooks that facilitate the interaction between your frontend application and the backend services. These hooks enable the sending of input to backend jobs, listening for outputs, and managing real-time data streams.

## Prerequisites

Before proceeding, ensure your project is set up with:

- React (version 18 or higher for Hooks support)
- `@livestack/client` package installed in your frontend application
- A Livestack-enabled backend service

## Integration Steps

### 1. Establishing Job Bindings with `useJobBinding`

The `useJobBinding` hook connects the frontend to a specific backend job spec. This connection is essential for sending inputs to and receiving outputs from the backend.

```javascript
const job = useJobBinding({
  specName: "example-job",
});
```

- **specName**: Specifies the name of the job spec to bind to. This should match the name defined in your backend setup.

### 2. Sending Inputs with `useInput`

The `useInput` hook is used to feed data into a job spec. It's particularly useful for submitting user-generated data to the backend for processing.

```javascript
const { feed } = useInput({
  tag: "default", // Optional when you use the "default" input or out stream name
  def: z.string(),
  job,
});
```

- **tag**: A unique identifier for the stream name.
- **def**: The data definition for the input, ensuring type safety and structure.
- **job**: The job binding established with `useJobBinding`.

### 3. Receiving Outputs with `useOutput`

The `useOutput` hook listens for the latest outputs from the backend, such as responses from a chatbot or status updates. If you want to maintain output history as a list, you can use the `useCumulative` hook in addition to the `useOutput` hook.

```javascript
const outputMessage = useOutput({
  tag: "default",
  def: z.string(),
  job,
});

const allOutputMessages = useCumulative(outputMessage);
```

This hook allows the frontend to reactively display information from the backend as it becomes available.  

Alternative to `useOutput` or `useInput`: `useStream`:

```javascript
const outputMessage = useOutput({
  tag: "default",
  def: z.string(),
  job,
});

// equivalent to above
const userMessage = useStream({
  tag: "default",
  def: z.string(),
  job,
  type: "output",
});
```

### Key Functionalities

Integrating Livestack with a React frontend allows for real-time, bidirectional communication between the frontend and backend services. By leveraging hooks like `useJobBinding`, `useInput`, `useOutput`, and `useStream`, developers can build dynamic, interactive applications that respond to user input and display real-time data efficiently.
