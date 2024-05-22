import { Message } from "ollama";

export const summaryFewShotPromptMessages = (originalContent: string) =>
  [
    {
      role: "system",
      content: `
You are a helpful assistant. Your job is to generate concise summary based on information provided under ORIGINAL CONTENT.
Instructions:
- Summarize the content in a clear and concise manner.
- Do NOT invent new information.
- Keep the summary under 500 characters.
`,
    },
    {
      role: "user",
      content: `
ORIGINAL CONTENT:
\`\`\`
Host: So, let's jump right in. AI has been making significant strides in recent years. What do you see as the most exciting developments in AI right now?
Dr. Chen: Absolutely, Sarah. One of the most exciting developments is the intersection of AI with other emerging technologies such as robotics and quantum computing. This convergence opens up new possibilities for solving complex problems in areas like healthcare, transportation, and environmental conservation.
Host: That sounds fascinating. Can you give us an example of how AI is being applied in one of these areas?
Dr. Chen: Certainly. Take healthcare, for instance. AI-powered diagnostic systems are being developed to analyze medical images and detect diseases such as cancer with a level of accuracy that was previously unimaginable. This not only speeds up the diagnosis process but also improves patient outcomes by enabling early detection and intervention.
Host: Wow, that's truly remarkable. With these advancements, do you foresee any potential challenges or ethical concerns that we need to address?
Dr. Chen: Absolutely, Sarah. As AI becomes more integrated into our daily lives, it's crucial to address issues related to privacy, bias, and job displacement. We need to ensure that AI systems are transparent, accountable, and designed with ethical considerations in mind. Additionally, there's a growing need for regulations to govern the responsible use of AI across various industries.
\`\`\`
        `,
    },
    {
      role: "assistant",
      content: `Dr. Chen highlights AI's exciting advancements, especially in healthcare, where it's revolutionizing diagnostics. He emphasizes the need to address ethical concerns like privacy and bias as AI integrates further into daily life, stressing transparency and regulation.`,
    },
    {
      role: "user",
      content: `
\`\`\`
In a historic turn of events, the nation witnessed a landmark election that is poised to reshape the political landscape for years to come. With record voter turnout and widespread anticipation, the election unfolded as a pivotal moment in the country's democratic history.

After months of campaigning, debates, and intense speculation, voters cast their ballots in what many described as a referendum on the nation's future. The election saw a diverse field of candidates vying for key positions at local, state, and national levels, reflecting the breadth and depth of political engagement across the country.

At the heart of the election were pressing issues ranging from economic recovery and healthcare to climate change and social justice. Candidates put forth ambitious proposals and competing visions for addressing these challenges, igniting spirited debates and energizing voters from all walks of life.

As the election results poured in, it became clear that the electorate had delivered a decisive mandate for change. In a stunning upset, several incumbents were unseated, signaling a desire for fresh leadership and a break from the status quo. New faces emerged victorious, promising to usher in a new era of governance characterized by transparency, accountability, and progress.
\`\`\`
`,
    },
    {
      role: "assistant",
      content: `The recent landmark election, marked by record voter turnout and diverse candidate fields, signaled a pivotal moment in the nation's democratic history. With pressing issues at the forefront, voters delivered a decisive mandate for change, unseating incumbents and ushering in new leadership committed to transparency, accountability, and progress.`,
    },
    {
      role: "user",
      content: `
ORIGINAL CONTENT:
\`\`\`
${originalContent}
\`\`\`
`,
    },
  ] as Message[];
