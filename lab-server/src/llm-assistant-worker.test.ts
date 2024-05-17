import {
  titleSummarizerSepc,
  ollamaTitleSummarizerWorkerDef,
} from "./llm-assistant-worker";
import { v4 } from "uuid";

async function main() {
  const jobId = Date.now().toString() + "_" + v4();
  await ollamaTitleSummarizerWorkerDef.startWorker();
  await titleSummarizerSepc.enqueueJob({
    jobId,
    jobOptions: {
      maxTokens: 100,
      temperature: 0.7,
      topP: 1,
      presencePenalty: 0,
      frequencyPenalty: 0,
      bestOf: 1,
      n: 1,
      stream: false,
      stop: ["\n"],
    },
  });

  for (const p of SAMPLE_PARAGRAPHS) {
    titleSummarizerSepc.feedJobInput({
      jobId,
      data: {
        transcript: p,
      },
    });
  }
  titleSummarizerSepc.terminateJobInput({
    jobId,
  });

  // process while output is not null

  const collector = titleSummarizerSepc.createOutputCollector({
    jobId,
  });
  let o = await collector.nextValue();
  while (o) {
    console.log("Title: ", o.data.summarizedTitle);
    o = await collector.nextValue();
  }

  process.exit();
}

main();

const SAMPLE_PARAGRAPHS = [
  `Rosalynn Carter, the closest adviser to Jimmy Carter during his one term as U.S. president and their four decades thereafter as global humanitarians, has died at the age of 96.
      The Carter Center said she died Sunday after living with dementia and suffering many months of declining health.
      The Carters were married for more than 77 years, forging what they both described as a "full partnership." Unlike many previous first ladies, Rosalynn sat in on cabinet meetings, spoke out on controversial issues and represented her husband on foreign trips. Aides to president Carter sometimes referred to her — privately — as "co-president."`,
  `Nike
  on Thursday unveiled plans to cut costs by about $2 billion over the next three years as it lowered its sales outlook.
 
 The stock fell about 10% after hours. Nike shares were up 4.7% so far this year through Thursday’s close, lagging far behind the S&P 500′s gains for the year. Retailer Foot Locker
 , which has leaned heavily on Nike products, fell about 7% after hours.
 
 Nike now expects full-year reported revenue to grow approximately 1%, compared to a prior outlook of up mid-single digits. In the current quarter, which includes the second half of the holiday shopping season, Nike expects reported revenue to be slightly negative as it laps tough prior year comparisons, and sales to be up low single digits in the fourth quarter.`,
  `“Last quarter as I provided guidance, I highlighted a number of risks in our operating environment, including the effects of a stronger U.S. dollar on foreign currency translation, consumer demand over the holiday season and our second half wholesale order books. Looking forward, the impact of these risks is becoming clearer,” finance chief Matthew Friend said on a call with analysts.

 “This new outlook reflects increased macro headwinds, particularly in Greater China and EMEA. Adjusted digital growth plans are based on recent digital traffic softness and higher marketplace promotions, life cycle management of key product franchises and a stronger U.S. dollar that has negatively impacted second-half reported revenue versus 90 days ago.”`,
  `The company still expects gross margins to expand between 1.4 and 1.6 percentage points. Excluding restructuring charges, it expects to deliver on its full-year earnings outlook.

 As part of its plan to cut costs, Nike said it’s looking to simplify its product assortment, increase automation and its use of technology, streamline the overall organization by reducing management layers and leverage its scale “to drive greater efficiency.”`,
  `It plans to reinvest the savings it gets from those initiatives into fueling future growth, accelerating innovation and driving long-term profitability.

 “As we look ahead to a softer second-half revenue outlook, we remain focused on strong gross margin execution and disciplined cost management,″ Friend said in a press release.
 
 The plan will cost the company between $400 million and $450 million in pretax restructuring charges that will largely come to fruition in Nike’s current quarter. Those costs are mostly related to employee severance costs, Nike said.`,
];
