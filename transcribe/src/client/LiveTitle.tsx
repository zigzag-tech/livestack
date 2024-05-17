"use client";
import React from "react";
import { useOutput, JobInfo } from "@livestack/client";
import { z } from "zod";

export const LiveTitle = ({ tag, job }: { tag: string; job: JobInfo<any> }) => {
  const { last: summarizedTitle } = useOutput({
    tag,
    def: z.object({
      summarizedTitle: z.string(),
    }),
    job,
  });

  return (
    <>
      <h2>Title</h2>
      <p>{summarizedTitle?.data.summarizedTitle}</p>
    </>
  );
};
