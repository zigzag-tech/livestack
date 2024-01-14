import { InferInputType, InferOutputType } from "../jobs/ZZJobSpec";
import { InferStreamSetType } from "../jobs/StreamDefSet";
import { CheckSpec, deriveStreamId, ZZJobSpec } from "../jobs/ZZJobSpec";
import { z } from "zod";

export type CheckArray<T> = T extends Array<infer V> ? Array<V> : never;

export type JobSpecAndJobParams<JobSpec> = {
  spec: CheckSpec<JobSpec>;
  jobParams: z.infer<CheckSpec<JobSpec>["jobParams"]>;
  jobLabel?: string;
};
export interface JobConnector<Spec1, Spec2> {
  from:
    | CheckSpec<Spec1>
    | [
        Spec1,
        keyof InferStreamSetType<CheckSpec<Spec1>["outputDefSet"]> | "default"
      ];

  to:
    | CheckSpec<Spec2>
    | [
        Spec2,
        keyof InferStreamSetType<CheckSpec<Spec2>["outputDefSet"]> | "default"
      ];

  transform?: (
    spec1Out: InferStreamSetType<CheckSpec<Spec1>["outputDefSet"]>,
    spec2In: InferStreamSetType<CheckSpec<Spec2>["inputDefSet"]>
  ) => void;
}

export class ZZWorkflowSpec<Specs> {
  public readonly name: string;

  public readonly jobs: {
    [K in keyof CheckArray<Specs>]: JobSpecAndJobParams<CheckArray<Specs>[K]>;
  };
  public readonly jobConnectors: JobConnector<
    ZZJobSpec<any, any, any, any>,
    ZZJobSpec<any, any, any, any>
  >[];
  constructor({
    jobs,
    jobConnectors,
    name,
  }: {
    name: string;
    jobs: {
      [K in keyof CheckArray<Specs>]: JobSpecAndJobParams<CheckArray<Specs>[K]>;
    };
    jobConnectors: JobConnector<
      ZZJobSpec<any, any, any, any>,
      ZZJobSpec<any, any, any, any>
    >[];
  }) {
    this.jobs = jobs;
    this.jobConnectors = jobConnectors;
    this.name = name;
  }

