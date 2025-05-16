import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';


export class EmbeddingCache {
    private cacheDir: string;
    private cache: Set<string>;
    private embeddingFn: (text: string) => Promise<number[]>;
    constructor({ embeddingFn, cacheDir = '.embedding-cache' }: { embeddingFn: (text: string) => Promise<number[]>; cacheDir?: string; }) {
        this.embeddingFn = embeddingFn;
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
            if (!fs.existsSync(cacheFilePath)) {
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
        const embedding = await this.embeddingFn(text);

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
            throw new Error('Invalid embeddings provided for cosine similarity calculation. embedding1.length: ' + embedding1.length + ' embedding2.length: ' + embedding2.length);
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
