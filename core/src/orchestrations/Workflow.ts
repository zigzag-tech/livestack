import {
  CheckSpec,
  JobSpec,
  SpecOrName,
  convertSpecOrName,
} from "../jobs/JobSpec";
import { IOSpec, InferTMap, TaggedStreamDef } from "@livestack/shared";
import { AliasNode } from "@livestack/shared/src/graph/DefGraph";
import { z } from "zod";
import { ZZEnv } from "../jobs/ZZEnv";
import _ from "lodash";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import {
  JobNode,
  InstantiatedGraph,
} from "@livestack/shared/src/graph/InstantiatedGraph";
import { TransformFunction } from "@livestack/shared/src/graph/DefGraph";
import { TransformRegistry } from "./TransformRegistry";

type UniqueSpecQuery<P = any, I = any, O = any, IMap = any, OMap = any> =
  | SpecOrNameOrTagged<P, I, O, IMap, OMap>
  | { spec: SpecOrName<P, I, O, IMap, OMap>; label: string };
type SpecOrNameOrTagged<P = any, I = any, O = any, IMap = any, OMap = any> =
  | SpecOrName<P, I, O, IMap, OMap>
  | TagObj<P, I, O, IMap, OMap, any, any>;

type TaggedSpecAndOutlet<P = any, I = any, O = any, IMap = any, OMap = any> =
  | SpecOrNameOrTagged<P, I, O, IMap, OMap>
  | [SpecOrNameOrTagged<P, I, O, IMap, OMap>, keyof OMap | keyof IMap];

type WithTr<P = any, I = any, O = any, IMap = any, OMap = any> =
  | [TaggedSpecAndOutlet<P, I, O, IMap, OMap>]
  | [TaggedSpecAndOutlet<P, I, O, IMap, OMap>, TransformFunction];

type Combos =
  | [...WithTr, TaggedSpecAndOutlet]
  | [...WithTr, ...WithTr, TaggedSpecAndOutlet]
  | [...WithTr, ...WithTr, ...WithTr, TaggedSpecAndOutlet]
  | [[...WithTr, ...WithTr, ...WithTr, ...WithTr, TaggedSpecAndOutlet]]
  | [
      [
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        TaggedSpecAndOutlet
      ]
    ]
  | [
      [
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        TaggedSpecAndOutlet
      ]
    ]
  | [
      [
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        TaggedSpecAndOutlet
      ]
    ]
  | [
      [
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        ...WithTr,
        TaggedSpecAndOutlet
      ]
    ];

export type JobSpecAndJobOptions<JobSpec> = {
  spec: CheckSpec<JobSpec>;
  jobOptions: z.infer<CheckSpec<JobSpec>["jobOptions"]>;
  jobLabel?: string;
};

type SpecAndOutput<P, I, O, IMap, OMap, K extends keyof OMap> = {
  spec: UniqueSpecQuery<P, I, O, IMap, OMap>;
  output?: K;
};

type SpecAndInput<P, I, O, IMap, OMap, K extends keyof IMap> = {
  spec: UniqueSpecQuery<P, I, O, IMap, OMap>;
  input?: K;
};

interface Connection<
  P1 = any,
  I1 = any,
  O1 = any,
  IMap1 = any,
  OMap1 = any,
  P2 = any,
  I2 = any,
  O2 = any,
  IMap2 = any,
  OMap2 = any,
  K1Out extends keyof OMap1 = keyof OMap1,
  K2In extends keyof IMap2 = keyof IMap2
> {
  from:
    | SpecAndOutput<P1, I1, O1, IMap1, OMap1, K1Out>
    | SpecOrName<P1, I1, O1, IMap1, OMap1>;
  to:
    | SpecAndInput<P2, I2, O2, IMap2, OMap2, K2In>
    | SpecOrName<P2, I2, O2, IMap2, OMap2>;
  transform?: NoInfer<TransformFunction<OMap1[K1Out], IMap2[K2In]>>;
}

