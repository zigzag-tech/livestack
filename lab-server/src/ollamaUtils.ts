import { wrapWithTransientStdout } from "@livestack/shared";
import { baseInstruction } from "./leptonUtils";

const CONVO_MODEL = "llama3:instruct";

export async function generateResponseOllama(prompt: string) {
  const { Ollama } = await import("ollama");
  const OLLAMA_HOST = process.env.OLLAMA_HOST || "http://localhost:11434";
  const ollama = new Ollama({ host: OLLAMA_HOST });

  const messages = [
    {
      role: "system",
      content: `${baseInstruction}`,
    },
    {
      role: "user",
      content: `ORIGINAL TEXT: 
\`\`\`
  Within health care, ambulatory services and hospitals combined to add 55,000 jobs, according to the Bureau of Labor Statistics. Local government was another strong subgroup for hiring, growing by 49,000 jobs.
  
  Notably, the leisure and hospitality sector is now back to its pre-pandemic employment level, according to the BLS. Employment in this area, which includes bars and restaurants, fell dramatically in 2020 when many such establishments were closed for health concerns.
  \`\`\`
JSON TITLE:
`,
    },
    {
      role: "assistant",
      content: `
{ "title": "Health & Hospitality Jobs Surge to Pre-Pandemic Levels" }
      `,
    },
    {
      role: "user",
      content: `ORIGINAL TEXT:
\`\`\`
Key lime pie is a beloved dessert made with tangy Key lime juice, egg yolks and sweetened condensed milk, all baked in a graham cracker pie crust. It’s refreshing citrus tang makes it an ideal dessert for warmer weather. Even if you can’t bear to fire up the oven, there are tons of no-bake recipes that include all the same flavors and textures of the original.

Serving key lime pie can be a delightful treat at barbecues, pool parties or any gathering where a light and zesty dessert is desired—although we’d gladly have a slice at any point during the year.
\`\`\`

JSON TITLE:
`,
    },
    {
      role: "assistant",
      content: `
{ "title": "Key Lime Pie: A Refreshing Citrus Treat for Any Occasion" }
`,
    },
    {
      role: "user",
      content: `ORIGINAL TEXT:
\`\`\`
2月底樓市全面撤辣後，新盤成交氣氛轉好，發展商加快去貨，陸續以低價推盤應市，不少早前摸頂入市買家紛紛選擇撻訂離場。據本報統計，上月錄得最少108宗取消交易個案，屬2013年《一手住宅物業銷售條例》實施後的單月新高，同時較上月的13宗撻訂個案大漲逾7倍。當中以嘉里建設(683)、信和置業(083)及港鐵(066)合作發展的黃竹坑站港島南岸第2期「揚海」佔25宗最多，大部分屬「撻大訂」個案，全數於2021年9月入市，原先成交價介乎約1,145萬至約5,888萬元。
\`\`\`

JSON TITLE:
`,
    },
    {
      role: "assistant",
      content: `
{ "title": "Post-Cooling Measures, Property Cancellations Surge with 'Yanghai' Leading" }
      `,
    },
    {
      role: "user",
      content: `ORIGINAL TEXT:
\`\`\`
${prompt}
\`\`\`

JSON TITLE:
`,
    },
  ];
  try {
    const response = await ollama.chat({
      stream: true,
      model: CONVO_MODEL,
      messages,
    });
    const message = wrapWithTransientStdout(response);
    // const message = response.message.content;
    return message;
  } catch (e) {
    console.log(e);
    return "Sorry, I am not able to respond right now. Please try again later.";
  }
}
