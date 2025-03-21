import * as path from 'path';
import * as fs from 'fs';
import ollama from 'ollama';
import sharp from 'sharp';
import crypto from 'crypto';

// Cache directory path
const VISION_CACHE_DIR = path.join('_vision_response_cache');


if (!process.env.VISION_MODEL) {
  throw new Error("VISION_MODEL is not set");
}

const VISION_MODEL = process.env.VISION_MODEL;


// Ensure cache directory exists
if (!fs.existsSync(VISION_CACHE_DIR)) {
  fs.mkdirSync(VISION_CACHE_DIR, { recursive: true });
  console.log(`Created vision cache directory at ${VISION_CACHE_DIR}`);
}

/**
 * Creates a hash from an image file's contents
 * @param imagePath Path to the image file
 * @returns String hash of the image content
 */
async function hashImageFile(imagePath: string): Promise<string> {
  return new Promise((resolve, reject) => {
    try {
      const hash = crypto.createHash('sha256');
      const stream = fs.createReadStream(imagePath);
      
      stream.on('data', (data) => {
        hash.update(data);
      });
      
      stream.on('end', () => {
        resolve(hash.digest('hex'));
      });
      
      stream.on('error', (error) => {
        reject(error);
      });
    } catch (error) {
      reject(error);
    }
  });
}

/**
 * Get the cache file path for an image based on its hash
 * @param imageHash Hash of the image file
 * @returns Path to the cache file
 */
function getCacheFilePath(imageHash: string): string {
  return path.join(VISION_CACHE_DIR, `${imageHash}.txt`);
}

/**
 * Check if the image format is supported by Ollama vision model
 * @param imagePath Path to the image file
 * @returns Boolean indicating if the format is directly supported
 */
function isFormatSupportedByVisionModel(imagePath: string): boolean {
    const ext = path.extname(imagePath).toLowerCase();
    // Most vision models support these formats
    const supportedFormats = ['.jpg', '.jpeg', '.png'];
    return supportedFormats.includes(ext);
  }
  

/**
 * Generate a description for a single image using Ollama vision model
 * 
 * @param imagePath - Path to the image file
 * @returns A promise that resolves to the image description
 */
export async function generateImageDescriptionWithVisionLLM(imagePath: string): Promise<string> {
    try {
      // Generate hash from image file
      const imageHash = await hashImageFile(imagePath);
      const cacheFilePath = getCacheFilePath(imageHash);
      
      // Check if cached description exists
      if (fs.existsSync(cacheFilePath)) {
        const existingDescription = fs.readFileSync(cacheFilePath, 'utf8').trim();
        if (existingDescription) {
          console.log(`Using cached description for ${imagePath} (hash: ${imageHash.substring(0, 8)}...)`);
          return existingDescription;
        }
      }
      
      let imageToProcess = imagePath;
      
      // Check if the image format is supported
      if (!isFormatSupportedByVisionModel(imagePath)) {
        // If not supported, convert to PNG first
        const tempFileName = `temp_${path.basename(imagePath, path.extname(imagePath))}_${Date.now()}.png`;
        const tempFilePath = path.join(path.dirname(imagePath), tempFileName);
        
        // Convert the image to PNG format
        imageToProcess = await convertImageToPng(imagePath, tempFilePath);
        console.log(`Converted ${imagePath} to ${imageToProcess} for vision model compatibility`);
      }
      
      // Call Ollama vision model to describe the image
      const response = await ollama.chat({
        model: VISION_MODEL,
        messages: [{
          role: 'user',
          content: 'Describe the contents of this image in a concise sentence.',
          images: [imageToProcess]
        }]
      });
      
      // Extract the description from the response
      const description = response.message.content.trim();
      
      // Clean up temporary file if created
      if (imageToProcess !== imagePath && fs.existsSync(imageToProcess)) {
        fs.unlinkSync(imageToProcess);
      }
      
      // Save the description to cache file
      fs.writeFileSync(cacheFilePath, description, 'utf8');
      console.log(`Saved description to cache (hash: ${imageHash.substring(0, 8)}...)`);
      
      return description;
    } catch (error) {
      console.error(`Error generating description for image ${imagePath}:`, error);
      return 'Failed to generate description';
    }
  }
  
  /**
   * Generate descriptions for all images in a folder
   * 
   * @param folderPath - Path to the folder containing images
   * @returns A promise that resolves when all descriptions are generated
   */
  export async function generateDescriptionsForImagesInFolder(folderPath: string): Promise<void> {
    try {
      // Get all files in the folder
      const files = fs.readdirSync(folderPath);
      
      // Filter for image files
      const imageFiles = files.filter(file => {
        const ext = path.extname(file).toLowerCase();
        return ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'].includes(ext);
      });
      
      console.log(`Found ${imageFiles.length} images in folder ${folderPath}`);
      
      // Process each image
      for (let i = 0; i < imageFiles.length; i++) {
        const imagePath = path.join(folderPath, imageFiles[i]);
        console.log(`Processing image ${i + 1}/${imageFiles.length}: ${imagePath}`);
        
        // Generate description (will use cache if available)
        const description = await generateImageDescriptionWithVisionLLM(imagePath);
        
        console.log(`Description for ${imagePath}: "${description.substring(0, 50)}${description.length > 50 ? '...' : ''}"`);
      }
      
      console.log(`Completed processing ${imageFiles.length} images in ${folderPath}`);
    } catch (error) {
      console.error(`Error generating descriptions for images in folder ${folderPath}:`, error);
    }
  }

  /**
 * Convert an image to PNG format using Sharp
 * @param inputPath Path to the input image
 * @param outputPath Path to save the converted PNG
 * @returns Promise resolving to the output path
 */
async function convertImageToPng(inputPath: string, outputPath: string): Promise<string> {
    try {
      await sharp(inputPath)
        .png()
        .toFile(outputPath);
      return outputPath;
    } catch (error) {
      console.error(`Error converting image to PNG: ${error}`);
      throw error;
    }
  }
  