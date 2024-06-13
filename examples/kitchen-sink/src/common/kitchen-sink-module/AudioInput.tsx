import React from "react";
import { useInput, useJobBinding, useOutput } from "@livestack/client";
import { z } from "zod";
import { usePCMRecorder, encodeToB64 } from "@livestack/transcribe/client";
import { FaStop, FaMicrophone } from "react-icons/fa";
import prettyBytes from "pretty-bytes";

export const AudioInput = ({ specName }: { specName: string }) => {
  const job = useJobBinding({ specName });

  const { last: transcriptStatus } = useOutput({
    tag: "output-topics",
    def: z.object({ topics: z.array(z.string()) }),
    job,
  });

  const { feed } = useInput({
    tag: "input-default",
    def: z.object({
      rawPCM64Str: z.string(),
    }),
    job,
  });

  const { last: summarizedTitle } = useOutput({
    tag: "summarized-title",
    def: z.object({
      summarizedTitle: z.string(),
    }),
    job,
  });

  const [volume, setVolume] = React.useState<number>(0);

  const { startRecording, stopRecording, isRecording, cumulativeDataSent } =
    usePCMRecorder({
      onDataAvailable: async (data) => {
        console.log(Math.max(...data), Math.min(...data));
        const encoded = encodeToB64(data);
        if (feed) {
          await feed({ rawPCM64Str: encoded });
          console.log(encoded.slice(0, 10), "length: ", encoded.length);
        }
      },
      onVolumeChange: (volume) => {
        setVolume(volume);
      },
      maxInterval: 7 * 1000,
      silenceDuration: 300,
      silentThreshold: -50,
      minDecibels: -100,
    });

  const handleRecording = isRecording ? stopRecording : startRecording;

  return (
    <div>
      {job.jobId && (
        <button className="btn mr-auto p-2 rounded" onClick={handleRecording}>
          <span style={{ display: "inline-block" }}>
            {isRecording ? "Stop Recording" : "Start Recording"}
          </span>
          &nbsp;
          <span style={{ display: "inline-block" }}>
            {isRecording ? <FaStop /> : <FaMicrophone />}
          </span>
        </button>
      )}
      <div>
        Volume: <span>{volume.toFixed(1)}</span>
        <br />
        <progress
          value={volume}
          max={100}
          style={{ width: "100px" }}
        ></progress>
        <br />
        {typeof cumulativeDataSent !== "undefined" && (
          <>Total data sent: {prettyBytes(cumulativeDataSent)}</>
        )}
      </div>
      <>
        <h2>Title</h2>
        <p>{summarizedTitle?.data.summarizedTitle}</p>
      </>
      {transcriptStatus && (
        <div>
          <h2 className="my-3 text-lg">Topics</h2>
          {transcriptStatus.data.topics.map((t, idx) => (
            <div key={idx}>{t}</div>
          ))}
        </div>
      )}
    </div>
  );
};

export default AudioInput;
