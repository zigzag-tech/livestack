import { vaultClient } from "@livestack/vault-client";
import {
  CheckSpec,
  JobSpec,
  SpecOrName,
  convertSpecOrName,
} from "../jobs/JobSpec";
import { AliasNode, IOSpec, InferTMap } from "@livestack/shared";
import { z } from "zod";
import { ZZEnv } from "../jobs/ZZEnv";
import _ from "lodash";
import { ZZWorkerDef } from "../jobs/ZZWorker";
import { JobNode, InstantiatedGraph } from "@livestack/shared";
import { resolveTagMapping } from "../jobs/JobSpec";
import { TransformFunction } from "@livestack/shared";
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

type WorkflowParams = {
  connections: (
    | {
        from: TaggedSpecAndOutlet;
        to: TaggedSpecAndOutlet;
      }
    | Combos
  )[];
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
  private workflowGraphCreated: boolean = false;

  public override getDefGraph() {
    const g = super.getDefGraph();
    if (!this.workflowGraphCreated) {
      const parentSpecNodeId = g.getRootSpecNodeId();
      if (!parentSpecNodeId) {
        throw new Error("No parent spec node id found");
      }

      // pass1
      for (const conn of this.connections) {
        const { fromSpecNodeId, toSpecNodeId, transformsToRegister } =
          g.addConnectedDualSpecs(conn.from, conn.to);
        g.ensureParentChildRelation(parentSpecNodeId, fromSpecNodeId);
        g.ensureParentChildRelation(parentSpecNodeId, toSpecNodeId);

        for (const {
          specName,
          uniqueSpecLabel,
          ...rest
        } of transformsToRegister) {
          TransformRegistry.registerTransform({
            workflowSpecName: this.name,
            receivingSpecName: specName,
            receivingSpecUniqueLabel: uniqueSpecLabel || null,
            ...rest,
          });
        }
      }

      // pass2: add all loose tags in specs
      for (const conn of this.connections) {
        for (const c of [conn.from, conn.to]) {
          const spec = c.spec;
          for (const tag of spec.inputTags) {
            g.ensureInletAndStream({
              specName: spec.name,
              tag,
              uniqueSpecLabel: c.uniqueSpecLabel,
              hasTransform: false,
            });
          }
          for (const tag of spec.outputTags) {
            g.ensureOutletAndStream({
              specName: spec.name,
              tag,
              uniqueSpecLabel: c.uniqueSpecLabel,
            });
          }
        }
      }

      for (const conn of this.connections) {
        for (const c of [conn.from, conn.to]) {
          for (const [specTag, alias] of Object.entries(c.inputAliasMap)) {
            g.assignAlias({
              alias,
              tag: specTag,
              specName: c.spec.name,
              uniqueSpecLabel: c.uniqueSpecLabel,
              type: "in",
              rootSpecName: this.name,
            });
          }
          for (const [specTag, alias] of Object.entries(c.outputAliasMap)) {
            g.assignAlias({
              alias,
              tag: specTag,
              specName: c.spec.name,
              uniqueSpecLabel: c.uniqueSpecLabel,
              type: "out",
              rootSpecName: this.name,
            });
          }
        }
      }

      this.workflowGraphCreated = true;
    }
    return g;
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
    const canonicalConns = convertConnectionsCanonical({
      connections,
    });
    this.connections = canonicalConns;

    this._validateConnections();

    this.orchestrationWorkerDef = new ZZWorkerDef({
      workerPrefix: "workflow",
      jobSpec: this,
      processor: async ({ jobOptions: childrenJobOptions, jobId, output }) => {
        const groupId = jobId;
        const { rec: parentRec } = await vaultClient.db.getParentJobRec({
          projectId: this.zzEnvEnsured.projectId,
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
          inletHasTransformOverridesByTag,
        });

        const allJobNodes = instG
          .nodes()
          .filter((n) => instG.getNodeAttributes(n).nodeType === "job");
        // remove self
        const jobNodesExceptSelf = allJobNodes.filter((n) => {
          const node = instG.getNodeAttributes(n) as JobNode;
          return node.jobId !== `[${groupId}]${this.name}`;
        });

        for (let i = 0; i < jobNodesExceptSelf.length; i++) {
          const jobNodeId = jobNodesExceptSelf[i];
          const jobNode = instG.getNodeAttributes(jobNodeId) as JobNode;

          //calculate input and output overrides
          const inputStreamIdOverridesByTag: Record<string, string> = {};
          const outputStreamIdOverridesByTag: Record<string, string> = {};

          // get the stream id overrides for the input
          const inboundEdges = instG.inboundEdges(jobNodeId);
          const inletEdgeIds = inboundEdges.filter((e) => {
            const node = instG.getNodeAttributes(instG.source(e));
            return node.nodeType === "inlet";
          });
          const inletNodeIds = inletEdgeIds.map((e) => instG.source(e));
          for (const inletNodeId of inletNodeIds) {
            const inletNode = instG.getNodeAttributes(inletNodeId);
            if (inletNode.nodeType !== "inlet") {
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
            return node.nodeType === "outlet";
          });
          const outletNodeIds = outletEdgeIds.map((e) => instG.target(e));

          for (const outletNodeId of outletNodeIds) {
            const outletNode = instG.getNodeAttributes(outletNodeId);
            if (outletNode.nodeType !== "outlet") {
              throw new Error("Expected outlet node");
            }

            const streamFromOutputEdgeId = instG.outboundEdges(outletNodeId)[0];
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

          await childJobSpec.enqueueJob({
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
        }

        await output("__zz_workflow_status").emit({
          __zz_workflow_status: "finished",
        });
      },
    });
  }
  public override get inputTags() {
    // traverse the def graph and get tags from alias nodes
    const g = this.getDefGraph();
    const aliasNodeIds = g.getAllAliasNodeIds();
    return [
      ...aliasNodeIds
        .map((n) => g.getNodeAttributes(n) as AliasNode)
        .filter((n) => n.direction === "in")
        .map((n) => n.alias),
    ];
  }

  public override get outputTags() {
    // traverse the def graph and get tags from alias nodes
    const g = this.getDefGraph();
    const aliasNodeIds = g.getAllAliasNodeIds();
    return [
      ...aliasNodeIds
        .map((n) => g.getNodeAttributes(n) as AliasNode)
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
    const existingAlias = this.getDefGraph().lookupRootSpecAlias({
      specName,
      tag,
      uniqueSpecLabel,
      type,
    });
    if (!existingAlias) {
      const alias = `${specName}[${uniqueSpecLabel || ""}]::${type}/${tag}`;
      this.getDefGraph().assignAlias({
        alias,
        specName,
        tag,
        uniqueSpecLabel,
        type,
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
      (type === "in" ? this.inputDefSet.tags : this.outputDefSet.tags).includes(
        alias.toString()
      )
    ) {
      return {
        specName: this.name,
        tag: alias.toString(),
        type,
        uniqueSpecLabel: undefined,
      };
    } else {
      const r = this.getDefGraph().lookupSpecAndTagByAlias({
        alias: alias.toString(),
        type,
      });
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
        type: "out",
        tag: outSpecInfo.tagInSpec,
      });
      validateSpecHasKey({
        spec: inSpecInfo.spec,
        type: "in",
        tag: inSpecInfo.tagInSpec,
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
        const { rec: parentRec } = await vaultClient.db.getParentJobRec({
          projectId: that.jobGroupDef.zzEnvEnsured.projectId,
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
        return new InstantiatedGraph({
          defGraph: this.jobGroupDef.getDefGraph(),
          contextId: this.contextId,
          rootJobId: this.contextId,
          streamIdOverrides: {},
          inletHasTransformOverridesByTag,
        });
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

function convertSpecAndOutletWithTags(
  specAndOutletOrTagged: TaggedSpecAndOutlet
): {
  spec: JobSpec<any, any, any>;
  uniqueSpecLabel?: string;
  tagInSpec?: string;
  inputAliasMap: Record<string, string>;
  outputAliasMap: Record<string, string>;
} {
  if (Array.isArray(specAndOutletOrTagged)) {
    const [uniqueSpec, tagInSpec] = specAndOutletOrTagged;
    const uniqueSpecLabel = resolveUniqueSpec(uniqueSpec).uniqueSpecLabel;
    const aliasMaps = resolveTagMapping(uniqueSpec);
    return {
      spec: resolveUniqueSpec(uniqueSpec).spec,
      ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
      tagInSpec: tagInSpec.toString(),
      ...aliasMaps,
    };
  } else {
    const converted = resolveUniqueSpec(specAndOutletOrTagged);
    const uniqueSpecLabel = converted.uniqueSpecLabel;
    const aliasMaps = resolveTagMapping(specAndOutletOrTagged);

    return {
      spec: converted.spec,
      ...(uniqueSpecLabel ? { uniqueSpecLabel } : {}),
      ...aliasMaps,
    };
  }
}

type CanonicalConnection<T1 = any, T2 = any> = {
  from: CanonicalSpecAndOutletFrom<T1>;
  to: CanonicalSpecAndOutletTo<T2>;
};

type CanonicalSpecAndOutletBase = {
  spec: JobSpec<any, any, any>;
  uniqueSpecLabel?: string;
  tagInSpec: string;
  inputAliasMap: Record<string, string>;
  outputAliasMap: Record<string, string>;
};
export type CanonicalSpecAndOutletFrom<T = any> = CanonicalSpecAndOutletBase & {
  spec: JobSpec<
    any,
    any,
    any,
    any & {
      K: T;
    }
  >;
  tagInSpecType: "output";
};

export type CanonicalSpecAndOutletTo<T = any> = CanonicalSpecAndOutletBase & {
  spec: JobSpec<
    any,
    any & {
      K: T;
    },
    any,
    any
  >;
  transform: TransformFunction | null;
  tagInSpecType: "input";
};

function convertConnectionsCanonical(workflowParams: WorkflowParams) {
  const convertedConnections = workflowParams.connections.reduce(
    (acc, conn) => {
      if (Array.isArray(conn)) {
        let newAcc: CanonicalConnection[] = [];
        let cursor = 0;

        const handleAndAdvanceCursor = () => {
          const from = convertSpecAndOutletWithTags(
            conn[cursor] as TaggedSpecAndOutlet
          );
          let to: ReturnType<typeof convertSpecAndOutletWithTags>;
          cursor++;
          let transform: TransformFunction | null = null;
          if (typeof conn[cursor] === "function") {
            transform = conn[cursor] as TransformFunction;
            cursor++;
            to = convertSpecAndOutletWithTags(
              conn[cursor] as TaggedSpecAndOutlet
            );
          } else {
            to = convertSpecAndOutletWithTags(
              conn[cursor] as TaggedSpecAndOutlet
            );
          }
          cursor++;

          newAcc.push({
            from: {
              ...from,
              tagInSpecType: "output",
              tagInSpec:
                from.tagInSpec || String(from.spec.getSingleOutputTag()),
            },
            to: {
              ...to,
              tagInSpecType: "input",
              tagInSpec: to.tagInSpec || String(to.spec.getSingleInputTag()),
              transform: transform,
            },
          });
        };

        while (cursor < conn.length) {
          handleAndAdvanceCursor();
        }

        return [...acc, ...newAcc];
      } else {
        const fromPartialCanonical = convertSpecAndOutletWithTags(conn.from);
        const toPartialCanonical = convertSpecAndOutletWithTags(conn.to);
        return [
          ...acc,
          {
            from: {
              ...fromPartialCanonical,
              tagInSpecType: "output",
              tagInSpec:
                fromPartialCanonical.tagInSpec ||
                String(fromPartialCanonical.spec.getSingleOutputTag()),
            },
            to: {
              ...toPartialCanonical,
              tagInSpecType: "input",
              tagInSpec:
                toPartialCanonical.tagInSpec ||
                String(toPartialCanonical.spec.getSingleInputTag()),
            },
          } as CanonicalConnection,
        ];
      }
    },
    [] as CanonicalConnection[]
  );

  return convertedConnections;
}

function validateSpecHasKey<P, I, O, IMap, OMap>({
  spec,
  type,
  tag,
}: {
  spec: IOSpec<I, O, IMap, OMap>;
  type: "in" | "out";
  tag: string;
}) {
  if (type === "in") {
    if (!spec.inputTags.includes(tag as keyof IMap)) {
      throw new Error(`Invalid spec tag: ${spec.name}/${tag} specified.`);
    }
  } else {
    if (!spec.outputTags.includes(tag as keyof OMap)) {
      throw new Error(`Invalid spec tag: ${spec.name}/${tag} specified.`);
    }
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

export function expose<P, I, O, IMap, OMap>(
  specLike: SpecOrName<P, I, O, IMap, OMap>
) {
  return _tagObj(specLike, {
    inputTag: {},
    outputTag: {},
  } as TagMaps<I, O, IMap, OMap, never, never>);
}

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
          const tag = specAndLabel.spec.getSingleInputTag();
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
          const tag = specAndLabel.spec.getSingleOutputTag();
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

function resolveUniqueSpec<P, I, O, IMap, OMap>(
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