/**Exampe:
    const workflow = Workflow.define({
    name: "fake-workflow",
    connections: [
      conn({
        from: {
          spec: fakeTextGenSpec,
          output: "combined",
        },
        transform: ({ combined }) => ({ text: combined }),
        to: {
          spec: fakeTextSummarizerSpec,
          input: "text",
        },
      }),
    ],
    exposures: [
      expose({
        spec: fakeTextGenSpec,
        input: {
          "num-stream1": "num",
          "bool-stream2": "bool",
        },
      }),
      expose({
        spec: fakeTextSummarizerSpec,
        output: { default: "text" },
      }),
    ],
  });
 */

export function conn<
  P1,
  I1,
  O1,
  IMap1,
  OMap1,
  P2,
  I2,
  O2,
  IMap2,
  OMap2,
  K1Out extends keyof OMap1,
  K2In extends keyof IMap2
>(
  p: Connection<P1, I1, O1, IMap1, OMap1, P2, I2, O2, IMap2, OMap2, K1Out, K2In>
): CanonicalConnection<P1, I1, O1, IMap1, OMap1, P2, I2, O2, IMap2, OMap2> {
  const from = resolveUniqueSpec(
    typeof p.from === "string" || JobSpec.isJobSpec(p.from)
      ? p.from
      : (p.from as SpecAndOutput<P1, I1, O1, IMap1, OMap1, K1Out>).spec
  );
  const to = resolveUniqueSpec(
    typeof p.to === "string" || JobSpec.isJobSpec(p.to)
      ? p.to
      : (p.to as SpecAndInput<P2, I2, O2, IMap2, OMap2, K2In>).spec
  );
  const fromOutput = !(typeof p.from === "string" || JobSpec.isJobSpec(p.from))
    ? p.from.output
    : null;
  const toInput = !(typeof p.to === "string" || JobSpec.isJobSpec(p.to))
    ? p.to.input
    : null;
  return {
    from: {
      ...from,
      output: (
        fromOutput || (from.spec.getSingleTag("output", true) as K1Out)
      ).toString(),
      tagInSpecType: "output",
    },
    to: {
      ...to,
      input: (
        toInput || (to.spec.getSingleTag("input", true) as K2In)
      ).toString(),
      tagInSpecType: "input",
    },
    transform:
      (p.transform as TransformFunction<OMap1[keyof OMap1], IMap2[K2In]>) ||
      null,
  };
}

interface Exposure<P = any, I = any, O = any, IMap = any, OMap = any> {
  spec: UniqueSpecQuery<P, I, O, IMap, OMap>;
  input?: Partial<Record<keyof IMap, string>>;
  output?: Partial<Record<keyof OMap, string>>;
}

interface CanonicalExposure {
  specName: string;
  uniqueSpecLabel?: string;
  input: Record<string, string>;
  output: Record<string, string>;
}

export function expose<K, T, I, O, IMap, OMap>(
  p: TaggedStreamDef<
    K,
    T,
    { spec: IOSpec<I, O, IMap, OMap>; type: "input" | "output" }
  >,
  alias: string
): CanonicalExposure {
  if (!p.spec || !p.type) {
    throw new Error("spec and type are required.");
  }
  const resolvedSpec = resolveUniqueSpec(
    p.spec as JobSpec<any, I, O, IMap, OMap>
  );
  return {
    specName: resolvedSpec.spec.name,
    uniqueSpecLabel: resolvedSpec.uniqueSpecLabel,
    input: (p.type === "input" ? { [p.tag as string]: alias } : {}) as Record<
      string,
      string
    >,
    output: (p.type === "output" ? { [p.tag as string]: alias } : {}) as Record<
      string,
      string
    >,
  };
}

type WorkflowParams = {
  connections: CanonicalConnection[];
  exposures: CanonicalExposure[];
};

const WorkflowChildJobOptionsSanitized = z.array(
  z.object({
    spec: z.string(),
    params: z.any(),
  })
);

type WorkflowChildJobOptionsSanitized = z.infer<
  typeof WorkflowChildJobOptionsSanitized
>;

