import {
  CMD_UNBIND,
  UnbindParams,
  REQUEST_AND_BIND_CMD,
  RequestAndBindType,
  CMD_FEED,
  FeedParams,
  MSG_JOB_INFO,
  JobInfoType,
  CMD_SUB_TO_STREAM,
  CMD_UNSUB_TO_STREAM,
} from "@livestack/shared";
import { Subscription } from "rxjs";
import { ZZEnv, JobSpec, JobManager } from "@livestack/core";
import { Socket } from "socket.io";
import { JobInput, JobOutput } from "@livestack/core";

export class LiveGatewayConn {
  socket: Socket;
  private readonly allowedSpecsForBinding: {
    specName: string;
    uniqueSpecLabel?: string;
  }[];

  static bindSessionIdsByJobId: Record<string, Set<string>> = {};

  constructor({
    zzEnv,
    socket,
    allowedSpecsForBinding = [],
  }: {
    zzEnv?: ZZEnv | null;
    socket: Socket;
    allowedSpecsForBinding?: {
      specName: string;
      uniqueSpecLabel?: string;
    }[];
  }) {
    this.socket = socket;
    this.allowedSpecsForBinding = allowedSpecsForBinding;

    const stopRespondingToBindCmd = addMethodResponder({
      socket: this.socket,
      req: { method: REQUEST_AND_BIND_CMD },
      res: { method: MSG_JOB_INFO },
      fn: async ({
        specName,
        uniqueSpecLabel,
        jobId: requestedJobId,
        jobOptions,
      }: RequestAndBindType) => {
        if (
          !this.allowedSpecsForBinding.some(
            (s) =>
              s.specName === specName &&
              (!s.uniqueSpecLabel || s.uniqueSpecLabel === uniqueSpecLabel)
          )
        ) {
          throw new Error(
            `Spec name ${specName} not allowed for binding to socket.`
          );
        }
        const spec = JobSpec.lookupByName(specName);
        let jobOutput: JobManager<any, any, any, any, any>;
        if (requestedJobId) {
          jobOutput = await spec.getJobManager(requestedJobId);
        } else {
          jobOutput = await spec.enqueueJob({
            jobOptions,
          });
        }

        LiveGatewayConn.bindSessionIdsByJobId[jobOutput.jobId] =
          LiveGatewayConn.bindSessionIdsByJobId[jobOutput.jobId] || new Set();

        LiveGatewayConn.bindSessionIdsByJobId[jobOutput.jobId].add(
          this.socket.id
        );

        this.jobFnsById[jobOutput.jobId] = jobOutput;
        const data: JobInfoType = {
          jobId: jobOutput.jobId,
          availableInputs: jobOutput.input.tags.map((k) => String(k)),
          availableOutputs: jobOutput.output.tags.map((k) => String(k)),
        };
        this.bindToNewJob({
          jobSpec: spec,
          jobId: jobOutput.jobId,
        });
        return data;
      },
    });

    const stopRespondingToSubToStreamCmd = addMethodResponder({
      socket: this.socket,
      req: { method: CMD_SUB_TO_STREAM },
      res: { method: "stream" },
      fn: async ({
        jobId,
        tag,
        type,
      }: {
        jobId: string;
        tag: string;
        type: "input" | "output";
      }) => {
        if (type === "output") {
          if (!this.subByJobIdAndTag[`${jobId}::out/${tag}`]) {
            this.subByJobIdAndTag[`${jobId}::out/${tag}`] = (async () => {
              const { output } = this.jobFnsById[jobId];
              const mostRecentVal = await output.byTag(tag).mostRecentValue();
              if (mostRecentVal) {
                this.socket.emit(
                  `stream:${jobId}/${String(tag)}`,
                  mostRecentVal
                );
              }
              const sub = output
                .byTag(tag)
                .valueObservable.subscribe((data) => {
                  this.socket.emit(`stream:${jobId}/${String(tag)}`, data);
                });
              return {
                sub,
                count: 0,
              };
            })();
          }
          (await this.subByJobIdAndTag[`${jobId}::out/${tag}`]).count++;
        } else {
          throw new Error("Input streams not yet supported.");
        }
      },
    });

    const stopRespondingToUnsubToStreamCmd = addMethodResponder({
      socket: this.socket,
      req: { method: CMD_UNSUB_TO_STREAM },
      res: { method: "stream" },
      fn: async ({
        jobId,
        tag,
        type,
      }: {
        jobId: string;
        tag: string;
        type: "input" | "output";
      }) => {
        if (type === "output") {
          (await this.subByJobIdAndTag[`${jobId}::out/${tag}`]!).count--;

          if (
            (await this.subByJobIdAndTag[`${jobId}::out/${tag}`]!).count === 0
          ) {
            (
              await this.subByJobIdAndTag[`${jobId}::out/${tag}`]!
            ).sub.unsubscribe();
            delete this.subByJobIdAndTag[`${jobId}::out/${tag}`];
          }
        } else {
          throw new Error("Input streams not yet supported.");
        }
      },
    });

    const that = this;

    const feedListener = async <K extends keyof any>({
      data,
      tag,
      jobId,
    }: FeedParams<K, any>) => {
      // console.info("Feeding data to job ", jobId, " with tag ", tag);
      const { input } = that.jobFnsById[jobId];
      try {
        await input.byTag(tag).feed(data);
      } catch (err) {
        console.error(err);
      }
    };
    this.socket.on(CMD_FEED, feedListener);
    const unbindListener = async ({ jobId }: UnbindParams) => {
      const input = this.jobFnsById[jobId]?.input;
      if (!input) {
        console.error("No input found for job ", jobId);
        return;
      }

      await this.disassociateAndMaybeTerminate(jobId);

      stopRespondingToBindCmd();
      stopRespondingToSubToStreamCmd();
      stopRespondingToUnsubToStreamCmd();

      this.socket.off(CMD_FEED, feedListener);
      this.socket.off(CMD_UNBIND, unbindListener);
    };
    this.socket.on(CMD_UNBIND, unbindListener);
  }

