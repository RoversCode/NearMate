import { describe, expect, it } from "vitest";
import {
  buildWindowBootstrapArgument,
  readWindowBootstrapArgument,
  type AvatarWindowBootstrap
} from "./windowBootstrap";

describe("window bootstrap argument", () => {
  it("只把窗口类型匹配的只读配置交给 preload", () => {
    const payload: AvatarWindowBootstrap = {
      kind: "avatar",
      runtime: {
        controlGateway: {
          httpBaseUrl: "http://127.0.0.1:8765",
          websocketUrl: "ws://127.0.0.1:8765/ws/control"
        },
        live2d: {
          defaultModelPath: "./live2d/resources/Haru/Haru.model3.json",
          catalog: []
        }
      }
    };
    const argument = buildWindowBootstrapArgument(payload);

    expect(readWindowBootstrapArgument([argument], "avatar")).toEqual(payload);
    expect(readWindowBootstrapArgument([argument], "chat")).toBeNull();
    expect(readWindowBootstrapArgument(["--companion-window-bootstrap=%7Bbad"], "avatar")).toBeNull();
  });
});
