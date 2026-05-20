import { JobSpec, Liveflow, conn, expose } from "@livestack/core";
import { z } from "zod";

const suffix = `github-issues-${Date.now()}`;

const noInputSourceSpec = JobSpec.define({
  name: `${suffix}-no-input-source`,
  output: z.object({ text: z.string() }),
});

const textSinkSpec = JobSpec.define({
  name: `${suffix}-text-sink`,
  input: z.object({ text: z.string() }),
  output: z.object({ done: z.boolean() }),
});

Liveflow.define({
  name: `${suffix}-undefined-input-schema-liveflow`,
  connections: [
    conn({
      from: noInputSourceSpec,
      to: textSinkSpec,
    }),
  ],
  exposures: [expose(textSinkSpec.output.default, "done")],
});

const danglingOutputSpec = JobSpec.define({
  name: `${suffix}-dangling-output`,
  output: z.object({ ignored: z.boolean() }),
});

const missingExposureLiveflow = Liveflow.define({
  name: `${suffix}-missing-exposure-liveflow`,
  connections: [
    conn({
      from: noInputSourceSpec,
      to: textSinkSpec,
    }),
  ],
  exposures: [expose(danglingOutputSpec.output.default, "missing")],
});

if (module === require.main) {
  try {
    missingExposureLiveflow.getDefGraph();
    throw new Error("Expected disconnected exposure to throw");
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    if (
      !message.includes(danglingOutputSpec.name) ||
      !message.includes("not part of liveflow")
    ) {
      throw err;
    }
  }
}
