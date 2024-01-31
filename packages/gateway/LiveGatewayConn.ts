import {
  CMD_UNBIND,
  UnbindParams,
  REQUEST_AND_BIND_CMD,
  RequestAndBindType,
  CMD_FEED,
  FeedParams,
  MSG_JOB_INFO,
  JobInfoType,
} from "@livestack/shared/gateway-binding-types";
import { Subscription } from "rxjs";
import { ZZEnv, ZZJobSpec } from "@livestack/core";
import { Socket } from "socket.io";

export class LiveGatewayConn {
  socket: Socket;
  private readonly allowedSpecsForBinding: {
    specName: string;
    uniqueSpecLabel?: string;
  }[];
  zzEnv: ZZEnv;

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
    zzEnv = zzEnv || ZZEnv.global();
    if (!zzEnv) {
      throw new Error("ZZEnv not found.");
    }
    this.zzEnv = zzEnv;

    this.socket.on(
      REQUEST_AND_BIND_CMD,
      async ({ specName, uniqueSpecLabel }: RequestAndBindType) => {
        if (
          !this.allowedSpecsForBinding.some(
            (s) =>
              s.specName === specName && s.uniqueSpecLabel === uniqueSpecLabel
          )
        ) {
          throw new Error(
            `Spec name ${specName} not allowed for binding to socket.`
          );
        }
        const spec = ZZJobSpec.lookupByName(specName);
        await this.bindToNewJob(spec);
      }
    );
  }

  public onDisconnect = async (cb: () => void) => {
    this.socket.on("disconnect", cb);
  };

  public bindToNewJob = async <P, I, O, IMap, OMap>(
    jobSpec: ZZJobSpec<P, I, O, IMap, OMap>,
    jobOptions?: P
  ) => {
    const { input, output, jobId } = await jobSpec.enqueueJob({ jobOptions });
    const data: JobInfoType = {
      jobId,
      availableInputs: input.tags.map((k) => String(k)),
      availableOutputs: output.tags.map((k) => String(k)),
    };
    this.socket.emit(MSG_JOB_INFO, data);

    this.socket.on(
      CMD_FEED,
      async <K extends keyof IMap>({ data, tag }: FeedParams<K, IMap[K]>) => {
        try {
          await input.byTag(tag).feed(data);
        } catch (err) {
          console.error(err);
        }
      }
    );

    let subs: Subscription[] = [];

    for (const tag of output.tags) {
      const sub = output.byTag(tag).valueObservable.subscribe((data) => {
        this.socket.emit(`stream:${jobId}/${String(tag)}`, data);
      });
      subs.push(sub);
    }

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
    });

    this.socket.on(CMD_UNBIND, ({ jobId }: UnbindParams) => {
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
    });
  };
}
