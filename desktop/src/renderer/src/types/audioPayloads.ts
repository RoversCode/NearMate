export type AudioDeviceSelectionPayload = {
  mode: "system_default" | "manual";
  microphone_id: string | null;
  microphone_name: string | null;
  speaker_loopback_id: string | null;
  speaker_loopback_name: string | null;
};
