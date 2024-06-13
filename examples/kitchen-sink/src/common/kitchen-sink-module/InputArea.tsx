"use client";

import React, { useState } from "react";
import { JobInfo, useInput, useJobBinding, useOutput } from "@livestack/client";
import { IndexContent } from "./defs";
import { z } from "zod";

const INDEX_JOB_SPEC_NAME = "text-index-liveflow";

const TextAreaWithSubmit: React.FC = () => {
  const job = useJobBinding({
    specName: INDEX_JOB_SPEC_NAME,
  });
  const { feed } = useInput({
    tag: "default",
    def: IndexContent,
    job,
  });
  return (
    <div>
      <URLInputArea feedInput={feed} />
      <br />
      <TextInputArea feedInput={feed} />
      <br />
      <IndexHistory tag="text-status" job={job} />
      <br />
      <Topics tag="output-topics" job={job} />
    </div>
  );
};

const IndexHistory = ({ tag, job }: { tag: string; job: JobInfo<any> }) => {
  const indexHistory = useOutput({
    tag,
    def: z.string(),
    job,
    query: { type: "lastN", n: 5 },
  });
  return (
    <div>
      <h2>Index History</h2>
      {indexHistory &&
        indexHistory.map((status) => (
          <p key={status.chunkId} className="text-xs">
            {status.data}
          </p>
        ))}
    </div>
  );
};

const Topics = ({ tag, job }: { tag: string; job: JobInfo<any> }) => {
  const { last: topics } = useOutput({
    tag,
    def: z.object({ topics: z.array(z.string()) }),
    job,
  });
  return (
    <div>
      <h2>Topics</h2>
      {topics &&
        topics.data.topics.map((t, idx) => (
          <div key={idx} className="text-xs">
            {t}
          </div>
        ))}
    </div>
  );
};

const TextInputArea = ({
  feedInput,
}: {
  feedInput: ((input: any) => void) | null;
}) => {
  const [textValue, setTextValue] = useState("");

  const handleTextChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    setTextValue(event.target.value);
  };

  const handleTextSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (textValue.trim() === "") return;
    feedInput && feedInput({ content: textValue.trim(), source: "text" });
  };

  return (
    <div>
      <StyledFormArea
        handleSubmit={handleTextSubmit}
        textValue={textValue}
        handleChange={handleTextChange}
        buttonText="Index Text Content"
        placeholderText="Copy and paste your text content here"
      />
    </div>
  );
};

const URLInputArea = ({
  feedInput,
}: {
  feedInput: ((input: any) => void) | null;
}) => {
  const [urlValue, setURLValue] = useState("");

  // Function to handle changes in the textarea
  const handleURLChange = (event: React.ChangeEvent<HTMLTextAreaElement>) => {
    setURLValue(event.target.value);
  };

  const handleURLSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (urlValue.trim() === "") return;
    const urls = urlValue
      .trim()
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter((line) => line !== "");
    feedInput && feedInput({ urls, source: "urls" });
  };

  return (
    <div>
      <StyledFormArea
        handleSubmit={handleURLSubmit}
        textValue={urlValue}
        handleChange={handleURLChange}
        buttonText="Index URL Content"
        placeholderText="Enter URLs here for content you wish to extract. One URL per line."
      />
    </div>
  );
};

const StyledFormArea = ({
  handleSubmit,
  handleChange,
  textValue,
  buttonText,
  placeholderText,
}: {
  handleSubmit: (event: React.FormEvent) => void;
  handleChange: (event: React.ChangeEvent<HTMLTextAreaElement>) => void;
  textValue: string;
  buttonText: string;
  placeholderText: string;
}) => (
  <form onSubmit={handleSubmit} className="flex flex-col">
    <textarea
      value={textValue}
      onChange={handleChange}
      rows={4}
      className="mb-2 p-2 w-full border rounded-md shadow-sm"
      placeholder={placeholderText}
    />
    <button type="submit" className="mr-auto p-2 rounded bg-zzyellow">
      {buttonText}
    </button>
  </form>
);

export default TextAreaWithSubmit;
