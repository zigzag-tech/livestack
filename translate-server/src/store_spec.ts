import { Pinecone } from '@pinecone-database/pinecone';
import { OpenAIEmbeddings } from "@langchain/openai";

const pc = new Pinecone({
  apiKey: 'df9ed73d-d3c9-48b5-a2c9-520f16645613'
});

const embeddings = new OpenAIEmbeddings({
  openAIApiKey: "sk-None-7DzU3GmFx5cOyBvhSfPgT3BlbkFJ9fVdlcPOZt8DxOpGgdC6",
  model: "text-embedding-3-large",
});

async function initializePinecone() {
  const indexName = 'transcript';
  await createIndexIfNotExists()
  return pc.Index(indexName);
}

async function deleteIndex() {
  await pc.deleteIndex("transcript");
}

async function createIndexIfNotExists() {
  try {
    await pc.describeIndex("transcript");
  } catch (error) {
      await pc.createIndex({
        name: "transcript",
        dimension: 3072,  
        metric: 'cosine',
        spec: {
          serverless: {
            cloud: 'aws',
            region: 'us-east-1'
          }
        }
      });
  }
}

async function createEmbedding(text: string): Promise<number[]> {
  const [embedding] = await embeddings.embedDocuments([text]);
  return embedding;
}


async function storeSentence(sentence: string) {
  const pineconeIndex = await initializePinecone()
  const embedding = await createEmbedding(sentence);

  await pineconeIndex.upsert([
    {
      id: Date.now().toString(),
      values: embedding,
      metadata: { text: sentence }
    }
  ]);
}


async function retrieveSentences(queryText: string, topK: number = 5) {
  const pineconeIndex = await initializePinecone()
  const queryEmbedding = await embeddings.embedQuery(queryText)
  const queryResponse = await pineconeIndex.query({
    vector: queryEmbedding,
    topK: topK,
    includeMetadata: true
  });
  const results = queryResponse.matches.map(match => ({
    score: match.score,
    sentence: match.metadata?.text as string
  }));
  return results;
}

export {storeSentence, retrieveSentences}