const WorkflowJobOptionsSanitized = z.object({
  groupId: z.string(),
  jobOptions: WorkflowChildJobOptionsSanitized.optional(),
});
type WorkflowJobOptionsSanitized = z.infer<typeof WorkflowJobOptionsSanitized>;

export class WorkflowSpec extends JobSpec<
  WorkflowChildJobOptionsSanitized,
  any,
  {
    __zz_workflow_status: z.ZodType<{
      __zz_workflow_status: "finished";
    }>;
  },
  any,
  any
> {
  public readonly connections: CanonicalConnection[];
  public readonly exposures: CanonicalExposure[];
  private workflowGraphCreated: boolean = false;

  public override getDefGraph() {
    const defG = super.getDefGraph();
    if (!this.workflowGraphCreated) {
      const parentSpecNodeId = defG.getRootSpecNodeId();

      // if not number, then throw
      if (typeof parentSpecNodeId !== "number") {
        throw new Error("No parent spec node id found");
      }

      // pass1
      for (const conn of this.connections) {
        const s = defG.addConnectedDualSpecs(
          {
            specName: conn.from.spec.name,
            uniqueSpecLabel: conn.from.uniqueSpecLabel || null,
            output: conn.from.output,
          },
          {
            specName: conn.to.spec.name,
            uniqueSpecLabel: conn.to.uniqueSpecLabel || null,
            input: conn.to.input,
            hasTransform: !!conn.transform,
          }
        );
        // g.ensureParentChildRelation(parentSpecNodeId, fromSpecNodeId);
        // g.ensureParentChildRelation(parentSpecNodeId, toSpecNodeId);

        if (conn.transform) {
          TransformRegistry.registerTransform({
            workflowSpecName: this.name,
            receivingSpecName: conn.to.spec.name,
            receivingSpecUniqueLabel: conn.to.uniqueSpecLabel || null,
            tag: conn.to.input,
            transform: conn.transform,
          });
        }
      }

      // pass2: add all loose tags in specs
      for (const conn of this.connections) {
        for (const c of [conn.from, conn.to]) {
          const spec = c.spec;
          for (const tag of spec.inputTags) {
            defG.ensureInletAndStream(
              {
                specName: spec.name,
                tag: tag.toString(),
                uniqueSpecLabel: c.uniqueSpecLabel || null,
              },
              false
            );
          }
          for (const tag of spec.outputTags) {
            defG.ensureOutletAndStream({
              specName: spec.name,
              tag: tag.toString(),
              uniqueSpecLabel: c.uniqueSpecLabel || null,
            });
          }
        }
      }
      type Rec = Record<
        string,
        {
          specName: string;
          uniqueSpecLabel?: string;
          tag: string;
        }
      >;
      const inputAliasesSoFar: Rec = {};
      const outputAliasesSoFar: Rec = {};

      for (const exposure of this.exposures) {
        for (const [specTag, alias] of Object.entries(exposure.input || {})) {
          if (inputAliasesSoFar[alias]) {
            throw new Error(
              `Input alias "${alias}" already used for input "${inputAliasesSoFar[alias].tag}" of spec "${inputAliasesSoFar[alias].specName}"`
            );
          }
          defG.assignAlias({
            alias,
            tag: specTag,
            specName: exposure.specName,
            uniqueSpecLabel: exposure.uniqueSpecLabel || null,
            direction: "in",
            rootSpecName: this.name,
          });
          inputAliasesSoFar[alias] = {
            specName: exposure.specName,
            uniqueSpecLabel: exposure.uniqueSpecLabel,
            tag: specTag,
          };
        }
        for (const [specTag, alias] of Object.entries(exposure.output || {})) {
          if (outputAliasesSoFar[alias]) {
            throw new Error(
              `Output alias "${alias}" already used for output "${outputAliasesSoFar[alias].tag}" of spec "${outputAliasesSoFar[alias].specName}"`
            );
          }

          defG.assignAlias({
            alias,
            tag: specTag,
            specName: exposure.specName,
            uniqueSpecLabel: exposure.uniqueSpecLabel || null,
            direction: "out",
            rootSpecName: this.name,
          });
          outputAliasesSoFar[alias] = {
            specName: exposure.specName,
            uniqueSpecLabel: exposure.uniqueSpecLabel,
            tag: specTag,
          };
        }
      }

      this.workflowGraphCreated = true;
    }
    return defG;
  }

  private orchestrationWorkerDef: ZZWorkerDef<
    WorkflowChildJobOptionsSanitized,
    any,
    any,
    any,
    any,
    any
  >;

  constructor({
    connections,
    exposures,
    name,
    zzEnv,
  }: {
    name: string;
    zzEnv?: ZZEnv;
  } & WorkflowParams) {
    super({
      name,
      jobOptions: WorkflowChildJobOptionsSanitized,
      zzEnv,
      output: {
        __zz_workflow_status: z.object({
          __zz_workflow_status: z.literal("finished"),
        }),
      },
    });
    // const canonicalConns = convertConnectionsCanonical({
    //   connections,
    // });
    this.connections = connections;
    this.exposures = exposures;

    this._validateConnections();

    this.orchestrationWorkerDef = new ZZWorkerDef({
      workerPrefix: "workflow",
      jobSpec: this,
      processor: async ({ jobOptions: childrenJobOptions, jobId, output }) => {
        const groupId = jobId;

        const { rec: parentRec } = await(
          await ZZEnv.vaultClient()
        ).db.getParentJobRec({
          projectId: (await this.zzEnvPWithTimeout).projectId,
          childJobId: groupId,
        });
        const inletHasTransformOverridesByTag: Record<string, boolean> = {};
        if (parentRec) {
          for (const tag of this.inputTags) {
            const transform = TransformRegistry.getTransform({
              workflowSpecName: parentRec.spec_name,
              receivingSpecName: this.name,
              receivingSpecUniqueLabel: parentRec.unique_spec_label || null,
              tag: tag.toString(),
            });
            if (!!transform) {
              inletHasTransformOverridesByTag[tag.toString()] = true;
            }
          }
        }
        const instG = new InstantiatedGraph({
          defGraph: this.getDefGraph(),
          contextId: groupId,
          rootJobId: groupId,
          streamIdOverrides: {},
          streamSourceSpecTypeByStreamId: {},
          inletHasTransformOverridesByTag,
        });
        await instG.initPromise;

        const allJobNodes = instG
          .nodes()
          .filter((n) => instG.getNodeAttributes(n).nodeType === "job");
        // remove self
        const jobNodesExceptSelf = allJobNodes.filter((n) => {
          const node = instG.getNodeAttributes(n) as JobNode;
          return node.jobId !== `[${groupId}]${this.name}`;
        });

        const managers = await Promise.all(
          jobNodesExceptSelf.map(async (jobNodeId) => {
            const jobNode = instG.getNodeAttributes(jobNodeId) as JobNode;

            //calculate input and output overrides
            const inputStreamIdOverridesByTag: Record<string, string> = {};
            const outputStreamIdOverridesByTag: Record<string, string> = {};

            // get the stream id overrides for the input
            const inboundEdges = instG.inboundEdges(jobNodeId);
            const inletEdgeIds = inboundEdges.filter((e) => {
              const node = instG.getNodeAttributes(instG.source(e));
              return node.nodeType === "Inlet";
            });
            const inletNodeIds = inletEdgeIds.map((e) => instG.source(e));
            for (const inletNodeId of inletNodeIds) {
              const inletNode = instG.getNodeAttributes(inletNodeId);
              if (inletNode.nodeType !== "Inlet") {
                throw new Error("Expected inlet node");
              }
              const streamToInpetEdgeId = instG.inboundEdges(inletNodeId)[0];
              const streamNodeId = instG.source(streamToInpetEdgeId);
              const streamNode = instG.getNodeAttributes(streamNodeId);
              if (streamNode.nodeType !== "stream") {
                throw new Error("Expected stream node");
              }
              const streamId = streamNode.streamId;
              inputStreamIdOverridesByTag[inletNode.tag] = streamId;
            }

            // get the stream id overrides for the output
            const outboundEdges = instG.outboundEdges(jobNodeId);
            const outletEdgeIds = outboundEdges.filter((e) => {
              const node = instG.getNodeAttributes(instG.target(e));
              return node.nodeType === "Outlet";
            });
            const outletNodeIds = outletEdgeIds.map((e) => instG.target(e));

            for (const outletNodeId of outletNodeIds) {
              const outletNode = instG.getNodeAttributes(outletNodeId);
              if (outletNode.nodeType !== "Outlet") {
                throw new Error("Expected outlet node");
              }

              const streamFromOutputEdgeId =
                instG.outboundEdges(outletNodeId)[0];
              const streamNodeId = instG.target(streamFromOutputEdgeId);
              const streamNode = instG.getNodeAttributes(streamNodeId);
              if (streamNode.nodeType !== "stream") {
                throw new Error("Expected stream node");
              }
              const streamId = streamNode.streamId;
              outputStreamIdOverridesByTag[outletNode.tag] = streamId;
            }

            const childSpecName = jobNode.specName;
            const childJobSpec = JobSpec.lookupByName(childSpecName);

            return await childJobSpec.enqueueJob({
              jobId: jobNode.jobId,
              parentJobId: groupId,
              uniqueSpecLabel: jobNode.uniqueSpecLabel,
              jobOptions: childrenJobOptions?.find(({ spec: specQuery }) => {
                const specInfo = resolveUniqueSpec(specQuery);
                return (
                  specInfo.spec.name === childSpecName &&
                  specInfo.uniqueSpecLabel === jobNode.uniqueSpecLabel
                );
              })?.params,
              inputStreamIdOverridesByTag,
              outputStreamIdOverridesByTag,
            });
          })
        );

        await output("__zz_workflow_status").emit({
          __zz_workflow_status: "finished",
        });

        // TODO: wait for all the child jobs to finish

        await Promise.all(
          managers.map((m) => {
            return m.waitUntilFinish();
          })
        );
      },
    });
  }
  public override get inputTags() {
    // traverse the def graph and get tags from alias nodes
    const defG = this.getDefGraph();
    const aliasNodeIds = Array.from(defG.getAllAliasNodeIds());
    return [
      ...aliasNodeIds
        .map((n) => defG.getNodeAttributes(n) as AliasNode)
        .filter((n) => n.direction === "in")
        .map((n) => n.alias),
    ];
  }

  public override get outputTags() {
    // traverse the def graph and get tags from alias nodes
    const defG = this.getDefGraph();
    const aliasNodeIds = Array.from(defG.getAllAliasNodeIds());
    return [
      ...aliasNodeIds
        .map((n) => defG.getNodeAttributes(n) as AliasNode)
        .filter((n) => n.direction === "out")
        .map((n) => n.alias),
    ];
  }

  protected override convertSpecTagToWorkflowAlias({
    specName,
    tag,
    uniqueSpecLabel,
    type,
  }: {
    specName: string;
    tag: string;
    uniqueSpecLabel?: string;
    type: "in" | "out";
  }) {
    const defG = this.getDefGraph();
    const existingAlias = defG.lookupRootSpecAlias({
      specName,
      tag,
      uniqueSpecLabel: uniqueSpecLabel || null,
      direction: type,
    });
    if (!existingAlias) {
      const alias = `${specName}[${uniqueSpecLabel || ""}]::${type}/${tag}`;
      const defG = this.getDefGraph();
      defG.assignAlias({
        alias,
        specName,
        tag,
        uniqueSpecLabel: uniqueSpecLabel || null,
        direction: type,
        rootSpecName: this.name,
      });
      return alias;
    } else {
      return existingAlias;
    }
  }

  protected override convertWorkflowAliasToSpecTag({
    type,
    alias,
  }: {
    type: "in" | "out";
    alias: string | symbol | number;
  }) {
    // from the root spec level
    if (
      (type === "in"
        ? Object.keys(this.input)
        : Object.keys(this.output)
      ).includes(alias.toString())
    ) {
      return {
        specName: this.name,
        tag: alias.toString(),
        direction: type,
        uniqueSpecLabel: undefined,
      };
    } else {
      const defG = this.getDefGraph();
      const r = defG.lookupSpecAndTagByAlias(alias.toString(), type)! as {
        specName: string;
        tag: string;
        uniqueSpecLabel: string | undefined;
        direction: "in" | "out";
      };
      if (!r) {
        throw new Error(
          `No spec and tag of type ${
            type === "in" ? "input" : "output"
          } found for alias ${alias.toString()}.`
        );
      }
      return r;
    }
  }

  private _validateConnections() {
    // calculate overrides based on jobConnectors
    for (let i = 0; i < this.connections.length; i++) {
      const outSpecInfo = this.connections[i].from;
      const inSpecInfo = this.connections[i].to;
      validateSpecHasKey({
        spec: outSpecInfo.spec,
        type: "output",
        tag: outSpecInfo.output,
      });
      validateSpecHasKey({
        spec: inSpecInfo.spec,
        type: "input",
        tag: inSpecInfo.input,
      });

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
    }
  }

  protected override async lookUpChildJobIdByGroupIDAndSpecTag({
    groupId,
    specInfo,
  }: {
    groupId: string;
    specInfo: {
      specName: string;
      uniqueSpecLabel?: string;
    };
  }) {
    let workflow = Workflow.lookupById(groupId);
    if (!workflow) {
      workflow = new Workflow({
        jobGroupId: groupId,
        jobGroupDef: this,
      });
    }

    const instaG = await workflow.graphP;

    const childJobNodeId = instaG.findNode((n) => {
      const node = instaG.getNodeAttributes(n);
      if (node.nodeType !== "job" && node.nodeType !== "root-job") {
        return false;
      } else {
        return (
          node.specName === specInfo.specName &&
          (node.nodeType === "root-job" ||
            (node.uniqueSpecLabel || null) ===
              (specInfo.uniqueSpecLabel || null))
        );
      }
    });
    if (!childJobNodeId) {
      throw new Error(
        `No child job found for ${groupId} and ${specInfo.specName}`
      );
    }
    return (instaG.getNodeAttributes(childJobNodeId) as JobNode).jobId;
  }

  public async startWorker(
    p?: Parameters<typeof this.orchestrationWorkerDef.startWorker>[0]
  ) {
    await this.orchestrationWorkerDef.startWorker(p);
    return void 0;
  }

  public async enqueueJob(p?: {
    jobId?: string;
    jobOptions?: WorkflowChildJobOptionsSanitized;
  }) {
    let { jobOptions } = p || {};

    // sanitize child job options
    const childJobOptionsSanitized = (jobOptions || []).map((c) => ({
      spec: typeof c.spec === "string" ? c.spec : c.spec,
      params: c.params,
    }));

    const manager = await super.enqueueJob({
      ...p,
      jobOptions: childJobOptionsSanitized,
    });

    const out = await manager.output("__zz_workflow_status");
    // wait on output to finish
    const r = await out.nextValue();

    return manager;
  }
}

