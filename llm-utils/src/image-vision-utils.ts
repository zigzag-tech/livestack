import * as path from 'path';
import * as fs from 'fs';
import sharp from 'sharp';
import { ChatMessage, generateJSONResponseOllama } from './ollama-client';

function getVisionModel(): string {
  if (!process.env.VISION_MODEL) {
    throw new Error("VISION_MODEL is not set");
  }
  return process.env.VISION_MODEL;
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

/**
 * Generate a description for a single image using Ollama vision model
 * 
 * @param imagePath - Path to the image file
 * @returns A promise that resolves to the image description
 */
export async function generateImageDescriptionWithVisionLLM(imagePath: string): Promise<{
  title: string;
  description: string;
}> {
  try {
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

    // Prepare message for vision model
    const messages: ChatMessage[] = [{
      role: 'user',
      content: `Describe the contents of this image in a concise sentence, and give it a short title. Return as a JSON object of the format { "title": "<title>", "desc": "<description>" }`,
      images: [imageToProcess]
    }];

    // Call Ollama vision model using the JSON response generator
    const { resultPromise } = await generateJSONResponseOllama<{ title: string, desc: string }>({
      messages,
      options: {
        model: getVisionModel(),
        temperature: 0.0
      },
      cache: true,
      logStream: false
    });

    const response = await resultPromise;
    if (response.status === 'failed') {
      throw new Error('Failed to generate image description');
    }

    // Clean up temporary file if created
    if (imageToProcess !== imagePath && fs.existsSync(imageToProcess)) {
      fs.unlinkSync(imageToProcess);
    }


    // Extract the description from the response
    if (response.status === 'success') {
      // The response will contain the full message content as a string, not in JSON format
      const result = response.content;
      // Handle different response formats - sometimes it might be a string directly,
      // other times it might be an object with a content property
      if (typeof result === 'object' && result !== null) {
        // Try to extract content from the result object
        return {
          title: result.title,
          description: result.desc
        };
      } else {
        throw new Error('Failed to generate image description');
      }
    } else {
      throw new Error('Failed to generate image description');
    }
  } catch (error) {
    console.error(`Error generating description for image ${imagePath}:`, error);
    throw error;
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

      console.log(`Description for ${imagePath}: "${description.title}": "${description.description.substring(0, 50)}${description.description.length > 50 ? '...' : ''}"`);
    }

    console.log(`Completed processing ${imageFiles.length} images in ${folderPath}`);
  } catch (error) {
    console.error(`Error generating descriptions for images in folder ${folderPath}:`, error);
  }
}
