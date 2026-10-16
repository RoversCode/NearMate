import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import { createLocalLiveKitConfiguration, readExternalLiveKitConnection, createLiveKitHealthToken } from "./liveKitConfiguration";

describe("LiveKit 安装态配置", () => {
  it("外部配置只有全无或完整两种有效状态，空白字段不能混入默认配置", () => {
    expect(readExternalLiveKitConnection({})).toBeNull();
    expect(() => readExternalLiveKitConnection({ LIVEKIT_URL: "ws://localhost:7880" })).toThrow("外部语音服务配置不完整");
    expect(() => readExternalLiveKitConnection({ LIVEKIT_URL: "ws://localhost:7880", LIVEKIT_API_KEY: "test", LIVEKIT_API_SECRET: " " })).toThrow("外部语音服务配置不完整");
    expect(readExternalLiveKitConnection({ livekit_url: "wss://example.test", livekit_api_key: "test-key", livekit_api_secret: "test-secret" })).toEqual({ url: "wss://example.test", apiKey: "test-key", apiSecret: "test-secret" });
  });

  it("非法地址和重复环境变量使用安全错误，不回显凭据", () => {
    const base = { LIVEKIT_URL: "wss://user:secret@example.test", LIVEKIT_API_KEY: "test", LIVEKIT_API_SECRET: "test" };
    expect(() => readExternalLiveKitConnection(base)).toThrow("外部语音服务地址无效");
    expect(() => readExternalLiveKitConnection({ ...base, LIVEKIT_URL: "wss://example.test", livekit_url: "wss://other.test" })).toThrow("外部语音服务配置存在重复字段");
  });

  it("信令仅绑定 loopback、媒体使用本机网卡候选且每次使用全新凭据", () => {
    const first = createLocalLiveKitConfiguration({ httpPort: 20001, tcpPort: 20002, udpPort: 20003 });
    const second = createLocalLiveKitConfiguration({ httpPort: 20004, tcpPort: 20005, udpPort: 20006 });
    const config = JSON.parse(first.configBody);
    expect(first.connection.url).toBe("ws://127.0.0.1:20001");
    expect(first.connection.apiKey).not.toBe(second.connection.apiKey);
    expect(first.connection.apiSecret).not.toBe(second.connection.apiSecret);
    expect(first.connection.apiSecret.length).toBeGreaterThanOrEqual(32);
    expect(config).toMatchObject({ port: 20001, bind_addresses: ["127.0.0.1"], rtc: { tcp_port: 20002, udp_port: 20003, enable_loopback_candidate: true, use_external_ip: false } });
    expect(config.rtc.node_ip).toBeUndefined();
    expect(config.rtc.ips).toBeUndefined();
    expect(config.keys[first.connection.apiKey]).toBe(first.connection.apiSecret);
  });

  it("就绪令牌只授予短时房间查询权限，签名使用当前随机凭据", () => {
    const connection = { url: "ws://127.0.0.1:1", apiKey: "test-key", apiSecret: "test-secret" };
    const token = createLiveKitHealthToken(connection, 1_000_000);
    const [header, payload, signature] = token.split(".");
    expect(JSON.parse(Buffer.from(payload, "base64url").toString())).toMatchObject({ iss: "test-key", exp: 1030, nbf: 995, video: { roomList: true } });
    expect(signature).toBe(createHmac("sha256", "test-secret").update(`${header}.${payload}`).digest("base64url"));
  });
});
