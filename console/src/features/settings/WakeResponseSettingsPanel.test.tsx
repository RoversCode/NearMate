import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { WakeResponseSettingsPanel } from "./WakeResponseSettingsPanel";
import type { WakeResponseConfig, WakeResponseItem } from "../../api/contracts/wakeResponse";

const CARD_ID = "card-1";

function createConfig(): WakeResponseConfig {
  return {
    buckets: {
      morning: [
        {
          id: "morning-1",
          text: "早呀，我在",
          audio_path: "G:/wake-response/audio/morning-1.wav",
          audio_url: "/api/character-cards/card-1/wake-response/audio/morning-1.wav",
          synthesized_at: 1
        }
      ],
      afternoon: [],
      evening: [],
      night: []
    }
  };
}

describe("WakeResponseSettingsPanel", () => {
  it("按四个时段展示回复文本，并支持生成、试听、删除和保存", async () => {
    const onGenerate = vi.fn().mockResolvedValue({
      buckets: {
        morning: ["醒啦，我听着"],
        afternoon: ["下午我在"],
        evening: ["晚上好"],
        night: ["夜里我也在"]
      }
    });
    const onSave = vi.fn().mockResolvedValue(createConfig());
    const onSynthesize = vi.fn().mockResolvedValue(createConfig());
    const onDeleteSavedItem = vi.fn().mockResolvedValue(createConfig());
    const audioPlay = vi.fn().mockResolvedValue(undefined);
    const originalAudio = globalThis.Audio;
    globalThis.Audio = vi.fn().mockImplementation(() => ({
      addEventListener: vi.fn(),
      play: audioPlay
    })) as unknown as typeof Audio;

    try {
      render(
        <WakeResponseSettingsPanel
          cardId={CARD_ID}
          config={createConfig()}
          isBusy={false}
          onGenerate={onGenerate}
          onDeleteSavedItem={onDeleteSavedItem}
          onSynthesize={onSynthesize}
          onSave={onSave}
        />
      );

      expect(screen.getByText("早晨")).toBeTruthy();
      expect(screen.getByText("午后")).toBeTruthy();
      expect(screen.getByDisplayValue("早呀，我在")).toBeTruthy();

      fireEvent.click(screen.getAllByText("试听")[0]);
      await waitFor(() =>
        expect(globalThis.Audio).toHaveBeenCalledWith(
          "/api/character-cards/card-1/wake-response/audio/morning-1.wav"
        )
      );
      expect(audioPlay).toHaveBeenCalledOnce();

      fireEvent.click(screen.getByText("生成唤醒回复"));
      await waitFor(() => expect(onGenerate).toHaveBeenCalledOnce());
      expect(await screen.findByDisplayValue("醒啦，我听着")).toBeTruthy();
      expect((screen.getAllByText("合成后可试听")[0] as HTMLButtonElement).disabled).toBe(true);

      fireEvent.click(screen.getAllByText("删除")[1]);
      expect(screen.queryByDisplayValue("醒啦，我听着")).toBeNull();

      fireEvent.click(screen.getByText("保存"));
      await waitFor(() => expect(onSave).toHaveBeenCalled());
    } finally {
      globalThis.Audio = originalAudio;
    }
  });

  it("编辑已合成文本后清除音频引用，并允许直接合成", () => {
    const onGenerate = vi.fn();
    const onSave = vi.fn();
    const onSynthesize = vi.fn();
    const onDeleteSavedItem = vi.fn();

    render(
      <WakeResponseSettingsPanel
        cardId={CARD_ID}
        config={createConfig()}
        isBusy={false}
        onGenerate={onGenerate}
        onDeleteSavedItem={onDeleteSavedItem}
        onSynthesize={onSynthesize}
        onSave={onSave}
      />
    );

    fireEvent.change(screen.getByLabelText("早晨回复 1"), { target: { value: "早呀，新句子" } });

    expect(screen.getByDisplayValue("早呀，新句子")).toBeTruthy();
    expect((screen.getByText("合成后可试听") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getAllByText("合成")[0] as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("待合成")).toBeTruthy();
    expect(screen.getByText("合成后可试听")).toBeTruthy();
  });

  it("新增短句后展示待合成状态，并在保存时保留未改动条目的音频路径", async () => {
    const onGenerate = vi.fn();
    const onSave = vi.fn().mockResolvedValue(createConfig());
    const onSynthesize = vi.fn();
    const onDeleteSavedItem = vi.fn();

    render(
      <WakeResponseSettingsPanel
        cardId={CARD_ID}
        config={createConfig()}
        isBusy={false}
        onGenerate={onGenerate}
        onDeleteSavedItem={onDeleteSavedItem}
        onSynthesize={onSynthesize}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getAllByText("新增回复")[0]);
    fireEvent.change(screen.getByLabelText("早晨回复 2"), { target: { value: "醒啦，我听着" } });

    expect(screen.getByText("待合成")).toBeTruthy();
    expect(screen.getByText("合成后可试听")).toBeTruthy();
    expect((screen.getByText("合成后可试听") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getAllByText("合成")[1] as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByText("保存"));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        CARD_ID,
        {
          buckets: {
            morning: [
              {
                id: "morning-1",
                text: "早呀，我在",
                audio_path: "G:/wake-response/audio/morning-1.wav",
                audio_url: "/api/character-cards/card-1/wake-response/audio/morning-1.wav",
                synthesized_at: 1
              },
              {
                id: expect.any(String),
                text: "醒啦，我听着"
              }
            ],
            afternoon: [],
            evening: [],
            night: []
          }
        }
      )
    );
  });

  it("单行合成只更新当前行，并保留其他行未保存修改", async () => {
    const onSynthesize = vi.fn(async (_cardId: string, itemId: string, _bucket: string, text: string) => ({
      id: itemId,
      text,
      audio_url: "/audio/updated.wav"
    }));
    render(
      <WakeResponseSettingsPanel
        cardId={CARD_ID}
        config={{
          buckets: {
            morning: [{ id: "morning-1", text: "早呀，我在", audio_url: "/audio/morning.wav" }],
            afternoon: [],
            evening: [{ id: "evening-1", text: "晚上好", audio_url: "/audio/evening.wav" }],
            night: []
          }
        }}
        isBusy={false}
        onGenerate={vi.fn()}
        onDeleteSavedItem={vi.fn()}
        onSynthesize={onSynthesize}
        onSave={vi.fn()}
      />
    );

    fireEvent.change(screen.getByLabelText("晚上回复 1"), { target: { value: "晚上新回复" } });
    fireEvent.click(screen.getAllByText("合成")[0]);

    await waitFor(() => expect(onSynthesize).toHaveBeenCalledWith(CARD_ID, "morning-1", "morning", "早呀，我在"));
    expect(screen.getByDisplayValue("晚上新回复")).toBeTruthy();
    expect(screen.getByDisplayValue("早呀，我在")).toBeTruthy();
    expect(screen.getAllByText("试听")[0]).toBeTruthy();
  });

  it("父级配置刷新时只接管干净条目，保留脏编辑", () => {
    const initialConfig = {
      buckets: {
        morning: [{ id: "morning-1", text: "早呀，我在", audio_url: "/audio/morning.wav" }],
        afternoon: [],
        evening: [{ id: "evening-1", text: "晚上好", audio_url: "/audio/evening.wav" }],
        night: []
      }
    };
    const createProps = (config: typeof initialConfig) => ({
      cardId: CARD_ID,
      config,
      isBusy: false,
      onGenerate: vi.fn(),
      onDeleteSavedItem: vi.fn(),
      onSynthesize: vi.fn(),
      onSave: vi.fn()
    });
    const { rerender } = render(<WakeResponseSettingsPanel {...createProps(initialConfig)} />);
    fireEvent.change(screen.getByLabelText("晚上回复 1"), { target: { value: "晚上未保存修改" } });
    rerender(
      <WakeResponseSettingsPanel
        {...createProps({
          buckets: {
            morning: [{ id: "morning-1", text: "早呀，新后端文本", audio_url: "/audio/morning-2.wav" }],
            afternoon: [],
            evening: [{ id: "evening-1", text: "晚上好", audio_url: "/audio/evening.wav" }],
            night: []
          }
        })}
      />
    );
    expect(screen.getByDisplayValue("早呀，新后端文本")).toBeTruthy();
    expect(screen.getByDisplayValue("晚上未保存修改")).toBeTruthy();
  });

  it("切换角色卡时即使新角色没有条目也会重置草稿", () => {
    const emptyConfig: WakeResponseConfig = {
      buckets: { morning: [], afternoon: [], evening: [], night: [] }
    };
    const props = {
      cardId: CARD_ID,
      config: createConfig(),
      isBusy: false,
      onGenerate: vi.fn(),
      onDeleteSavedItem: vi.fn(),
      onSynthesize: vi.fn(),
      onSave: vi.fn()
    };
    const { rerender } = render(<WakeResponseSettingsPanel {...props} />);
    rerender(<WakeResponseSettingsPanel {...props} cardId="card-2" config={emptyConfig} />);
    expect(screen.queryByDisplayValue("早呀，我在")).toBeNull();
  });

  it("生成唤醒回复时追加候选短句，并保留已有已合成条目的音频引用", async () => {
    const onGenerate = vi.fn().mockResolvedValue({
      buckets: {
        morning: ["醒啦，我听着"],
        afternoon: ["下午我在"],
        evening: [],
        night: []
      }
    });
    const onSave = vi.fn().mockResolvedValue(createConfig());
    const onSynthesize = vi.fn();
    const onDeleteSavedItem = vi.fn();

    render(
      <WakeResponseSettingsPanel
        cardId={CARD_ID}
        config={createConfig()}
        isBusy={false}
        onGenerate={onGenerate}
        onDeleteSavedItem={onDeleteSavedItem}
        onSynthesize={onSynthesize}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByText("生成唤醒回复"));

    expect(await screen.findByDisplayValue("醒啦，我听着")).toBeTruthy();
    expect(screen.getByDisplayValue("早呀，我在")).toBeTruthy();
    expect((screen.getAllByText("试听")[0] as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getAllByText("合成")[0] as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getAllByText("合成后可试听")[0] as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getAllByText("待合成")).toHaveLength(2);

    fireEvent.click(screen.getByText("保存"));

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(CARD_ID, {
        buckets: {
          morning: [
            {
              id: "morning-1",
              text: "早呀，我在",
              audio_path: "G:/wake-response/audio/morning-1.wav",
              audio_url: "/api/character-cards/card-1/wake-response/audio/morning-1.wav",
              synthesized_at: 1
            },
            {
              id: expect.any(String),
              text: "醒啦，我听着"
            }
          ],
          afternoon: [
            {
              id: expect.any(String),
              text: "下午我在"
            }
          ],
          evening: [],
          night: []
        }
      })
    );
  });

  it("删除已保存条目时立即调用后端删除，不提交整份草稿", async () => {
    const onGenerate = vi.fn();
    const onSave = vi.fn();
    const onSynthesize = vi.fn();
    const onDeleteSavedItem = vi.fn().mockResolvedValue({
      buckets: {
        morning: [],
        afternoon: [],
        evening: [],
        night: []
      }
    });

    render(
      <WakeResponseSettingsPanel
        cardId={CARD_ID}
        config={createConfig()}
        isBusy={false}
        onGenerate={onGenerate}
        onDeleteSavedItem={onDeleteSavedItem}
        onSynthesize={onSynthesize}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getAllByText("删除")[0]);

    await waitFor(() => expect(onDeleteSavedItem).toHaveBeenCalledWith(CARD_ID, "morning-1"));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.queryByDisplayValue("早呀，我在")).toBeNull();
  });

  it("删除后收到旧配置刷新时不会把已删除条目补回来", async () => {
    const initialConfig = createConfig();
    const onDeleteSavedItem = vi.fn().mockResolvedValue(undefined);
    const props = {
      cardId: CARD_ID,
      config: initialConfig,
      isBusy: false,
      onGenerate: vi.fn(),
      onDeleteSavedItem,
      onSynthesize: vi.fn(),
      onSave: vi.fn()
    };
    const { rerender } = render(<WakeResponseSettingsPanel {...props} />);

    fireEvent.click(screen.getAllByText("删除")[0]);
    await waitFor(() => expect(screen.queryByDisplayValue("早呀，我在")).toBeNull());

    rerender(<WakeResponseSettingsPanel {...props} config={createConfig()} />);
    expect(screen.queryByDisplayValue("早呀，我在")).toBeNull();
  });

  it("单行合成期间锁定目标输入框，其他行仍可编辑", async () => {
    let resolveSynthesis!: (item: WakeResponseItem) => void;
    const onSynthesize = vi.fn(
      () => new Promise<WakeResponseItem>((resolve) => { resolveSynthesis = resolve; })
    );
    render(
      <WakeResponseSettingsPanel
        cardId={CARD_ID}
        config={{
          buckets: {
            morning: [{ id: "morning-1", text: "早呀，我在", audio_url: "/audio/morning.wav" }],
            afternoon: [],
            evening: [{ id: "evening-1", text: "晚上好", audio_url: "/audio/evening.wav" }],
            night: []
          }
        }}
        isBusy={false}
        onGenerate={vi.fn()}
        onDeleteSavedItem={vi.fn()}
        onSynthesize={onSynthesize}
        onSave={vi.fn()}
      />
    );

    fireEvent.click(screen.getAllByText("合成")[0]);
    await waitFor(() => expect(onSynthesize).toHaveBeenCalledOnce());
    expect(screen.getByLabelText("早晨回复 1")).toHaveProperty("disabled", true);
    expect(screen.getByLabelText("晚上回复 1")).toHaveProperty("disabled", false);
    await act(async () => {
      resolveSynthesis({ id: "morning-1", text: "早呀，我在", audio_url: "/audio/new.wav" });
    });
  });

  it("删除未保存草稿时只更新本地草稿，不调用后端删除", async () => {
    const onGenerate = vi.fn().mockResolvedValue({
      buckets: {
        morning: ["醒啦，我听着"],
        afternoon: [],
        evening: [],
        night: []
      }
    });
    const onSave = vi.fn();
    const onSynthesize = vi.fn();
    const onDeleteSavedItem = vi.fn();

    render(
      <WakeResponseSettingsPanel
        cardId={CARD_ID}
        config={createConfig()}
        isBusy={false}
        onGenerate={onGenerate}
        onDeleteSavedItem={onDeleteSavedItem}
        onSynthesize={onSynthesize}
        onSave={onSave}
      />
    );

    fireEvent.click(screen.getByText("生成唤醒回复"));
    expect(await screen.findByDisplayValue("醒啦，我听着")).toBeTruthy();
    fireEvent.click(screen.getAllByText("删除")[1]);

    expect(onDeleteSavedItem).not.toHaveBeenCalled();
    expect(screen.queryByDisplayValue("醒啦，我听着")).toBeNull();
  });
});
