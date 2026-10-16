import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useEffect } from "react";
import { describe, expect, it, vi } from "vitest";

import type { CharacterCardPayload, CharacterCardStreamVoxExampleSet } from "../../api/contracts/characterCards";
import { CharacterCardPage } from "./CharacterCardPage";
import {
  createCharacterCardPayload,
  createCharacterCardsPayload,
  createCharacterCardsPayloadWithYaeMiko
} from "../../test/characterCardFixtures";

vi.mock("react-easy-crop", () => ({
  default: ({ onCropComplete }: { onCropComplete: (_croppedArea: unknown, croppedAreaPixels: unknown) => void }) => {
    useEffect(() => {
      onCropComplete({}, { x: 0, y: 0, width: 400, height: 520 });
    }, []);
    return <div data-testid="cover-cropper-surface" />;
  }
}));

vi.mock("./coverImageProcessing", async () => {
  const actual = await vi.importActual<typeof import("./coverImageProcessing")>("./coverImageProcessing");
  return {
    ...actual,
    createImagePreview: vi.fn(async () => ({
      image: { naturalWidth: 400, naturalHeight: 520 },
      objectUrl: "blob:cover-preview"
    })),
    createCroppedCoverFile: vi.fn(async () => new File(["jpeg"], "icon.jpg", { type: "image/jpeg" }))
  };
});

