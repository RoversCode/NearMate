import { describe, expect, it } from "vitest";
import config from "./electron.vite.config";

describe("electron-vite sandbox preload build", () => {
  it("把唯一 preload 入口构建为自包含 CommonJS 文件", () => {
    const staticConfig = config as {
      preload?: {
        build?: {
          rollupOptions?: {
            input?: unknown;
            output?: unknown;
          };
        };
      };
    };

    expect(staticConfig.preload?.build?.rollupOptions?.input).toEqual({
      sandbox: expect.stringMatching(/src\/preload\/index\.ts$/)
    });
    expect(staticConfig.preload?.build?.rollupOptions?.output).toMatchObject({
      entryFileNames: "[name].cjs",
      format: "cjs",
      inlineDynamicImports: true
    });
  });

  it("renderer 公共目录保留 Live2D 运行时与模型资源", () => {
    const staticConfig = config as {
      renderer?: {
        publicDir?: unknown;
      };
    };

    expect(staticConfig.renderer?.publicDir).toMatch(/src[\\/]renderer[\\/]public$/);
  });

  it("renderer 固定共享开发端口并拒绝自动顺延", () => {
    const staticConfig = config as {
      renderer?: {
        server?: {
          host?: string;
          port?: number;
          strictPort?: boolean;
        };
      };
    };

    expect(staticConfig.renderer?.server).toEqual({
      host: "127.0.0.1",
      port: 5174,
      strictPort: true
    });
  });
});
