"use client";
import React from "react";
import { useOutput, JobInfo } from "@livestack/client";
import { speechChunkToTextOutput } from "./defs";
import { StreamQuery } from "@livestack/shared";

export const Transcripts = ({
  tag,
  job,
  query,
}: {
  tag: string;
  job: JobInfo<any>;
  query: StreamQuery;
}) => {
  const transcription = useOutput({
    tag,
    def: speechChunkToTextOutput,
    job,
    query,
  });

  return (
    <>
      <h2>Transcript</h2>
      <article
        style={{
          maxWidth: "100%",
        }}
      >
        {transcription.map((transcript, i) => (
          <span key={i} className="text-sm">
            {transcript.data.transcript}
          </span>
        ))}
      </article>
    </>
  );
};
