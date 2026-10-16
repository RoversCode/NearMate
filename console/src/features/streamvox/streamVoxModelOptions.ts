export type StreamVoxModelOption = {
  value: string;
  label: string;
};

export const streamVoxModelOptions: StreamVoxModelOption[] = [
  { value: "qwen3-tts-clone-0.6b-gguf", label: "qwen3-tts-clone-0.6b-gguf" },
  { value: "qwen3-tts-clone-1.7b-gguf", label: "qwen3-tts-clone-1.7b-gguf" },
  { value: "s2-pro-4b-gguf", label: "s2-pro-4b-gguf" },
  { value: "voxcpm2-gguf", label: "voxcpm2-gguf" },
  { value: "moss-tts-nano-onnx", label: "moss-tts-nano-onnx" }
];