export class Workflow {
  private _graphP: Promise<InstantiatedGraph> | null = null;
  public get graphP(): Promise<InstantiatedGraph> {
    const that = this;
    if (!this._graphP) {
      this._graphP = (async () => {
        const { rec: parentRec } = await (
          await ZZEnv.vaultClient()
        ).db.getParentJobRec({
          projectId: (await that.jobGroupDef.zzEnvPWithTimeout).projectId,
          childJobId: this.contextId,
        });
        const inletHasTransformOverridesByTag: Record<string, boolean> = {};
        if (parentRec) {
          for (const tag of that.jobGroupDef.inputTags) {
            const transform = TransformRegistry.getTransform({
              workflowSpecName: parentRec.spec_name,
              receivingSpecName: that.jobGroupDef.name,
              receivingSpecUniqueLabel: parentRec.unique_spec_label || null,
              tag: tag.toString(),
            });
            if (!!transform) {
              inletHasTransformOverridesByTag[tag.toString()] = true;
            }
          }
        }
        const g = new InstantiatedGraph({
          defGraph: this.jobGroupDef.getDefGraph(),
          contextId: this.contextId,
          rootJobId: this.contextId,
          streamIdOverrides: {},
          streamSourceSpecTypeByStreamId: {},
          inletHasTransformOverridesByTag,
        });
        await g.initPromise;
        return g;
      })();
    }
    return this._graphP;
  }
  public readonly jobGroupDef: WorkflowSpec;
  public readonly contextId: string;
  private static _workflowById: Record<string, Workflow> = {};