  public async request({
    jobGroupId,
    lazyJobCreation = false,
  }: {
    jobGroupId: string;
    lazyJobCreation?: boolean;
  }) {
    if (lazyJobCreation) {
      throw new Error("Lazy job creation is not supported yet.");
    }

    const inOverridesByIndex = [] as {
      [K in keyof CheckArray<Specs>]: Partial<
        Record<
          keyof CheckSpec<CheckArray<Specs>[K]>["inputDefSet"]["defs"],
          string
        >
      >;
    };
    const outOverridesByIndex = [] as {
      [K in number]: Partial<
        Record<
          keyof CheckSpec<CheckArray<Specs>[K]>["outputDefSet"]["defs"],
          string
        >
      >;
    };

    // calculate overrides based on jobConnectors
    for (const connector of this.jobConnectors) {
      const fromPairs = Array.isArray(connector.from)
        ? connector.from
        : ([connector.from, "default"] as const);
      const toPairs = Array.isArray(connector.to)
        ? connector.to
        : ([connector.to, "default"] as const);
      const [fromP] = fromPairs;
      const fromKey =
        fromPairs[1] as keyof (typeof fromP)["outputDefSet"]["defs"];
      const [toP] = toPairs;
      const toKey = toPairs[1] as keyof (typeof toP)["inputDefSet"]["defs"];

      const fromKeyStr = String(fromKey);
      const toKeyStr = String(toKey);

      const fromJobIndex = (
        this.jobs as JobSpecAndJobParams<unknown>[]
      ).findIndex((j) => j.spec === fromP);
      const toJobIndex = (
        this.jobs as JobSpecAndJobParams<unknown>[]
      ).findIndex((j) => j.spec === toP);

      if (fromJobIndex === -1) {
        throw new Error(
          `Invalid jobConnector: ${fromP.name}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "from" job not in the jobs list.`
        );
      }
      const fromJobDecs = this.jobs[fromJobIndex];
      if (toJobIndex === -1) {
        throw new Error(
          `Invalid jobConnector: ${fromP.name}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "to" job not in the jobs list.`
        );
      }
      const toJobDesc = this.jobs[toJobIndex];
      if (!fromJobDecs.spec.outputDefSet.hasDef(fromKeyStr)) {
        throw new Error(
          `Invalid jobConnector: ${fromP}/${fromKeyStr} >> ${toP.name}/${String(
            toKey
          )}: "from" key "${fromKeyStr}" not found.`
        );
      }
      if (!toJobDesc.spec.inputDefSet.hasDef(toKeyStr)) {
        throw new Error(
          `Invalid jobConnector: ${fromP}/${String(fromKey)} >> ${
            toP.name
          }/${String(toKey)}: "to" key "${toKeyStr}" not found.`
        );
      }

      // TODO: to bring back this check
      // const fromDef = fromJobDecs.spec.outputDefSet.getDef(fromKeyStr);
      // const toDef = toJobDesc.spec.inputDefSet.getDef(toKeyStr);
      // if (hashDef(fromDef) !== hashDef(toDef)) {
      //   const msg = `Streams ${fromP.name}.${fromKeyStr} and ${toP.name}.${toKeyStr} are incompatible.`;
      //   console.error(
      //     msg,
      //     "Upstream schema: ",
      //     zodToJsonSchema(fromDef),
      //     "Downstream schema: ",
      //     zodToJsonSchema(toDef)
      //   );
      //   throw new Error(msg);
      // }
      // validate that the types match
      const commonStreamName = deriveStreamId({
        groupId: `[${jobGroupId}]`,
        from: {
          jobSpec: fromP,
          key: fromKeyStr,
        },
        to: {
          jobSpec: toP,
          key: toKeyStr,
        },
        // TODO: fix typing
      } as any);

      inOverridesByIndex[toJobIndex] = {
        ...inOverridesByIndex[toJobIndex],
        [toKeyStr]: commonStreamName,
      };

      outOverridesByIndex[fromJobIndex] = {
        ...outOverridesByIndex[fromJobIndex],
        [fromKeyStr]: commonStreamName,
      };
    }

    // keep count of job with the same name
    const countByName: { [k: string]: number } = {};
    const jobIdsBySpecName: { [k: string]: string[] } = {};

    for (let i = 0; i < Object.keys(this.jobs).length; i++) {
      // calculate overrides based on jobConnectors
      const { spec: jobSpec, jobParams } = this.jobs[i];

      if (!countByName[jobSpec.name]) {
        countByName[jobSpec.name] = 0;
      } else {
        countByName[jobSpec.name] += 1;
      }
      const jobId = `[${jobGroupId}]${jobSpec.name}${
        countByName[jobSpec.name] > 0 ? `-${countByName[jobSpec.name]}` : ""
      }`;

      jobIdsBySpecName[jobSpec.name] = [
        ...(jobIdsBySpecName[jobSpec.name] || []),
        jobId,
      ];

      const jDef = {
        jobId,
        jobParams,
        inputStreamIdOverridesByKey: inOverridesByIndex[i],
        outputStreamIdOverridesByKey: outOverridesByIndex[i],
      };
      await jobSpec.requestJob(jDef);
    }

    const identifySingleJobIdBySpec = <P, I, O, TP>(
      spec: ZZJobSpec<P, I, O, TP>
    ) => {
      const jobs = jobIdsBySpecName[spec.name];
      if (jobs.length > 1) {
        throw new Error(
          `More than one job with spec ${spec.name} detected. Please use jobIdsBySpecName instead.`
        );
      }
      const [job] = jobs;
      return job;
    };

    // Create interfaces for inputs and outputs
    const inputs = {
      bySpec: <P, I, O, TP>(spec: ZZJobSpec<P, I, O, TP>) => {
        return spec._deriveInputsForJob(identifySingleJobIdBySpec(spec));
      },
    };

    const outputs = {
      bySpec: <P, I, O, TP>(spec: ZZJobSpec<P, I, O, TP>) => {
        return spec._deriveOutputsForJob(identifySingleJobIdBySpec(spec));
      },
    };

    // console.log("countByName", countByName);
    return new ZZWorkflow({
      jobIdsBySpecName,
      inputs,
      outputs,
      jobGroupDef: this,
    });
  }
}

