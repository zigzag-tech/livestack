export const REQUEST_AND_BIND_CMD = "request_and_bind";
export type RequestAndBindType = {
  specName: string;
  uniqueSpecLabel?: string;
};

export const JOB_INFO = "job_info";
export type JobInfoType = {
  jobId: string;
  availableInputs: {
    specName: string;
    uniqueSpecLabel?: string;
    key: string;
    inDegree: number;
  }[];
  availableOutputs: {
    specName: string;
    uniqueSpecLabel?: string;
    key: string;
    outDegree: number;
  }[];
};

export function resolveToUniqueStream(identifier: StreamIdentifier, 
  availableInputs: JobInfoType["availableInputs"]) {
  const { specName, uniqueSpecLabel, key } = identifier;
  const matchingInputs = availableInputs.filter((input) => {
    return (
      (!specName || input.specName === specName) &&
      (!uniqueSpecLabel ||
        input.uniqueSpecLabel === uniqueSpecLabel) &&
      (!key || input.key === key)
    );
  });

  if(matchingInputs.length === 0) {
    throw new Error(`No matching inputs found for ${JSON.stringify(identifier)}`);
  } else if (matchingInputs.length > 1) {
    throw new Error(`More than one matching inputs found for ${JSON.stringify(identifier)}: ${JSON.stringify(matchingInputs)}`);
  }
  return {
    specName: matchingInputs[0].specName,
   ...(matchingInputs[0] ? { uniqueSpecLabel: matchingInputs[0].uniqueSpecLabel } : {}),
    key: matchingInputs[0].key,
  };
}

export type StreamIdentifier = {
  specName?: string;
  uniqueSpecLabel?: string;
  key?: string;
}



export const UNBIND_CMD = "unbind";
