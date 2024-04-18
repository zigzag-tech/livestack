export const REQUEST_AND_BIND_CMD = "request_and_bind";
export type RequestAndBindType = {
  specName: string;
  uniqueSpecLabel?: string;
  jobId?: string;
  jobOptions?: any;
};

export const MSG_JOB_INFO = "job_info";
export type JobInfoType = {
  jobId: string;
  availableInputs: string[];
  availableOutputs: string[];
};

export type StreamIdentifier = {
  specName?: string;
  uniqueSpecLabel?: string;
  key?: string;
};

export const CMD_UNBIND = "unbind";
export type UnbindParams = {
  jobId: string;
};

export const CMD_FEED = "feed";
export const CMD_SUB_TO_STREAM = "sub_to_stream";
export const CMD_UNSUB_TO_STREAM = "unsub_to_stream";

export type FeedParams<K, T> = {
  data: T;
  tag: K;
  jobId: string;
};
