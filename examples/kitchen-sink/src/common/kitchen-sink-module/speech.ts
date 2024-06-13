import axios from "axios";

export async function getTTSAudio(text: string) {
  const url = "https://api.openai.com/v1/audio/speech";
  const headers = {
    Authorization: `Bearer ${process.env.OPENAI_API_KEY}`,
  };
  const data = {
    model: "tts-1",
    input: text,
    voice: "alloy",
    response_format: "mp3",
  };
  const response = await axios.post(url, data, {
    headers: headers,
    responseType: "stream",
  });
  return response;
}
