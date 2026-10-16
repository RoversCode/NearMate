import { describe, expect, it } from "vitest";
import { readAvatarPetBounds } from "./avatarWindowInteractionPayloads";

describe("avatar window interaction payloads", () => {
  it("readAvatarPetBounds 只接受有限数字边界，并保留 null 清除信号", () => {
    expect(readAvatarPetBounds({
      x: 12,
      y: 24,
      width: 128,
      height: 256
    })).toEqual({
      x: 12,
      y: 24,
      width: 128,
      height: 256
    });
    expect(readAvatarPetBounds(null)).toBeNull();
    expect(readAvatarPetBounds({
      x: Number.NaN,
      y: 24,
      width: 128,
      height: 256
    })).toBeNull();
  });
});
