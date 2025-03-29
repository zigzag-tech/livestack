import { EmbeddingCache } from './EmbeddingCache';

/**
 * Function to generate embeddings using Hugging Face Text Embeddings Inference service
 * @param text Text to generate embeddings for
 * @returns Array of embedding values
 */
async function genEmbeddingHGInference(text: string): Promise<number[]> {
    const endpoint = process.env.EMBEDDING_CLIENT;
    
    if (!endpoint) {
        throw new Error('EMBEDDING_CLIENT environment variable is not set. Please set it to your Hugging Face Text Embeddings Inference endpoint URL.');
    }

    try {
        const response = await fetch(`${endpoint}/embed`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
            },
            body: JSON.stringify({
                inputs: text,
            }),
        });

        if (!response.ok) {
            const errorText = await response.text();
            throw new Error(`Failed to generate embedding: ${response.status} ${response.statusText} - ${errorText}`);
        }

        const result = await response.json();
        return result.embeddings;
    } catch (error) {
        console.error('Error generating embedding from Hugging Face inference endpoint:', error);
        throw error;
    }
}

// Lazy initialization of the cache
let cache: EmbeddingCache | null = null;

/**
 * Generate embedding for a text description using Hugging Face Text Embeddings Inference
 * This function lazily initializes the cache and ensures the environment variable is set
 * @param text Text to generate embeddings for
 * @returns Array of embedding values
 */
export async function generateEmbeddingHGInference(text: string): Promise<number[]> {
    // Lazily initialize the cache only when the function is first called
    if (!cache) {
        // Check if the environment variable is set before initializing
        if (!process.env.EMBEDDING_CLIENT) {
            throw new Error('EMBEDDING_CLIENT environment variable is not set. Please set it to your Hugging Face Text Embeddings Inference endpoint URL.');
        }
        cache = new EmbeddingCache({ embeddingFn: genEmbeddingHGInference });
    }
    
    return cache.getEmbedding(text);
}

/**
 * Calculate cosine similarity between two embeddings
 * @param embedding1 First embedding array
 * @param embedding2 Second embedding array
 * @returns Cosine similarity value between 0 and 1
 */
export async function getCosineSimilarityHGInference(embedding1: number[], embedding2: number[]): Promise<number> {
    // Initialize cache if needed
    if (!cache) {
        if (!process.env.EMBEDDING_CLIENT) {
            throw new Error('EMBEDDING_CLIENT environment variable is not set. Please set it to your Hugging Face Text Embeddings Inference endpoint URL.');
        }
        cache = new EmbeddingCache({ embeddingFn: genEmbeddingHGInference });
    }
    
    return cache.getCosineSimilarity(embedding1, embedding2);
}