  constructor({
    jobGroupDef,
    jobGroupId,
  }: {
    jobGroupId: string;
    jobGroupDef: WorkflowSpec;
  }) {
    this.contextId = jobGroupId;
    this.jobGroupDef = jobGroupDef;
    Workflow._workflowById[jobGroupId] = this;
  }

  public static define(p: ConstructorParameters<typeof WorkflowSpec>[0]) {
    return new WorkflowSpec(p);
  }

  public static lookupById(jobGroupId: string) {
    const r = this._workflowById[jobGroupId] as Workflow | undefined;
    return r || null;
  }
}

type CanonicalConnection<
  P1 = any,
  I1 = any,
  O1 = any,
  IMap1 = any,
  OMap1 = any,
  P2 = any,
  I2 = any,
  O2 = any,
  IMap2 = any,
  OMap2 = any
> = {
  from: CanonicalSpecAndOutletFrom<P1, I1, O1, IMap1, OMap1>;
  to: CanonicalSpecAndOutletTo<P2, I2, O2, IMap2, OMap2>;
  transform: TransformFunction<OMap1[keyof OMap1], IMap2[keyof IMap2]> | null;
};

type CanonicalSpecAndOutletBase<P, I, O, IMap, OMap> = {
  spec: JobSpec<P, I, O, IMap, OMap>;
  uniqueSpecLabel?: string;
};
export type CanonicalSpecAndOutletFrom<P, I, O, IMap, OMap> =
  CanonicalSpecAndOutletBase<P, I, O, IMap, OMap> & {
    output: string;
    tagInSpecType: "output";
  };