export class ZZWorkflow<Specs> {
  public readonly jobIdsBySpecName: { [k: string]: string[] };
  public readonly inputs: {
    bySpec: <P, I, O, TP>(
      spec: ZZJobSpec<P, I, O, TP>
    ) => ReturnType<ZZJobSpec<P, I, O, TP>["_deriveInputsForJob"]>;
  };
  public readonly outputs: {
    bySpec: <P, I, O, TP>(
      spec: ZZJobSpec<P, I, O, TP>
    ) => ReturnType<ZZJobSpec<P, I, O, TP>["_deriveOutputsForJob"]>;
  };
  public readonly jobGroupDef: ZZWorkflowSpec<Specs>;
  constructor({
    jobIdsBySpecName,
    inputs,
    outputs,
    jobGroupDef,
  }: {
    jobGroupDef: ZZWorkflowSpec<Specs>;
    jobIdsBySpecName: { [k: string]: string[] };
    inputs: {
      bySpec: <P, I, O, TP>(
        spec: ZZJobSpec<P, I, O, TP>
      ) => ReturnType<ZZJobSpec<P, I, O, TP>["_deriveInputsForJob"]>;
    };
    outputs: {
      bySpec: <P, I, O, TP>(
        spec: ZZJobSpec<P, I, O, TP>
      ) => ReturnType<ZZJobSpec<P, I, O, TP>["_deriveOutputsForJob"]>;
    };
  }) {
    this.jobIdsBySpecName = jobIdsBySpecName;
    this.inputs = inputs;
    this.outputs = outputs;
    this.jobGroupDef = jobGroupDef;
  }

  public static define<Specs>({
    jobs,
    jobConnectors,
    name,
  }: {
    name: string;
    jobs: {
      [K in keyof CheckArray<Specs>]: JobSpecAndJobParams<CheckArray<Specs>[K]>;
    };

    jobConnectors: ReturnType<
      typeof connect<
        ZZJobSpec<any, any, any, any>,
        ZZJobSpec<any, any, any, any>,
        any,
        any
      >
    >[];
  }) {
    return new ZZWorkflowSpec<Specs>({ name, jobs, jobConnectors });
  }

  public static connect = connect;
}

export function connect<
  Spec1,
  Spec2,
  K1 extends keyof CheckSpec<Spec1>["outputDefSet"]["defs"],
  K2 extends keyof CheckSpec<Spec2>["inputDefSet"]["defs"]
>({
  from,
  to,
  transform,
}: {
  from: Spec1;
  to: Spec2;
  transform?: (
    spec1Out: NonNullable<InferOutputType<Spec1, K1>>
  ) => NonNullable<InferInputType<Spec2, K2>>;
}): {
  from: Spec1;
  to: Spec2;
  transform?: (
    spec1Out: NonNullable<InferOutputType<Spec1, K1>>
  ) => NonNullable<InferInputType<Spec2, K2>>;
} {
  return {
    from,
    to,
    transform,
  };
}

// export async function requestJobGroup<Specs>({
//   jobGroupId,
//   jobs,
//   jobConnectors,
// }: {
//   jobGroupId: string;
//   jobs: {
//     [K in keyof CheckArray<Specs>]: JobSpecAndJobParams<CheckArray<Specs>[K]>;
//   };

//   jobConnectors: JobConnector<
//     ZZJobSpec<any, any, any, any>,
//     ZZJobSpec<any, any, any, any>
//   >[];
// }) {
//   const inOverridesByIndex = [] as {
//     [K in keyof CheckArray<Specs>]: Partial<
//       Record<
//         keyof CheckSpec<CheckArray<Specs>[K]>["inputDefSet"]["defs"],
//         string
//       >
//     >;
//   };
//   const outOverridesByIndex = [] as {
//     [K in number]: Partial<
//       Record<
//         keyof CheckSpec<CheckArray<Specs>[K]>["outputDefSet"]["defs"],
//         string
//       >
//     >;
//   };

//   // calculate overrides based on jobConnectors
//   for (const connector of jobConnectors) {
//     const fromPairs = Array.isArray(connector.from)
//       ? connector.from
//       : ([connector.from, "default"] as const);
//     const toPairs = Array.isArray(connector.to)
//       ? connector.to
//       : ([connector.to, "default"] as const);
//     const [fromP] = fromPairs;
//     const fromKey =
//       fromPairs[1] as keyof (typeof fromP)["outputDefSet"]["defs"];
//     const [toP] = toPairs;
//     const toKey = toPairs[1] as keyof (typeof toP)["inputDefSet"]["defs"];

//     const fromKeyStr = String(fromKey);
//     const toKeyStr = String(toKey);

//     const fromJobIndex = (jobs as JobSpecAndJobParams<unknown>[]).findIndex(
//       (j) => j.spec === fromP
//     );
//     const toJobIndex = (jobs as JobSpecAndJobParams<unknown>[]).findIndex(
//       (j) => j.spec === toP
//     );

