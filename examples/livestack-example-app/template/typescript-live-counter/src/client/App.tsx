"use client";
import React from "react";
import { useInput, useJobBinding, useOutput } from "@livestack/client";
import { INCREMENTER, incrementInput, incrementOutput } from "../common/defs";

export function App() {
  const job = useJobBinding({
    specName: INCREMENTER,
  });

  const { last: currCount } = useOutput({
    tag: "default",
    def: incrementOutput,
    job,
  });
  const { feed } = useInput({ tag: "default", def: incrementInput, job });

  return (
    <div className="App">
      <button onClick={() => feed && feed({ action: "increment" })}>
        Click me
      </button>
      <div>{currCount?.data.count || `No count, click the button!`}</div>
    </div>
  );
}
