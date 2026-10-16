import { describe, expect, it, vi } from "vitest";

import {
  activateCharacterCard,
  calibrateCharacterCardVoiceCaption,
  captureCharacterCardVoiceFromRuntime,
  cloneCharacterCard,
  deleteCharacterCardCover,
  fetchActiveCharacterCard,
  fetchCharacterCards,
  generateCharacterCardStreamVoxExamples,
  updateCharacterCard,
  uploadCharacterCardCover,
  uploadCharacterCardVoiceReference
} from "./characterCards";
import { createCharacterCardPayload, createCharacterCardsPayload } from "../../test/characterCardFixtures";
describe("character card api", () => {
  it("读取角色卡列表、激活卡和当前卡并校验响应契约", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(createCharacterCardsPayload()), {
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(createCharacterCardPayload()), {
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...createCharacterCardPayload(), effect_message: "切换角色卡会在新会话生效" }), {
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ ...createCharacterCardPayload(), effect_message: "新会话生效" }), {
          headers: { "Content-Type": "application/json" }
        })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(createCharacterCardPayload()), {
          headers: { "Content-Type": "application/json" }
        })
      );

    vi.stubGlobal("fetch", fetchMock);

    const cardsPayload = await fetchCharacterCards("http://127.0.0.1:8765");
    const activeCardPayload = await fetchActiveCharacterCard("http://127.0.0.1:8765");
    await activateCharacterCard("http://127.0.0.1:8765", "card-1");
    await updateCharacterCard("http://127.0.0.1:8765", "card-1", createCharacterCardPayload().card!);
    await cloneCharacterCard("http://127.0.0.1:8765", {
      source_card_id: "card-1",
      card_id: "card-2",
      name: "火花",
      title: "克隆卡"
    });

    expect(cardsPayload.cards[0].meta.card_id).toBe("card-1");
    if (activeCardPayload.card === null) {
      throw new Error("active card should be present");
    }
    expect(activeCardPayload.card.meta.name).toBe("火花");
    expect(activeCardPayload.card.meta.avatar_path).toBeNull();
    expect(fetchMock).toHaveBeenNthCalledWith(1, "http://127.0.0.1:8765/api/character-cards");
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8765/api/character-cards/active");
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "http://127.0.0.1:8765/api/character-cards/card-1/activate",
      { method: "POST" }
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "http://127.0.0.1:8765/api/character-cards/card-1",
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(createCharacterCardPayload().card)
      }
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      "http://127.0.0.1:8765/api/character-cards/card-1/clone",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          card_id: "card-2",
          name: "火花",
          title: "克隆卡"
        })
      }
    );

    vi.unstubAllGlobals();
  });

  it("根据页面草稿生成 StreamVox 模型示例并校验响应契约", async () => {
    const card = createCharacterCardPayload().card;
    if (card === null) {
      throw new Error("test card should be present");
    }
    const generatedExamples = {
      items: [
        {
          title: "情绪承接",
          user: "我今天好累",
          char: "[sigh]先坐下，我在。",
          expected_tool: null
        },
        {
          title: "现场感知",
          user: "世界杯踢到第几强了？",
          char: "我确认一下。",
          expected_tool: "lookup_current_fact"
        }
      ]
    };
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify(generatedExamples), {
        headers: { "Content-Type": "application/json" }
      });
    });

    vi.stubGlobal("fetch", fetchMock);

    const payload = await generateCharacterCardStreamVoxExamples("http://127.0.0.1:8765", "card-1", "s2-pro-4b-gguf", card);

    expect(payload).toEqual(generatedExamples);
    expect(fetchMock).toHaveBeenCalledWith("http://127.0.0.1:8765/api/character-cards/card-1/streamvox-examples/generate", {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        card,
        target_model: "s2-pro-4b-gguf"
      })
    });

    vi.unstubAllGlobals();
  });

  it("从当前 StreamVox runtime 捕获语音绑定到角色卡", async () => {
    const capturedPayload = createCharacterCardPayload();
    if (capturedPayload.card === null) {
      throw new Error("active card should be present");
    }
    capturedPayload.card.meta.voice_binding = {
      streamvox_model: "s2-pro-4b-gguf",
      role_name: "trial_role",
      reference_audio_path: "voice/reference.wav",
      prompt_text: "这是一段参考音频文本。",
      seed: 7,
      inference_options: { mode: 1 },
      audio_sha256: "first-audio-sha",
      caption_timing: null
    };
    capturedPayload.voice_match_status = "matched";
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify(capturedPayload), {
        status: 202,
        headers: { "Content-Type": "application/json" }
      })
    );

    vi.stubGlobal("fetch", fetchMock);

    const payload = await captureCharacterCardVoiceFromRuntime("http://127.0.0.1:8765", "card-1");

    expect(payload.card?.meta.voice_binding.role_name).toBe("trial_role");
    expect(payload.card?.meta.voice_binding.audio_sha256).toBe("first-audio-sha");
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/character-cards/card-1/voice/capture-from-runtime",
      { method: "POST" }
    );

    vi.unstubAllGlobals();
  });

  it("测算角色卡字幕语速会调用校准接口并返回更新后的角色卡", async () => {
    const calibratedPayload = createCharacterCardPayload();
    if (calibratedPayload.card === null) {
      throw new Error("active card should be present");
    }
    calibratedPayload.card.meta.voice_binding.caption_timing = {
      ms_per_unit: 210,
      calibration_text: "我在这里，听见你说话了。",
      duration_ms: 8400,
      unit_count: 40,
      measured_at: 1710000000
    };
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify(calibratedPayload), {
        status: 202,
        headers: { "Content-Type": "application/json" }
      })
    );

    vi.stubGlobal("fetch", fetchMock);

    const payload = await calibrateCharacterCardVoiceCaption("http://127.0.0.1:8765", "card-1");

    expect(payload.card?.meta.voice_binding.caption_timing?.ms_per_unit).toBe(210);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/character-cards/card-1/voice-caption-calibration",
      { method: "POST" }
    );

    vi.unstubAllGlobals();
  });

  it("上传角色卡参考音频只携带音频文件并返回资产路径", async () => {
    const uploadResult = {
      reference_audio_path: "voice/reference.wav",
      effect_message: "参考音频已上传，保存当前卡后生效"
    };
    const fetchMock = vi.fn().mockResolvedValueOnce(
      new Response(JSON.stringify(uploadResult), {
        status: 202,
        headers: { "Content-Type": "application/json" }
      })
    );

    vi.stubGlobal("fetch", fetchMock);

    const audioFile = new File(["audio"], "reference.wav", { type: "audio/wav" });
    const result = await uploadCharacterCardVoiceReference("http://127.0.0.1:8765", "card-1", audioFile);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:8765/api/character-cards/card-1/voice/reference-audio",
      expect.objectContaining({
        method: "POST",
        body: expect.any(FormData)
      })
    );
    const formData = fetchMock.mock.calls[0][1]?.body as FormData;
    expect(formData.get("card")).toBeNull();
    expect(formData.get("audio_file")).toBe(audioFile);
    expect(result).toEqual(uploadResult);

    vi.unstubAllGlobals();
  });

  it("旧角色卡字段响应会被契约拒绝", async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          card: {
            ...createCharacterCardPayload().card,
            origin: {
              past_identity: "旧字段",
              origin_story: "旧字段",
              real_world_texture: "旧字段"
            }
          },
          active_card_id: "card-1",
          shared_world_canon: "shared world canon",
          validation_messages: [],
          effect_message: null
        }),
        {
          headers: {
            "Content-Type": "application/json"
          }
        }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchCharacterCards("http://127.0.0.1:8765")).rejects.toThrow(
      "读取角色卡失败：响应格式不符合控制台契约"
    );

    vi.unstubAllGlobals();
  });

  it.each([
    ["缺失 expected_tool", { title: "问候", user: "你好", char: "你好呀" }],
    ["非法 expected_tool", { title: "问候", user: "你好", char: "你好呀", expected_tool: "browse_everything" }],
    ["旧 route/brief 协议", { title: "问候", user: "你好", char: "你好呀", route: null, brief: null }]
  ])("拒绝%s的角色卡响应", async (_caseName, invalidExample) => {
    const invalidPayload = JSON.parse(JSON.stringify(createCharacterCardsPayload())) as {
      cards: Array<{
        voiceprint: {
          streamvox_examples: {
            generic: { items: unknown[] };
          };
        };
      }>;
    };
    invalidPayload.cards[0].voiceprint.streamvox_examples.generic.items = [invalidExample];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(invalidPayload), { headers: { "Content-Type": "application/json" } }))
    );

    await expect(fetchCharacterCards("http://127.0.0.1:8765")).rejects.toThrow(
      "读取角色卡失败：响应格式不符合控制台契约"
    );

    vi.unstubAllGlobals();
  });

  it("旧 StreamVox intent 字段响应会被契约拒绝", async () => {
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("test card should be present");
    }
    payload.card.voiceprint.streamvox_examples.generic.items = [
      {
        title: "旧协议",
        user: "帮我查资料",
        char: "好，稍等。",
        intent: "查询资料。"
      } as never
    ];
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify(payload), {
        headers: { "Content-Type": "application/json" }
      });
    });

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchActiveCharacterCard("http://127.0.0.1:8765")).rejects.toThrow(
      "读取当前角色卡失败：响应格式不符合控制台契约"
    );

    vi.unstubAllGlobals();
  });

  it("上传和删除角色卡封面使用封面 API 契约", async () => {
    const uploadedPayload = createCharacterCardPayload();
    if (uploadedPayload.card === null) {
      throw new Error("active card should be present");
    }
    uploadedPayload.card.meta.avatar_path = "assets/icon.png";
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(uploadedPayload), { status: 202 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(createCharacterCardPayload()), { status: 202 }));

    vi.stubGlobal("fetch", fetchMock);

    const imageFile = new File(["image"], "icon.png", { type: "image/png" });
    const uploadPayload = await uploadCharacterCardCover("http://127.0.0.1:8765", "card-1", imageFile);
    const deletePayload = await deleteCharacterCardCover("http://127.0.0.1:8765", "card-1");

    expect(uploadPayload.card?.meta.avatar_path).toBe("assets/icon.png");
    expect(deletePayload.card?.meta.avatar_path).toBeNull();
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://127.0.0.1:8765/api/character-cards/card-1/cover",
      expect.objectContaining({
        method: "POST",
        body: expect.any(FormData)
      })
    );
    const formData = fetchMock.mock.calls[0][1]?.body as FormData;
    expect(formData.get("image_file")).toBe(imageFile);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "http://127.0.0.1:8765/api/character-cards/card-1/cover", {
      method: "DELETE"
    });

    vi.unstubAllGlobals();
  });

  it("角色卡 meta.avatar_path 类型错误时拒绝响应", async () => {
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("active card should be present");
    }
    const card = payload.card;
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          ...payload,
          card: {
            ...card,
            meta: {
              ...card.meta,
              avatar_path: 123
            }
          }
        }),
        { headers: { "Content-Type": "application/json" } }
      );
    });

    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchActiveCharacterCard("http://127.0.0.1:8765")).rejects.toThrow(
      "读取当前角色卡失败：响应格式不符合控制台契约"
    );

    vi.unstubAllGlobals();
  });
});

