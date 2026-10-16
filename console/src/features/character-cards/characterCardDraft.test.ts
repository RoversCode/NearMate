import { describe, expect, it } from "vitest";

import { createCharacterCardPayload, createCharacterCardsPayload } from "../../test/characterCardFixtures";
import {
  buildCloneCardId,
  mergeCharacterCardDraft,
  prepareCharacterCardForSave,
  shouldSyncDraftFromActivePayload
} from "./characterCardDraft";

describe("character card draft helpers", () => {
  it("合并资产刷新时保留脏列表并接纳未编辑的封面和语音摘要", () => {
    const baseline = createCharacterCardPayload().card!;
    const draft = { ...baseline, inner_core: { ...baseline.inner_core, values: ["本地草稿"] } };
    const incoming = createCharacterCardPayload().card!;
    incoming.meta.avatar_path = "assets/new-cover.jpg";
    incoming.meta.voice_binding.audio_sha256 = "new-audio-hash";
    incoming.inner_core.values = ["服务端列表"];

    const merged = mergeCharacterCardDraft(draft, baseline, incoming);
    expect(merged.inner_core.values).toEqual(["本地草稿"]);
    expect(merged.meta.avatar_path).toBe("assets/new-cover.jpg");
    expect(merged.meta.voice_binding.audio_sha256).toBe("new-audio-hash");
    expect(baseline.meta.avatar_path).toBeNull();
  });

  it("合并字典不把已删除参数变成 undefined 条目", () => {
    expect(mergeCharacterCardDraft({ a: 3, b: 2 }, { a: 1, b: 2 }, { a: 1 })).toEqual({ a: 3 });
    expect(mergeCharacterCardDraft({ a: 1 }, { a: 1, b: 2 }, { a: 3, b: 4 })).toEqual({ a: 3 });
  });

  it("保存前会过滤空白列表项并规范化模型示例", () => {
    const payload = createCharacterCardPayload();
    if (payload.card === null) {
      throw new Error("测试角色卡不应为空");
    }

    const preparedCard = prepareCharacterCardForSave({
      ...payload.card,
      inner_core: {
        ...payload.card.inner_core,
        values: [" 温柔 ", "", "清醒"]
      },
      voiceprint: {
        ...payload.card.voiceprint,
        streamvox_examples: {
          generic: {
            items: [
              {
                title: " 问候 ",
                user: " 你好 ",
                char: " 你好呀 ",
                expected_tool: "start_background_work"
              },
              {
                title: " 看图 ",
                user: " 这张截图是什么 ",
                char: " 这是设置界面 ",
                expected_tool: null
              },
              { title: "", user: "缺标题", char: "应丢弃", expected_tool: null }
            ]
          },
          models: {}
        }
      }
    });

    expect(preparedCard.inner_core.values).toEqual(["温柔", "清醒"]);
    expect(preparedCard.voiceprint.streamvox_examples.generic.items).toEqual([
      { title: "问候", user: "你好", char: "你好呀", expected_tool: "start_background_work" },
      { title: "看图", user: "这张截图是什么", char: "这是设置界面", expected_tool: null }
    ]);
  });

  it("只有草稿仍对应当前激活卡时才同步父级刷新", () => {
    const activePayload = createCharacterCardPayload();
    const draftCard = activePayload.card;
    if (draftCard === null) {
      throw new Error("测试角色卡不应为空");
    }

    expect(shouldSyncDraftFromActivePayload(draftCard, activePayload)).toBe(true);
    expect(
      shouldSyncDraftFromActivePayload(
        {
          ...draftCard,
          meta: {
            ...draftCard.meta,
            card_id: "other-card"
          }
        },
        activePayload
      )
    ).toBe(false);
  });

  it("复制角色卡时会生成不冲突的编号", () => {
    const cardsPayload = createCharacterCardsPayload();

    expect(buildCloneCardId("yae-miko", cardsPayload.cards)).toBe("yae-miko_copy");
    expect(buildCloneCardId("xiaolian", cardsPayload.cards)).toBe("xiaolian_copy");
  });
});