export type CanonicalSpecAndOutletTo<P, I, O, IMap, OMap> =
  CanonicalSpecAndOutletBase<P, I, O, IMap, OMap> & {
    input: string;
    tagInSpecType: "input";
  };

function validateSpecHasKey<P, I, O, IMap, OMap>({
  spec,
  type,
  tag,
}: {
  spec: IOSpec<I, O, IMap, OMap>;
  type: "input" | "output";
  tag: string;
}) {
  const tags = (type === "input" ? spec.inputTags : spec.outputTags).map((s) =>
    s.toString()
  );
  if (!tags.includes(tag)) {
    throw new Error(
      `Invalid ${type} tag "${tag}" specified for spec "${
        spec.name
      }". Available tags: [${tags.map((s) => `"${s}"`).join(", ")}]`
    );
  }
}

export type TagMaps<I, O, IMap, OMap, IKs, OKs> = {
  inputTag: Partial<Record<keyof IMap, IKs>>;
  outputTag: Partial<Record<keyof OMap, OKs>>;
};

export interface TagObj<P, I, O, IMap, OMap, IKs, OKs> {
  spec: JobSpec<P, I, O, IMap, OMap>;
  uniqueSpecLabel?: string;
  input: <newK extends string>(
    tagOrMap: newK | Partial<Record<keyof InferTMap<I>, newK>>
  ) => TagObj<P, I, O, IMap, OMap, IKs | newK, OKs>;
  output: <newK extends string>(
    tagOrMap: newK | Partial<Record<keyof InferTMap<O>, newK>>
  ) => TagObj<P, I, O, IMap, OMap, IKs, OKs | newK>;
  _aliasMaps: TagMaps<I, O, IMap, OMap, IKs, OKs>;
}

