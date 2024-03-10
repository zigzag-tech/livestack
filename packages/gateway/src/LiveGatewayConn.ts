import {
  CMD_UNBIND,
  UnbindParams,
  REQUEST_AND_BIND_CMD,
  RequestAndBindType,
  CMD_FEED,
  FeedParams,
  MSG_JOB_INFO,
  JobInfoType,
} from "@livestack/shared";
import { Subscription } from "rxjs";
import { ZZEnv, JobSpec } from "@livestack/core";
import { Socket } from "socket.io";
import { JobInput, JobOutput } from "@livestack/core";

export class LiveGatewayConn {
  socket: Socket;
  private readonly allowedSpecsForBinding: {
    specName: string;
    uniqueSpecLabel?: string;
  }[];

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

    const cleanup = addMethodResponder({
      socket: this.socket,
      req: { method: REQUEST_AND_BIND_CMD },
      res: { method: MSG_JOB_INFO },
      fn: async ({ specName, uniqueSpecLabel }: RequestAndBindType) => {
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
        const jobOutput = await spec.enqueueJob({});

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
      const input = this.jobFnsById[jobId].input;

      for (const key of input.tags) {
        try {
          await input.byTag(key).terminate();
        } catch (err) {
          console.error(err);
        }
      }

      const subs = this.subsByJobId[jobId];

      for (const sub of subs) {
        sub.unsubscribe();
      }

      cleanup();
      this.socket.off(CMD_FEED, feedListener);
      this.socket.off(CMD_UNBIND, unbindListener);
    };
    this.socket.on(CMD_UNBIND, unbindListener);
  }
  private subsByJobId: Record<string, Subscription[]> = {};

  private jobFnsById: Record<
    string,
    {
      input: JobInput<any>;
      output: JobOutput<any>;
    }
  > = {};

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
    const { input, output } = this.jobFnsById[jobId];

    const subs: Subscription[] = [];
    console.info("Tags to transmit for job ", jobId, ":", output.tags);
    for (const tag of output.tags) {
      const sub = output.byTag(tag).valueObservable.subscribe((data) => {
        this.socket.emit(`stream:${jobId}/${String(tag)}`, data);
      });
      subs.push(sub);
    }
    this.subsByJobId[jobId] = subs;

    this.onDisconnect(() => {
      for (const key of input.tags) {
        try {
          input.byTag(key).terminate();
        } catch (err) {
          console.error(err);
        }
      }

      for (const sub of subs) {
        sub.unsubscribe();
      }
      delete this.subsByJobId[jobId];
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
  res: {
    method: string;
  };
  socket: Socket;
  fn: (arg: P) => Promise<R>;
}) {
  const listener = async (arg: { data: P; requestIdentifier: string }) => {
    const result = await fn(arg.data);
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
