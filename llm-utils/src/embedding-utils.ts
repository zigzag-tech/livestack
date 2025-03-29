
import * as fs from 'fs';
import * as path from 'path';
import { getAvailableOllama } from './multi-ollama'; // Import the new function
import * as crypto from 'crypto';


/**
 * Calculates the similarity ratio between two strings (0 to 1)
 * 
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns A value between 0 (completely different) and 1 (identical)
 */
export function stringSimilarity(str1: string, str2: string): number {
    const maxLength = Math.max(str1.length, str2.length);
    if (maxLength === 0) return 1.0; // Both strings are empty
    const distance = levenshteinDistance(str1, str2);
    return 1 - distance / maxLength;
  } 
  
  const EMBEDDING_MODEL = 'nomic-embed-text'; // Ollama embedding model
  
  /**
   * Generate embedding for a text description using Ollama
   * @param text Text to generate embeddings for
   * @returns Array of embedding values
   */
   async function genEmbeddingOllama(text: string): Promise<number[]> {
    // Get the auto-releasing client
    const client = await getAvailableOllama();
    try {
      // Call embeddings using the client
      const response = await client.embeddings({
        model: EMBEDDING_MODEL,
        prompt: text,
      });
      
      return response.embedding;
    } catch (error) {
      console.error('Error generating embedding:', error);
      throw error;
    }
    // No finally block needed for release, it's handled internally by the client's methods
  }
  
  
  
  export class EmbeddingCache {
    private cacheDir: string;
    private cache: Set<string>;
  
    constructor(cacheDir: string = '.cache/embeddings') {
      this.cacheDir = cacheDir;
      this.cache = new Set();
      this.initializeCache();
    }
  
    private initializeCache() {
      // Create cache directory if it doesn't exist
      if (!fs.existsSync(this.cacheDir)) {
        fs.mkdirSync(this.cacheDir, { recursive: true });
      }
      // load existing cache files into memory  
      const files = fs.readdirSync(this.cacheDir);
  
      files.forEach(file => {
        const filePath = path.join(this.cacheDir, file);
        if (fs.statSync(filePath).isFile()) {
          const hash = file.replace('.bin', '');
          this.cache.add(hash);
        }
      });
     
    }
  
    private getHash(text: string): string {
      return crypto.createHash('sha256').update(text).digest('hex');
    }
  
    private getCacheFilePath(hash: string): string {
      return path.join(this.cacheDir, `${hash}.bin`);
    }
  
    private arrayToBuffer(arr: number[]): Buffer {
      const buffer = Buffer.alloc(arr.length * Float32Array.BYTES_PER_ELEMENT);
      const view = new Float32Array(buffer.buffer);
      arr.forEach((val, i) => view[i] = val);
      return buffer;
    }
  
    private bufferToArray(buffer: Buffer): number[] {
      const view = new Float32Array(buffer.buffer, buffer.byteOffset, buffer.length / Float32Array.BYTES_PER_ELEMENT);
      return Array.from(view);
    }
  
    async getEmbedding(text: string): Promise<number[]> {
      const hash = this.getHash(text);
  
      // Check in-memory cache first
      if (this.cache.has(hash)) {
        // read from file cache
        const cacheFilePath = this.getCacheFilePath(hash);
        if(!fs.existsSync(cacheFilePath)) {
         throw new Error(`Cache file not found for hash: ${hash}`);
        }
  
        const buffer = fs.readFileSync(cacheFilePath);
        const embedding = this.bufferToArray(buffer);
        return embedding;
      }
  
      // Check file cache
      const cacheFilePath = this.getCacheFilePath(hash);
      if (fs.existsSync(cacheFilePath)) {
        const buffer = fs.readFileSync(cacheFilePath);
        const embedding = this.bufferToArray(buffer);
        this.cache.add(hash);
        return embedding;
      }
  
      // Generate new embedding
      const embedding = await genEmbeddingOllama(text);
      
      // Save to both caches
      this.cache.add(hash);
      const buffer = this.arrayToBuffer(embedding);
      fs.writeFileSync(cacheFilePath, buffer);
  
      return embedding;
    }
  
    /**
     * Calculate cosine similarity between two embeddings
     */
    getCosineSimilarity(embedding1: number[], embedding2: number[]): number {
      if (!Array.isArray(embedding1) || !Array.isArray(embedding2) || 
          embedding1.length !== embedding2.length || embedding1.length === 0) {
        console.warn('Invalid embeddings provided for cosine similarity calculation');
        return 0;
      }
      
      let dotProduct = 0;
      let normA = 0;
      let normB = 0;
      
      for (let i = 0; i < embedding1.length; i++) {
        if (!Number.isFinite(embedding1[i]) || !Number.isFinite(embedding2[i])) continue;
        dotProduct += embedding1[i] * embedding2[i];
        normA += embedding1[i] * embedding1[i];
        normB += embedding2[i] * embedding2[i];
      }
      
      if (normA === 0 || normB === 0) {
        console.warn('Zero norm detected in cosine similarity calculation');
        return 0;
      }
      
      const similarity = dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
      return Math.max(-1, Math.min(1, similarity));
    }
  }
  const cache = new EmbeddingCache();
  
  // cached version of the embedding function
  export async function generateEmbedding(text: string): Promise<number[]> {
    return cache.getEmbedding(text);
  }
  
  
  export async function getCosineSimilarity(embedding1: number[], embedding2: number[]): Promise<number> {
    return cache.getCosineSimilarity(embedding1, embedding2);
  }
  

  
/**
 * Calculates the Levenshtein distance between two strings
 * 
 * @param str1 - First string to compare
 * @param str2 - Second string to compare
 * @returns The Levenshtein distance (number of edits needed to transform str1 into str2)
 */
export function levenshteinDistance(str1: string, str2: string): number {
    const m = str1.length;
    const n = str2.length;
    const dp: number[][] = Array(m + 1).fill(null).map(() => Array(n + 1).fill(0));
  
    for (let i = 0; i <= m; i++) dp[i][0] = i;
    for (let j = 0; j <= n; j++) dp[0][j] = j;
  
    for (let i = 1; i <= m; i++) {
      for (let j = 1; j <= n; j++) {
        if (str1[i - 1] === str2[j - 1]) {
          dp[i][j] = dp[i - 1][j - 1];
        } else {
          dp[i][j] = Math.min(
            dp[i - 1][j - 1] + 1,  // substitution
            dp[i - 1][j] + 1,      // deletion
            dp[i][j - 1] + 1       // insertion
          );
        }
      }
    }
    return dp[m][n];
  }
  