describe("CharacterCardPage", () => {
  it.each([
    ["背景解释", "术语名称 1"],
    ["社交与羁绊", "关系类型 1"],
    ["声纹与台词", "示例用户 1"],
    ["模型示例", "示例标题 1"],
    ["模型示例", "示例用户 1"]
  ])("编辑%s的%s时保留焦点，可以连续输入", (section, label) => {
    render(<CharacterCardPage activeCard={createCharacterCardPayload()} cardsPayload={createCharacterCardsPayload()}
      isLoading={false} onActivate={vi.fn()} onClone={vi.fn()} onSave={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: section }));

    const field = screen.getByLabelText(label) as HTMLInputElement | HTMLTextAreaElement;
    field.focus();
    fireEvent.change(field, { target: { value: "持续编辑" } });
    expect(document.activeElement).toBe(screen.getByLabelText(label));
    fireEvent.change(document.activeElement!, { target: { value: "持续编辑这条内容" } });
    expect((screen.getByLabelText(label) as HTMLInputElement | HTMLTextAreaElement).value).toBe("持续编辑这条内容");
  });

  it("切换另一张卡前保留并提示未保存编辑，继续编辑不会读取旧快照", () => {
    const select = vi.fn(async () => undefined);
    render(<CharacterCardPage activeCard={createCharacterCardPayload()} cardsPayload={createCharacterCardsPayloadWithYaeMiko()}
      isLoading={false} onActivate={vi.fn()} onClone={vi.fn()} onSave={vi.fn()} onSelectCard={select} />);
    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.change(screen.getByLabelText("角色称呼"), { target: { value: "新的称呼" } });
    expect((screen.getByRole("button", { name: "激活卡" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "返回角色库" }));
    fireEvent.click(screen.getByRole("button", { name: /八重神子/ }));
    expect(screen.getByRole("dialog").textContent).toContain("未保存修改");
    expect(select).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "继续编辑" }));
    expect((screen.getByLabelText("角色称呼") as HTMLInputElement).value).toBe("新的称呼");
  });

  it("封面响应只接管未编辑字段，保留同一卡的人格草稿和语音草稿", async () => {
    const payload = createCharacterCardPayload();
    const onSave = vi.fn(async () => undefined);
    const props = { activeCard: payload, cardsPayload: createCharacterCardsPayload(), isLoading: false,
      onActivate: vi.fn(), onClone: vi.fn(), onSave };
    const { rerender } = render(<CharacterCardPage {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.change(screen.getByLabelText("角色称呼"), { target: { value: "还没保存的新名字" } });
    fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));
    fireEvent.change(screen.getByLabelText("Prompt 文本"), { target: { value: "还没保存的语音文本" } });

    const refreshedPayload = createCharacterCardPayload();
    refreshedPayload.card!.meta.avatar_path = "assets/new-cover.jpg";
    refreshedPayload.card!.meta.voice_binding.caption_timing = {
      ms_per_unit: 200, calibration_text: "校准文本", duration_ms: 800, unit_count: 4, measured_at: 1
    };
    rerender(<CharacterCardPage {...props} activeCard={refreshedPayload}
      cardsPayload={{ ...props.cardsPayload, cards: [refreshedPayload.card!] }} />);
    fireEvent.click(screen.getByRole("button", { name: "基础档案" }));
    expect((screen.getByLabelText("角色称呼") as HTMLInputElement).value).toBe("还没保存的新名字");
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ meta: expect.objectContaining({
      name: "还没保存的新名字", avatar_path: "assets/new-cover.jpg",
      voice_binding: expect.objectContaining({ prompt_text: "还没保存的语音文本" })
    }) })));
  });

  it.each(["保存期间继续输入的名字", "火花"])("保存返回后保留请求期间改成的%s，同时接纳已提交字段的规范化结果", async laterName => {
    const payload = createCharacterCardPayload();
    let finishSave!: (value: CharacterCardPayload) => void;
    const onSave = vi.fn(() => new Promise<CharacterCardPayload>(resolve => { finishSave = resolve; }));
    const props = { activeCard: payload, cardsPayload: createCharacterCardsPayload(), isLoading: false,
      onActivate: vi.fn(), onClone: vi.fn(), onSave };
    const { rerender } = render(<CharacterCardPage {...props} />);
    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.change(screen.getByLabelText("角色称呼"), { target: { value: "提交时的名字" } });
    fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));
    fireEvent.change(screen.getByLabelText("Prompt 文本"), { target: { value: "  新语音文本  " } });
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));
    fireEvent.click(screen.getByRole("button", { name: "基础档案" }));
    fireEvent.change(screen.getByLabelText("角色称呼"), { target: { value: laterName } });

    const savedPayload = createCharacterCardPayload();
    savedPayload.card!.meta.name = "提交时的名字";
    savedPayload.card!.meta.voice_binding.prompt_text = "新语音文本";
    savedPayload.card!.meta.voice_binding.audio_sha256 = "server-audio-hash";
    // 先投递父级快照，再结束回调，覆盖两条异步更新顺序不同的情况。
    rerender(<CharacterCardPage {...props} activeCard={savedPayload}
      cardsPayload={{ ...props.cardsPayload, cards: [savedPayload.card!] }} />);
    await act(async () => finishSave(savedPayload));
    expect((screen.getByLabelText("角色称呼") as HTMLInputElement).value).toBe(laterName);
    fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));
    expect((screen.getByLabelText("Prompt 文本") as HTMLTextAreaElement).value).toBe("新语音文本");
    expect((screen.getByRole("button", { name: "激活卡" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("校准回执更新语音信息但保留正在编辑的人格字段", async () => {
    const calibratedPayload = createCharacterCardPayload();
    calibratedPayload.card!.meta.voice_binding.caption_timing = {
      ms_per_unit: 210, calibration_text: "校准文本", duration_ms: 840, unit_count: 4, measured_at: 1
    };
    render(<CharacterCardPage activeCard={createCharacterCardPayload()} cardsPayload={createCharacterCardsPayload()}
      isLoading={false} onActivate={vi.fn()} onClone={vi.fn()} onSave={vi.fn()}
      onCalibrateVoiceCaption={vi.fn(async () => calibratedPayload)} />);
    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.change(screen.getByLabelText("角色称呼"), { target: { value: "尚未提交的称呼" } });
    fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));
    fireEvent.click(screen.getByRole("button", { name: "测算字幕语速" }));
    await screen.findByText("每单位 210ms · 音频 0.8s");
    fireEvent.click(screen.getByRole("button", { name: "基础档案" }));
    expect((screen.getByLabelText("角色称呼") as HTMLInputElement).value).toBe("尚未提交的称呼");
  });

  it("切换角色后到达的示例生成结果不会写入另一张角色卡", async () => {
    let finishGeneration!: (value: CharacterCardStreamVoxExampleSet) => void;
    const onGenerateStreamVoxExamples = vi.fn(() => new Promise<CharacterCardStreamVoxExampleSet>(resolve => { finishGeneration = resolve; }));
    const onSave = vi.fn(async () => undefined);
    render(<CharacterCardPage activeCard={createCharacterCardPayload()} cardsPayload={createCharacterCardsPayloadWithYaeMiko()}
      isLoading={false} onActivate={vi.fn()} onClone={vi.fn()} onSave={onSave}
      onGenerateStreamVoxExamples={onGenerateStreamVoxExamples} />);
    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "模型示例" }));
    fireEvent.click(screen.getByRole("button", { name: "生成模型示例" }));
    fireEvent.click(screen.getByRole("button", { name: "返回角色库" }));
    fireEvent.click(screen.getByRole("button", { name: /八重神子/ }));
    await act(async () => finishGeneration({ items: [{ title: "迟到的旧角色示例", user: "问", char: "答", expected_tool: null }] }));
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(JSON.stringify(onSave.mock.calls[0])).not.toContain("迟到的旧角色示例");
  });

  it("切换角色后迟到的参考音频上传不改写新角色的绑定", async () => {
    let finishUpload!: (value: { reference_audio_path: string; effect_message: string }) => void;
    const onUploadVoiceReference = vi.fn(() => new Promise<{ reference_audio_path: string; effect_message: string }>(resolve => { finishUpload = resolve; }));
    const onSave = vi.fn(async () => undefined);
    render(<CharacterCardPage activeCard={createCharacterCardPayload()} cardsPayload={createCharacterCardsPayloadWithYaeMiko()}
      isLoading={false} onActivate={vi.fn()} onClone={vi.fn()} onSave={onSave}
      onUploadVoiceReference={onUploadVoiceReference} />);
    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));
    fireEvent.change(screen.getByLabelText("上传参考音频"), { target: { files: [new File(["audio"], "old-card.wav", { type: "audio/wav" })] } });
    fireEvent.click(screen.getByRole("button", { name: "返回角色库" }));
    fireEvent.click(screen.getByRole("button", { name: /八重神子/ }));
    await act(async () => finishUpload({ reference_audio_path: "voice/old-card.wav", effect_message: "旧卡上传完成" }));
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));
    await waitFor(() => expect(onSave).toHaveBeenCalled());
    expect(JSON.stringify(onSave.mock.calls[0])).not.toContain("voice/old-card.wav");
    expect(screen.queryByText("旧卡上传完成")).toBeNull();
  });

  it("首次读取失败时展示契约错误而不是正常空态", () => {
    render(
      <CharacterCardPage
        activeCard={null}
        cardsPayload={null}
        errorMessage="读取角色卡失败：响应格式不符合控制台契约"
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    expect(screen.getByText("角色卡加载失败")).toBeTruthy();
    expect(screen.getByText("读取角色卡失败：响应格式不符合控制台契约")).toBeTruthy();
    expect(screen.queryByText("没有可编辑的角色卡。")).toBeNull();
  });

  it("写入成功但附属列表刷新失败时展示状态提醒而不是保存失败", () => {
    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        warningMessage="已保存，但页面状态刷新失败：角色卡列表暂时不可用"
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    expect(screen.getByRole("status").textContent).toContain("已保存，但页面状态刷新失败");
    expect(screen.queryByText("角色卡操作失败：已保存，但页面状态刷新失败：角色卡列表暂时不可用")).toBeNull();
  });

  it("接口成功但没有角色卡时展示正常空态", () => {
    render(
      <CharacterCardPage
        activeCard={{
          card: null,
          active_card_id: "",
          shared_world_canon: "",
          validation_messages: [],
          effect_message: null,
          voice_match_status: null
        }}
        cardsPayload={{ cards: [], active_card_id: "", shared_world_canon: "" }}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    expect(screen.getByText("没有可编辑的角色卡。")).toBeTruthy();
    expect(screen.queryByText("角色卡加载失败")).toBeNull();
  });

  it("默认展示角色卡图库，点击卡片后进入五段式详情编辑", async () => {
    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    expect(screen.getByRole("heading", { name: "角色卡图库" })).toBeTruthy();
    expect(screen.getByRole("button", { name: /火花/ })).toBeTruthy();
    expect(screen.getByText("温柔而直接的伴侣")).toBeTruthy();
    expect(screen.getByText("当前激活")).toBeTruthy();
    expect(screen.queryByLabelText("角色称呼")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));

    expect(screen.getByRole("button", { name: "基础档案" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "灵魂内核" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "社交与羁绊" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "声纹与台词" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "模型约束指令" })).toBeTruthy();
    expect(screen.getByLabelText("角色称呼")).toBeTruthy();
    expect(screen.getByLabelText("卡片标题 / 人物画像")).toBeTruthy();
    expect(screen.getByText("她叫什么，或者平时大家怎么叫她。")).toBeTruthy();
    expect(screen.getByText("一句话概括这张卡最直观的人物印象，例如：普通的现实女孩。")).toBeTruthy();
    expect(screen.queryByText("高级设定")).toBeNull();
    expect(screen.queryByText("保存后会在新会话读取最新版本。")).toBeNull();
  });

  it("可以从详情编辑返回角色卡图库", async () => {
    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    expect(screen.getByLabelText("角色称呼")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "返回角色库" }));
    expect(screen.getByRole("heading", { name: "角色卡图库" })).toBeTruthy();
    expect(screen.queryByLabelText("角色称呼")).toBeNull();
  });

  it("图库和详情页展示角色封面，并可以上传裁剪后的 JPEG 和删除封面", async () => {
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("测试角色卡不应为空");
    }
    payload.card.meta.avatar_path = "assets/icon.png";
    const onUploadCover = vi.fn(async () => undefined);
    const onDeleteCover = vi.fn(async () => undefined);

    render(
      <CharacterCardPage
        activeCard={payload}
        cardsPayload={{ ...createCharacterCardsPayload(), cards: [payload.card] }}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onDeleteCover={onDeleteCover}
        onSave={vi.fn()}
        onUploadCover={onUploadCover}
      />
    );

    const galleryImage = screen.getByRole("img", { name: "火花封面" });
    expect(galleryImage.getAttribute("src")).toBe("/api/character-cards/card-1/asset/assets/icon.png");

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    expect(screen.getByRole("img", { name: "火花形象照片" })).toBeTruthy();

    const uploadButton = screen.getByRole("button", { name: "上传形象照片" });
    const fileInput = screen.getByLabelText("上传形象照片") as HTMLInputElement;
    const chooseFile = vi.spyOn(fileInput, "click");
    uploadButton.focus();
    expect(document.activeElement).toBe(uploadButton);
    fireEvent.click(uploadButton);
    expect(chooseFile).toHaveBeenCalledOnce();
    chooseFile.mockRestore();

    const imageFile = new File(["image"], "icon.png", { type: "image/png" });
    fireEvent.change(screen.getByLabelText("上传形象照片"), {
      target: {
        files: [imageFile]
      }
    });

    expect(await screen.findByRole("dialog", { name: "裁剪形象照片" })).toBeTruthy();
    expect(onUploadCover).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "确认使用" }));
    fireEvent.click(screen.getByRole("button", { name: "删除形象照片" }));

    await waitFor(() => {
      expect(onUploadCover).toHaveBeenCalledWith("card-1", expect.objectContaining({ name: "icon.jpg", type: "image/jpeg" }));
      expect(onDeleteCover).toHaveBeenCalledWith("card-1");
    });
  });

  it("拒绝不支持类型和超过 20MB 的原图，不打开裁剪器", async () => {
    const onUploadCover = vi.fn(async () => undefined);

    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={vi.fn()}
        onUploadCover={onUploadCover}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.change(screen.getByLabelText("上传形象照片"), {
      target: {
        files: [new File(["gif"], "cover.gif", { type: "image/gif" })]
      }
    });

    expect(await screen.findByText("形象照片只支持 PNG 或 JPEG")).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "裁剪形象照片" })).toBeNull();
    expect(onUploadCover).not.toHaveBeenCalled();

    fireEvent.change(screen.getByLabelText("上传形象照片"), {
      target: {
        files: [new File([new Uint8Array(20 * 1024 * 1024 + 1)], "cover.jpg", { type: "image/jpeg" })]
      }
    });

    expect(await screen.findByText("原图不能超过 20MB")).toBeTruthy();
    expect(screen.queryByRole("dialog", { name: "裁剪形象照片" })).toBeNull();
  });

  it("可以在基础档案和灵魂内核中编辑并保存", async () => {
    const onSave = vi.fn(async () => undefined);

    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.change(screen.getByLabelText("卡片标题 / 人物画像"), {
      target: {
        value: "普通的现实女孩"
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "灵魂内核" }));
    fireEvent.change(screen.getByLabelText("核心矛盾感"), {
      target: {
        value: "外冷静、内护短。"
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          meta: expect.objectContaining({ title: "普通的现实女孩" }),
          inner_core: expect.objectContaining({ core_contradiction: "外冷静、内护短。" })
        })
      );
    });
  });

  it("可以编辑背景解释术语，并在保存时保留结构化字段", async () => {
    const onSave = vi.fn(async () => undefined);

    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "背景解释" }));
    fireEvent.change(screen.getByLabelText("术语名称 1"), {
      target: {
        value: "鸣神大社"
      }
    });
    fireEvent.change(screen.getByLabelText("术语别名 1"), {
      target: {
        value: "神社\n大社\n神社"
      }
    });
    fireEvent.change(screen.getByLabelText("术语解释 1"), {
      target: {
        value: "稻妻信仰与祭祀中心，八重神子作为宫司执掌此处。"
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          background_context: {
            terms: [
              {
                name: "鸣神大社",
                aliases: ["神社", "大社"],
                explanation: "稻妻信仰与祭祀中心，八重神子作为宫司执掌此处。"
              }
            ]
          }
        })
      );
    });
  });

  it("可以编辑社交羁绊、声纹台词和模型约束", async () => {
    const onSave = vi.fn(async () => undefined);

    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "社交与羁绊" }));
    fireEvent.change(screen.getByLabelText("主控羁绊类型"), {
      target: {
        value: "命定陪伴"
      }
    });
    fireEvent.change(screen.getByLabelText("应激反应 1"), {
      target: {
        value: "用户被欺负时 → 先站到用户这边。"
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "声纹与台词" }));
    fireEvent.change(screen.getByLabelText("标志性口语词"), {
      target: {
        value: "我在\n先别急"
      }
    });
    fireEvent.change(screen.getByLabelText("示例用户 1"), {
      target: {
        value: "今天好累"
      }
    });
    fireEvent.change(screen.getByLabelText("示例角色 1"), {
      target: {
        value: "先别硬撑，我在。"
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "模型约束指令" }));
    fireEvent.change(screen.getByLabelText("人设绝对禁忌"), {
      target: {
        value: "绝不准变成客服。"
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          relationships: expect.objectContaining({
            user_bond: expect.objectContaining({ bond_type: "命定陪伴" }),
            stress_reactions: ["用户被欺负时 → 先站到用户这边。"]
          }),
          voiceprint: expect.objectContaining({
            catchphrases: ["我在", "先别急"],
            example_dialogues: {
              items: [
                {
                  user: "今天好累",
                  char: "先别硬撑，我在。"
                }
              ]
            }
          }),
          model_constraints: expect.objectContaining({
            persona_forbidden_rules: "绝不准变成客服。"
          })
        })
      );
    });
  });

  it("唤醒入口用独立输入框管理唤醒词，并在保存时过滤空白项", async () => {
    const onSave = vi.fn(async () => undefined);
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("测试角色卡不应为空");
    }
    payload.card.meta.wakeword.keywords = ["火花", "火花同学"];

    render(
      <CharacterCardPage
        activeCard={payload}
        cardsPayload={{ ...createCharacterCardsPayload(), cards: [payload.card] }}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "唤醒入口" }));

    expect((screen.getByLabelText("唤醒词 1") as HTMLInputElement).value).toBe("火花");
    expect((screen.getByLabelText("唤醒词 2") as HTMLInputElement).value).toBe("火花同学");

    fireEvent.change(screen.getByLabelText("唤醒词 1"), {
      target: { value: "小火花" }
    });
    fireEvent.click(screen.getByRole("button", { name: "新增唤醒词" }));
    fireEvent.change(screen.getByLabelText("唤醒词 3"), {
      target: { value: "   " }
    });
    fireEvent.click(screen.getAllByRole("button", { name: "删除唤醒词" })[1]);
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          meta: expect.objectContaining({
            wakeword: {
              keywords: ["小火花"]
            }
          })
        })
      );
    });
  });

  it("基础档案直接展示并保存每张卡自己的主人称呼", async () => {
    const onSave = vi.fn(async () => undefined);
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("测试角色卡不应为空");
    }
    payload.card.meta.user_call_name = "哥哥";

    render(
      <CharacterCardPage
        activeCard={payload}
        cardsPayload={{ ...createCharacterCardsPayload(), cards: [payload.card] }}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    expect((screen.getByLabelText("主人称呼") as HTMLInputElement).value).toBe("哥哥");

    fireEvent.change(screen.getByLabelText("主人称呼"), {
      target: { value: "宝宝" }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          meta: expect.objectContaining({
            user_call_name: "宝宝"
          })
        })
      );
    });
  });

  it("唤醒回复操作会携带当前编辑卡编号", async () => {
    const onDeleteWakeResponseItem = vi.fn(async () => undefined);
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("测试角色卡不应为空");
    }

    render(
      <CharacterCardPage
        activeCard={payload}
        cardsPayload={{ ...createCharacterCardsPayload(), cards: [payload.card] }}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onDeleteWakeResponseItem={onDeleteWakeResponseItem}
        onGenerateWakeResponse={vi.fn(async () => ({
          buckets: { morning: [], afternoon: [], evening: [], night: [] }
        }))}
        onSynthesizeWakeResponse={vi.fn(async (_cardId, itemId, _bucket, text) => ({ id: itemId, text }))}
        onSave={vi.fn()}
        onSaveWakeResponse={vi.fn(async (cardId, config) => {
          void cardId;
          return config;
        })}
        wakeResponseConfig={{
          buckets: {
            morning: [{ id: "m1", text: "早呀，我在", audio_url: "/api/character-cards/card-1/wake-response/audio/m1.wav" }],
            afternoon: [],
            evening: [],
            night: []
          }
        }}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "唤醒入口" }));
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(onDeleteWakeResponseItem).toHaveBeenCalledWith("card-1", "m1");
    });
  });

  it("语音绑定使用参考音频上传和直接 prompt 文本", async () => {
    const onSave = vi.fn(async () => undefined);
    const onUploadVoiceReference = vi.fn(async (_cardId, _audioFile) => ({
      reference_audio_path: "voice/reference.wav",
      effect_message: "参考音频已上传，保存当前卡后生效"
    }));
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("测试角色卡不应为空");
    }
    payload.card.meta.voice_binding.reference_audio_path = null;
    payload.card.meta.voice_binding.audio_sha256 = "old-sha";

    render(
      <CharacterCardPage
        activeCard={{ ...payload, voice_match_status: "missing_assets" }}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={onSave}
        onUploadVoiceReference={onUploadVoiceReference}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));

    expect(screen.queryByLabelText("参考音频路径")).toBeNull();
    expect(screen.queryByLabelText("Prompt 文本路径")).toBeNull();
    expect(screen.getByLabelText("上传参考音频")).toBeTruthy();
    expect(screen.getByRole("button", { name: "选择参考音频" })).toBeTruthy();
    expect(screen.getByText("当前参考音频：未上传")).toBeTruthy();
    const streamVoxModelSelect = screen.getByLabelText("StreamVox 模型");
    expect(streamVoxModelSelect).toBeInstanceOf(HTMLSelectElement);
    fireEvent.change(streamVoxModelSelect, {
      target: { value: "s2-pro-4b-gguf" }
    });
    expect((screen.getByLabelText("Prompt 文本") as HTMLTextAreaElement).value).toBe("这是一段参考音频文本。");

    const audioFile = new File(["audio"], "yae-miko-reference-very-long-file-name-that-should-be-truncated.wav", {
      type: "audio/wav"
    });
    fireEvent.change(screen.getByLabelText("上传参考音频"), {
      target: {
        files: [audioFile]
      }
    });

    await waitFor(() => {
      expect(onUploadVoiceReference).toHaveBeenCalledWith("card-1", audioFile);
    });
    expect(screen.getByText("参考音频已上传，保存当前卡后生效")).toBeTruthy();
    expect(screen.getByText("当前参考音频：yae-miko-reference-very-long-file-...").getAttribute("title")).toBe(audioFile.name);
    expect(screen.queryByText("当前参考音频：voice/reference.wav")).toBeNull();
    expect(screen.queryByText("缺少注册音色所需资产")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          meta: expect.objectContaining({
            voice_binding: expect.objectContaining({
              streamvox_model: "s2-pro-4b-gguf",
              reference_audio_path: "voice/reference.wav",
              audio_sha256: null
            })
          })
        })
      );
    });
  });

  it("语音绑定可以测算字幕语速并回填校准结果", async () => {
    const calibratedPayload = createCharacterCardPayload();
    if (calibratedPayload.card === null) {
      throw new Error("测试角色卡不应为空");
    }
    calibratedPayload.card.meta.voice_binding.caption_timing = {
      ms_per_unit: 210,
      calibration_text: "我在这里，听见你说话了。",
      duration_ms: 8400,
      unit_count: 40,
      measured_at: 1710000000
    };
    const onCalibrateVoiceCaption = vi.fn(async () => calibratedPayload);

    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onCalibrateVoiceCaption={onCalibrateVoiceCaption}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "语音绑定" }));
    fireEvent.click(screen.getByRole("button", { name: "测算字幕语速" }));

    await waitFor(() => {
      expect(onCalibrateVoiceCaption).toHaveBeenCalledWith("card-1");
    });
    expect(screen.getByText("每单位 210ms · 音频 8.4s")).toBeTruthy();
    expect(screen.getByText("字幕语速已测算")).toBeTruthy();
  });

  it("父级刷新当前激活卡时不会把正在编辑的非激活卡切回激活卡", async () => {
    const activePayload = createCharacterCardPayload();
    const cardsPayload = createCharacterCardsPayloadWithYaeMiko();
    const { rerender } = render(
      <CharacterCardPage
        activeCard={activePayload}
        cardsPayload={cardsPayload}
        isLoading={false}
        onActivate={vi.fn()}
        onApplyVoice={vi.fn(async () => undefined)}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /八重神子/ }));
    expect(screen.getByRole("heading", { name: "八重神子" })).toBeTruthy();

    rerender(
      <CharacterCardPage
        activeCard={{ ...activePayload, voice_match_status: "matched", effect_message: "角色语音已应用" }}
        cardsPayload={cardsPayload}
        isLoading={false}
        onActivate={vi.fn()}
        onApplyVoice={vi.fn(async () => undefined)}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    expect(screen.getByRole("heading", { name: "八重神子" })).toBeTruthy();
    expect(screen.queryByRole("heading", { name: "火花" })).toBeNull();
  });

  it("保存时会忽略只填写一侧的高光对白打样", async () => {
    const onSave = vi.fn(async () => undefined);
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("测试角色卡不应为空");
    }

    render(
      <CharacterCardPage
        activeCard={{
          ...payload,
          card: {
            ...payload.card,
            voiceprint: {
              ...payload.card.voiceprint,
              example_dialogues: { items: [] }
            }
          }
        }}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "声纹与台词" }));
    fireEvent.change(screen.getByLabelText("示例用户 1"), {
      target: {
        value: "只有用户说话"
      }
    });
    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          voiceprint: expect.objectContaining({
            example_dialogues: { items: [] }
          })
        })
      );
    });
  });

  it("模型示例生成只回填草稿，保存当前卡后才写入角色卡", async () => {
    const onSave = vi.fn(async () => undefined);
    const onGenerateStreamVoxExamples = vi.fn(async (): Promise<CharacterCardStreamVoxExampleSet> => ({
      items: [
        {
          title: "情绪承接",
          user: "我今天好累",
          char: "[sigh]听着就累，小可爱，先坐下。",
          expected_tool: "analyze_deeply"
        },
        {
          title: "现场感知",
          user: "世界杯踢到第几强了？",
          char: "妾身确认一下。",
          expected_tool: "lookup_current_fact"
        }
      ]
    }));

    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onGenerateStreamVoxExamples={onGenerateStreamVoxExamples}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "模型示例" }));
    fireEvent.change(screen.getByLabelText("示例目标模型"), {
      target: { value: "s2-pro-4b-gguf" }
    });
    fireEvent.click(screen.getByRole("button", { name: "生成模型示例" }));

    await waitFor(() => {
      expect(onGenerateStreamVoxExamples).toHaveBeenCalledWith(
        "card-1",
        "s2-pro-4b-gguf",
        expect.objectContaining({ meta: expect.objectContaining({ card_id: "card-1" }) })
      );
    });
    expect(onSave).not.toHaveBeenCalled();
    expect(((await screen.findByLabelText("示例标题 1")) as HTMLInputElement).value).toBe("情绪承接");
    expect((screen.getByLabelText("示例角色 1") as HTMLTextAreaElement).value).toBe("[sigh]听着就累，小可爱，先坐下。");
    expect((screen.getByLabelText("预期工具 1") as HTMLSelectElement).value).toBe("analyze_deeply");
    expect((screen.getByLabelText("预期工具 2") as HTMLSelectElement).value).toBe("lookup_current_fact");
    expect(screen.queryByLabelText("隐藏任务说明 2")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "保存当前卡" }));

    await waitFor(() => {
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({
          voiceprint: expect.objectContaining({
            streamvox_examples: expect.objectContaining({
              models: expect.objectContaining({
                "s2-pro-4b-gguf": {
                  items: [
                    {
                      title: "情绪承接",
                      user: "我今天好累",
                      char: "[sigh]听着就累，小可爱，先坐下。",
                      expected_tool: "analyze_deeply"
                    },
                    {
                      title: "现场感知",
                      user: "世界杯踢到第几强了？",
                      char: "妾身确认一下。",
                      expected_tool: "lookup_current_fact"
                    }
                  ]
                }
              })
            })
          })
        })
      );
    });
  });

  it("shared_world_canon 只读显示，且不展示迁移状态", async () => {
    render(
      <CharacterCardPage
        activeCard={{
          ...createCharacterCardPayload(),
          shared_world_canon: "shared world canon"
        }}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={vi.fn()}
        onSave={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    expect(screen.getByText("shared world canon")).toBeTruthy();
    expect(screen.queryByText("需要确认迁移")).toBeNull();
    expect(screen.queryByLabelText("共享世界观")).toBeNull();
  });

  it("可以复制当前卡", async () => {
    const onClone = vi.fn(async () => undefined);

    render(
      <CharacterCardPage
        activeCard={createCharacterCardPayload()}
        cardsPayload={createCharacterCardsPayload()}
        isLoading={false}
        onActivate={vi.fn()}
        onClone={onClone}
        onSave={vi.fn()}
      />
    );

    fireEvent.click(screen.getByRole("button", { name: /火花/ }));
    fireEvent.click(screen.getByRole("button", { name: "复制当前卡" }));

    await waitFor(() => {
      expect(onClone).toHaveBeenCalledWith({
        source_card_id: "card-1",
        card_id: "card-1_copy",
        name: "火花",
        title: "温柔而直接的伴侣"
      });
    });
  });
});
