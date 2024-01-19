export const REQUEST_AND_BIND_CMD = "request_and_bind";
export type RequestAndBindType = {
  specName: string;
  uniqueSpecLabel?: string;
};

export const JOB_INFO = "job_info";

export type JobInfoType = {
  jobId: string;
  inputKeys: string[];
  outputKeys: string[];
};