// function expose0<P, I, O, IMap, OMap>(
//   specLike: SpecOrName<P, I, O, IMap, OMap>
// ) {
//   return _tagObj(specLike, {
//     inputTag: {},
//     outputTag: {},
//   } as TagMaps<I, O, IMap, OMap, never, never>);
// }

function _tagObj<P, I, O, IMap, OMap, IKs, OKs>(
  specLike: SpecOrName<P, I, O, IMap, OMap>,
  _aliasMaps: TagMaps<I, O, IMap, OMap, IKs, OKs>
): TagObj<P, I, O, IMap, OMap, IKs, OKs> {
  const aliasMaps = { ..._aliasMaps };
  const specAndLabel = resolveUniqueSpec(specLike);
  return {
    spec: specAndLabel.spec,
    uniqueSpecLabel: specAndLabel.uniqueSpecLabel,
    _aliasMaps: aliasMaps,
    input: <Ks extends string>(
      tagOrMap: Ks | Partial<Record<keyof InferTMap<I>, Ks>>
    ) => {
      const tm = aliasMaps as TagMaps<I, O, IMap, OMap, IKs | Ks, OKs>;
      if (typeof tagOrMap === "string") {
        if (specAndLabel.spec.inputTags.length === 1) {
          const tag = specAndLabel.spec.getSingleTag("input", true);
          tm.inputTag[tag] = tagOrMap;
        } else if (!specAndLabel.spec.inputTags.includes(tagOrMap as any)) {
          throw new Error(
            `Invalid input tag: ${tagOrMap} specified. Available: [${specAndLabel.spec.inputTags.join(
              ", "
            )}]`
          );
        }
        tm.inputTag[tagOrMap as unknown as keyof IMap] = tagOrMap;
      } else {
        tm.inputTag = { ...aliasMaps.inputTag, ...tagOrMap };
      }
      return _tagObj(specAndLabel.spec, tm);
    },
    output: <Ks extends string>(
      tagOrMap: Ks | Partial<Record<keyof InferTMap<O>, string>>
    ) => {
      const tm = aliasMaps as TagMaps<I, O, IMap, OMap, IKs, OKs | Ks>;
      if (typeof tagOrMap === "string") {
        if (specAndLabel.spec.outputTags.length === 1) {
          const tag = specAndLabel.spec.getSingleTag("output", true);
          tm.outputTag[tag] = tagOrMap;
        } else if (!specAndLabel.spec.outputTags.includes(tagOrMap as any)) {
          throw new Error(
            `Invalid output tag: ${tagOrMap} specified. Available: [${specAndLabel.spec.outputTags.join(
              ", "
            )}]`
          );
        }
        tm.outputTag[tagOrMap as unknown as keyof OMap] = tagOrMap;
      } else {
        tm.outputTag = { ...aliasMaps.outputTag, ...tagOrMap };
      }
      return _tagObj(specAndLabel.spec, tm);
    },
  };
}

export function resolveUniqueSpec<P, I, O, IMap, OMap>(
  uniqueSpec: UniqueSpecQuery<P, I, O, IMap, OMap>
): {
  spec: JobSpec<P, I, O, IMap, OMap>;
  uniqueSpecLabel?: string;
} {
  if (typeof uniqueSpec === "string") {
    const spec = JobSpec.lookupByName(uniqueSpec);
    return {
      spec,
    };
  } else if ("spec" in (uniqueSpec as any) && "label" in (uniqueSpec as any)) {
    const spec = convertSpecOrName(
      (
        uniqueSpec as {
          spec: SpecOrNameOrTagged;
          label: string;
        }
      ).spec
    );

    return {
      spec,
      uniqueSpecLabel: (
        uniqueSpec as {
          spec: SpecOrNameOrTagged;
          label: string;
        }
      ).label,
    };
  } else {
    const spec = convertSpecOrName(uniqueSpec as any);
    return {
      spec,
    };
  }
}

