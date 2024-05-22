"use client";
import React from "react";
import { useOutput, JobInfo } from "@livestack/client";
import { topicsSchema } from "@livestack/summarizer/client";

export const TopicsList = ({ tag, job }: { tag: string; job: JobInfo<any> }) => {
  const { last: topicsList } = useOutput({
    tag,
    def: topicsSchema,
    job,
  });

  return (
    <>
      <h2 className="my-3 text-lg">Topics Covered</h2>
      <ul>
        {topicsList?.data.topics.map((topic, i) => (
          <li className="list-disc" key={i}>
            {topic}
          </li>
        ))}
      </ul>
    </>
  );
};