//     if (fromJobIndex === -1) {
//       throw new Error(
//         `Invalid jobConnector: ${fromP.name}/${String(fromKey)} >> ${
//           toP.name
//         }/${String(toKey)}: "from" job not in the jobs list.`
//       );
//     }
//     const fromJobDecs = jobs[fromJobIndex];
//     if (toJobIndex === -1) {
//       throw new Error(
//         `Invalid jobConnector: ${fromP.name}/${String(fromKey)} >> ${
//           toP.name
//         }/${String(toKey)}: "to" job not in the jobs list.`
//       );
//     }
//     const toJobDesc = jobs[toJobIndex];
//     if (!fromJobDecs.spec.outputDefSet.hasDef(fromKeyStr)) {
//       throw new Error(
//         `Invalid jobConnector: ${fromP}/${fromKeyStr} >> ${toP.name}/${String(
//           toKey
//         )}: "from" key "${fromKeyStr}" not found.`
//       );
//     }
//     if (!toJobDesc.spec.inputDefSet.hasDef(toKeyStr)) {
//       throw new Error(
//         `Invalid jobConnector: ${fromP}/${String(fromKey)} >> ${
//           toP.name
//         }/${String(toKey)}: "to" key "${toKeyStr}" not found.`
//       );
//     }

//     // TODO: to bring back this check
//     // const fromDef = fromJobDecs.spec.outputDefSet.getDef(fromKeyStr);
//     // const toDef = toJobDesc.spec.inputDefSet.getDef(toKeyStr);
//     // if (hashDef(fromDef) !== hashDef(toDef)) {
//     //   const msg = `Streams ${fromP.name}.${fromKeyStr} and ${toP.name}.${toKeyStr} are incompatible.`;
//     //   console.error(
//     //     msg,
//     //     "Upstream schema: ",
//     //     zodToJsonSchema(fromDef),
//     //     "Downstream schema: ",
//     //     zodToJsonSchema(toDef)
//     //   );
//     //   throw new Error(msg);
//     // }
//     // validate that the types match
//     const commonStreamName = deriveStreamId({
//       groupId: `[${jobGroupId}]`,
//       from: {
//         jobSpec: fromP,
//         key: fromKeyStr,
//       },
//       to: {
//         jobSpec: toP,
//         key: toKeyStr,
//       },
//       // TODO: fix typing
//     } as any);

//     inOverridesByIndex[toJobIndex] = {
//       ...inOverridesByIndex[toJobIndex],
//       [toKeyStr]: commonStreamName,
//     };

//     outOverridesByIndex[fromJobIndex] = {
//       ...outOverridesByIndex[fromJobIndex],
//       [fromKeyStr]: commonStreamName,
//     };
//   }

//   // keep count of job with the same name
//   const countByName: { [k: string]: number } = {};
//   const jobIdsBySpecName: { [k: string]: string[] } = {};

//   for (let i = 0; i < Object.keys(jobs).length; i++) {
//     // calculate overrides based on jobConnectors
//     const { spec: jobSpec, jobParams } = jobs[i];

//     if (!countByName[jobSpec.name]) {
//       countByName[jobSpec.name] = 0;
//     } else {
//       countByName[jobSpec.name] += 1;
//     }
//     const jobId = `[${jobGroupId}]${jobSpec.name}${
//       countByName[jobSpec.name] > 0 ? `-${countByName[jobSpec.name]}` : ""
//     }`;

//     jobIdsBySpecName[jobSpec.name] = [
//       ...(jobIdsBySpecName[jobSpec.name] || []),
//       jobId,
//     ];

//     const jDef = {
//       jobId,
//       jobParams,
//       inputStreamIdOverridesByKey: inOverridesByIndex[i],
//       outputStreamIdOverridesByKey: outOverridesByIndex[i],
//     };
//     await jobSpec.requestJob(jDef);
//   }

//   const identifySingleJobIdBySpec = <P, I, O, TP>(
//     spec: ZZJobSpec<P, I, O, TP>
//   ) => {
//     const jobs = jobIdsBySpecName[spec.name];
//     if (jobs.length > 1) {
//       throw new Error(
//         `More than one job with spec ${spec.name} detected. Please use jobIdsBySpecName instead.`
//       );
//     }
//     const [job] = jobs;
//     return job;
//   };

//   // Create interfaces for inputs and outputs
//   const inputs = {
//     bySpec: <P, I, O, TP>(spec: ZZJobSpec<P, I, O, TP>) => {
//       return spec._deriveInputsForJob(identifySingleJobIdBySpec(spec));
//     },
//   };

//   const outputs = {
//     bySpec: <P, I, O, TP>(spec: ZZJobSpec<P, I, O, TP>) => {
//       return spec._deriveOutputsForJob(identifySingleJobIdBySpec(spec));
//     },
//   };

//   // console.log("countByName", countByName);
//   return { jobIdsBySpecName, inputs, outputs };
// }