  private subByJobIdAndTag: Record<
    `${string}::${"in" | "out"}/${string}`,
    Promise<{
      sub: Subscription;
      count: number;
    }>
  > = {};

  private jobFnsById: Record<
    string,
    {
      input: JobInput<any>;
      output: JobOutput<any>;
    }
  > = {};

  private disassociateAndMaybeTerminate = async (jobId: string) => {
    const input = this.jobFnsById[jobId].input;
    LiveGatewayConn.bindSessionIdsByJobId[jobId]!.delete(this.socket.id);
    if (LiveGatewayConn.bindSessionIdsByJobId[jobId]!.size === 0) {
      for (const key of input.tags) {
        try {
          await input.byTag(key).terminate();
        } catch (err) {
          console.error(err);
        }
      }
    }
    const relevantKeys = Object.keys(this.subByJobIdAndTag).filter((k) =>
      k.startsWith(jobId)
    ) as Array<keyof typeof this.subByJobIdAndTag>;
    for (const k of relevantKeys) {
      (await this.subByJobIdAndTag[k]).sub.unsubscribe();
      delete this.subByJobIdAndTag[k];
    }
  };

  public onDisconnect = async (cb: () => void) => {
    this.socket.on("disconnect", cb);
  };

  public bindToNewJob = async <P, I, O, IMap, OMap>({
    jobSpec,

    jobId,
  }: {
    jobId: string;

    jobSpec: JobSpec<P, I, O, IMap, OMap>;
  }) => {
    // const { output } = this.jobFnsById[jobId];

    // const subs: Subscription[] = [];
    // console.info("Tags to transmit for job ", jobId, ":", output.tags);
    // for (const tag of output.tags) {
    //   const mostRecentVal = await output.byTag(tag).mostRecentValue();
    //   if (mostRecentVal) {
    //     this.socket.emit(`stream:${jobId}/${String(tag)}`, mostRecentVal);
    //   }
    //   const sub = output.byTag(tag).valueObservable.subscribe((data) => {
    //     this.socket.emit(`stream:${jobId}/${String(tag)}`, data);
    //   });
    //   subs.push(sub);
    // }
    // this.subsByJobId[jobId] = subs;

    this.onDisconnect(async () => {
      await this.disassociateAndMaybeTerminate(jobId);
      delete this.jobFnsById[jobId];
    });
  };
}

function addMethodResponder<P, R>({
  socket,
  req,
  res,
  fn,
}: {
  req: {
    method: string;
  };
  res?: {
    method: string;
  };
  socket: Socket;
  fn: (arg: P) => Promise<R>;
}) {
  const listener = async (arg: { data: P; requestIdentifier: string }) => {
    const result = await fn(arg.data);
    if (!res) return;
    socket.emit(res.method, {
      data: result,
      requestIdentifier: arg.requestIdentifier,
    });
  };
  socket.on(req.method, listener);

  const cleanup = () => {
    socket.off(req.method, listener);
  };
  return cleanup;
}
