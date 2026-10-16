import { type FormEvent, useEffect, useRef, useState } from "react";

import { EmptyState } from "@/components/layout/EmptyState";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import type {
  CharacterCard,
  CharacterCardClonePayload,
  CharacterCardPayload,
  CharacterCardStreamVoxExampleSet,
  CharacterCardsPayload,
  CharacterCardVoiceReferenceUploadResult
} from "../../api/contracts/characterCards";
import type {
  WakeResponseConfig,
  WakeResponseGeneratedTexts,
  WakeResponseItem
} from "../../api/contracts/wakeResponse";
import { CoverCropperDialog } from "./CoverCropperDialog";
import {
  type CoverImagePreview,
  createImagePreview,
  validateCoverSourceFile
} from "./coverImageProcessing";
import { CharacterCardGallery } from "./CharacterCardGallery";
import { CoverPanel, PreviewPanel, ValidationPanel } from "./CharacterCardPanels";
import {
  BackgroundContextSectionEditor,
  CHARACTER_CARD_SECTIONS,
  type SectionId,
  IdentitySectionEditor,
  InnerCoreSectionEditor,
  ModelConstraintsSectionEditor,
  RelationshipsSectionEditor,
  VoiceprintSectionEditor,
  WakeEntrySectionEditor
} from "./CharacterCardSections";
import { StreamVoxExamplesSectionEditor } from "./CharacterCardStreamVoxExamplesSection";
import {
  type BackgroundContextRow,
  type ExampleDialogueRow,
  type ListFieldPath,
  type RelationshipRow,
  type StreamVoxExampleRow,
  type TextFieldPath,
  buildCloneCardId,
  cloneCard,
  mergeCharacterCardDraft,
  prepareCharacterCardForSave,
  shouldSyncDraftFromActivePayload,
  updateBackgroundContextTermsField,
  updateEmotionGradingField,
  updateExampleDialoguesField,
  updateListField,
  updateRelationshipsField,
  updateStreamVoxExamplesField,
  updateTextField,
  updateVoiceInferenceOptionsField,
  updateVoiceReferenceAudioDraft,
  updateWakewordKeywordsDraft
} from "./characterCardDraft";
import { VoiceBindingSectionEditor } from "./CharacterCardVoiceBindingSection";
import { revokeCoverObjectUrl } from "./characterCardCoverUrl";

type CharacterCardPageProps = {
  cardsPayload: CharacterCardsPayload | null;
  activeCard: CharacterCardPayload | null;
  characterAuthoringEnabled?: boolean;
  isLoading: boolean;
  errorMessage?: string | null;
  warningMessage?: string | null;
  isSaving?: boolean;
  onSelectCard?: (cardId: string) => Promise<void>;
  onSave: (card: CharacterCard) => Promise<CharacterCardPayload | null | void>;
  onActivate: (cardId: string) => Promise<void>;
  onApplyVoice?: (cardId: string) => Promise<void>;
  onCalibrateVoiceCaption?: (cardId: string) => Promise<CharacterCardPayload>;
  onClone: (payload: CharacterCardClonePayload) => Promise<void>;
  onUploadCover?: (cardId: string, imageFile: File) => Promise<void>;
  onDeleteCover?: (cardId: string) => Promise<void>;
  onUploadVoiceReference?: (cardId: string, audioFile: File) => Promise<CharacterCardVoiceReferenceUploadResult>;
  onGenerateStreamVoxExamples?: (
    cardId: string,
    targetModel: string,
    card: CharacterCard
  ) => Promise<CharacterCardStreamVoxExampleSet>;
  wakeResponseConfig?: WakeResponseConfig;
  onGenerateWakeResponse?: (cardId: string) => Promise<WakeResponseGeneratedTexts>;
  onDeleteWakeResponseItem?: (cardId: string, itemId: string) => Promise<void>;
  onSynthesizeWakeResponse?: (cardId: string, itemId: string, bucket: string, text: string) => Promise<WakeResponseItem>;
  onSaveWakeResponse?: (cardId: string, config: WakeResponseConfig) => Promise<WakeResponseConfig>;
};

type ViewMode = "gallery" | "detail";

/**
 * 渲染角色卡控制台页面容器。
 *
 * Args:
 *   cardsPayload: 角色卡列表、当前激活卡编号和共享世界观。
 *   activeCard: 当前正在编辑的角色卡响应。
 *   isLoading: 当前是否正在读取角色卡数据。
 *   errorMessage: 最近一次角色卡操作错误。
 *   warningMessage: 写入成功但附属状态读取失败时的提醒。
 *   isSaving: 当前是否存在保存、激活或复制操作。
 *   onSelectCard: 选择卡槽后读取对应角色卡。
 *   onSave: 保存当前编辑卡的完整内容。
 *   onActivate: 激活当前卡。
 *   onClone: 复制当前卡。
 *
 * Returns:
 *   角色卡图库、编辑区、预览区和校验区。
 */
export function CharacterCardPage({
  cardsPayload,
  activeCard,
  characterAuthoringEnabled = true,
  isLoading,
  errorMessage = null,
  warningMessage = null,
  isSaving = false,
  onSelectCard,
  onSave,
  onActivate,
  onApplyVoice,
  onCalibrateVoiceCaption,
  onClone,
  onUploadCover,
  onDeleteCover,
  onUploadVoiceReference,
  onGenerateStreamVoxExamples,
  wakeResponseConfig,
  onGenerateWakeResponse,
  onDeleteWakeResponseItem,
  onSynthesizeWakeResponse,
  onSaveWakeResponse
}: CharacterCardPageProps): JSX.Element {
  const [draftCard, setDraftCard] = useState<CharacterCard | null>(activeCard?.card ?? null);
  const savedDraftRef = useRef<CharacterCard | null>(activeCard?.card ?? null);
  const submittedDraftRef = useRef<CharacterCard | null>(null);
  const draftSessionRef = useRef(0);
  const [activeSectionId, setActiveSectionId] = useState<SectionId>("identity");
  const [pendingCard, setPendingCard] = useState<CharacterCard | null>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("gallery");
  const [coverDraft, setCoverDraft] = useState<CoverImagePreview | null>(null);
  const [coverDraftCardId, setCoverDraftCardId] = useState<string | null>(null);
  const [coverLocalError, setCoverLocalError] = useState<string | null>(null);
  const [voiceReferenceUploadMessage, setVoiceReferenceUploadMessage] = useState<string | null>(null);
  const [voiceReferenceDisplayName, setVoiceReferenceDisplayName] = useState<string | null>(null);
  const [voiceCaptionCalibrationMessage, setVoiceCaptionCalibrationMessage] = useState<string | null>(null);
  const [hideVoiceStatusUntilSave, setHideVoiceStatusUntilSave] = useState(false);
  const [streamVoxExampleTargetModel, setStreamVoxExampleTargetModel] = useState("generic");
  const [streamVoxExampleMessage, setStreamVoxExampleMessage] = useState<string | null>(null);

  useEffect(() => {
    const incomingCard = activeCard?.card ?? null;
    if (!shouldSyncDraftFromActivePayload(savedDraftRef.current, activeCard)) return;
    // 保存响应可能先于保存回调到达；用提交时草稿识别期间的新输入，包括改回旧值。
    const previousSavedCard = submittedDraftRef.current ?? savedDraftRef.current;
    savedDraftRef.current = incomingCard;
    setDraftCard((currentCard) => {
      if (!shouldSyncDraftFromActivePayload(currentCard, activeCard)) {
        return currentCard;
      }

      // 同一卡的封面或语音更新不能覆盖人格草稿，服务端只接管未编辑字段。
      if (currentCard === null || previousSavedCard === null || incomingCard === null) return incomingCard;
      if (JSON.stringify(currentCard) === JSON.stringify(previousSavedCard)) {
        clearVoiceDraftMessages();
        setStreamVoxExampleMessage(null);
      }
      return mergeCharacterCardDraft(currentCard, previousSavedCard, incomingCard);
    });
  }, [activeCard]);

  useEffect(() => () => { draftSessionRef.current += 1; }, []);

  useEffect(() => {
    return () => {
      if (coverDraft !== null) {
        revokeCoverObjectUrl(coverDraft.objectUrl);
      }
    };
  }, [coverDraft]);

  if (isLoading && activeCard === null) {
    return (
      <section className="space-y-6">
        <PageHeader title="角色卡" description="正在读取角色卡列表和当前激活角色卡。" />
        <EmptyState>角色卡尚未可用。</EmptyState>
      </section>
    );
  }

  if (errorMessage !== null && (cardsPayload === null || activeCard === null)) {
    return (
      <section className="space-y-6">
        <PageHeader title="角色卡" description="管理新会话生效的角色人格、羁绊和表达方式。" />
        <EmptyState>
          <p className="font-medium text-destructive">角色卡加载失败</p>
          <p>{errorMessage}</p>
        </EmptyState>
      </section>
    );
  }

  if (cardsPayload === null || activeCard === null || draftCard === null) {
    return (
      <section className="space-y-6">
        <PageHeader title="角色卡" description="管理新会话生效的角色人格、羁绊和表达方式。" />
        <EmptyState>没有可编辑的角色卡。</EmptyState>
      </section>
    );
  }

  async function handleSave(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();

    if (draftCard === null || isSaving || submittedDraftRef.current !== null) {
      return;
    }

    const submittedDraft = draftCard;
    const draftSession = draftSessionRef.current;
    submittedDraftRef.current = submittedDraft;
    try {
      const savedPayload = await onSave(prepareCharacterCardForSave(submittedDraft));
      const savedCard = savedPayload?.card;
      if (!savedCard || draftSession !== draftSessionRef.current) return;
      savedDraftRef.current = savedCard;
      // 提交后的新输入继续留在草稿；已提交字段接受服务端的裁剪、资产摘要等规范化结果。
      setDraftCard(currentCard => currentCard?.meta.card_id === savedCard.meta.card_id
        ? mergeCharacterCardDraft(currentCard, submittedDraft, savedCard) : currentCard);
    } finally {
      submittedDraftRef.current = null;
    }
  }

  function handleTextChanged(path: TextFieldPath, value: string): void {
    if (path.startsWith("meta.voice_binding.")) {
      clearVoiceDraftMessages();
    }
    setDraftCard((currentCard) => (currentCard === null ? currentCard : updateTextField(currentCard, path, value)));
  }

  function handleListChanged(path: ListFieldPath, value: string): void {
    setDraftCard((currentCard) => (currentCard === null ? currentCard : updateListField(currentCard, path, value)));
  }

  function handleWakewordKeywordsChanged(keywords: string[]): void {
    setDraftCard((currentCard) => (currentCard === null ? currentCard : updateWakewordKeywordsDraft(currentCard, keywords)));
  }

  function handleBackgroundContextChanged(nextTerms: BackgroundContextRow[]): void {
    setDraftCard((currentCard) => (currentCard === null ? currentCard : updateBackgroundContextTermsField(currentCard, nextTerms)));
  }

  function handleRelationshipsChanged(nextRelationships: RelationshipRow[]): void {
    setDraftCard((currentCard) =>
      currentCard === null ? currentCard : updateRelationshipsField(currentCard, nextRelationships)
    );
  }

  function handleExamplesChanged(nextExamples: ExampleDialogueRow[]): void {
    setDraftCard((currentCard) => (currentCard === null ? currentCard : updateExampleDialoguesField(currentCard, nextExamples)));
  }

  function handleStreamVoxExamplesChanged(targetModel: string, nextExamples: StreamVoxExampleRow[]): void {
    setDraftCard((currentCard) =>
      currentCard === null ? currentCard : updateStreamVoxExamplesField(currentCard, targetModel, { items: nextExamples })
    );
  }

  async function handleGenerateStreamVoxExamples(): Promise<void> {
    if (draftCard === null || onGenerateStreamVoxExamples === undefined || isSaving) {
      return;
    }

    const cardId = draftCard.meta.card_id;
    const draftSession = draftSessionRef.current;
    const generatedExamples = await onGenerateStreamVoxExamples(
      cardId,
      streamVoxExampleTargetModel,
      prepareCharacterCardForSave(draftCard)
    );
    if (draftSession !== draftSessionRef.current) return;
    setDraftCard((currentCard) =>
      currentCard?.meta.card_id === cardId ? updateStreamVoxExamplesField(currentCard, streamVoxExampleTargetModel, generatedExamples) : currentCard
    );
    setStreamVoxExampleMessage("模型示例已生成，保存当前卡后生效");
  }

  function handleEmotionGradingChanged(value: string): void {
    setDraftCard((currentCard) => (currentCard === null ? currentCard : updateEmotionGradingField(currentCard, value)));
  }

  function handleVoiceInferenceOptionsChanged(value: string): void {
    setVoiceCaptionCalibrationMessage(null);
    setDraftCard((currentCard) => (currentCard === null ? currentCard : updateVoiceInferenceOptionsField(currentCard, value)));
  }

  async function handleVoiceReferenceSelected(audioFile: File): Promise<void> {
    if (draftCard === null || onUploadVoiceReference === undefined) {
      return;
    }

    const cardId = draftCard.meta.card_id;
    const draftSession = draftSessionRef.current;
    const result = await onUploadVoiceReference(cardId, audioFile);
    if (draftSession !== draftSessionRef.current) return;
    setDraftCard((currentCard) =>
      currentCard?.meta.card_id === cardId ? updateVoiceReferenceAudioDraft(currentCard, result.reference_audio_path) : currentCard
    );
    setVoiceReferenceUploadMessage(result.effect_message);
    setVoiceReferenceDisplayName(audioFile.name);
    setVoiceCaptionCalibrationMessage(null);
    setHideVoiceStatusUntilSave(true);
  }

  async function handleVoiceCaptionCalibration(): Promise<void> {
    if (draftCard === null || onCalibrateVoiceCaption === undefined || isSaving) {
      return;
    }

    const previousSavedCard = savedDraftRef.current;
    const draftSession = draftSessionRef.current;
    const result = await onCalibrateVoiceCaption(draftCard.meta.card_id);
    if (draftSession !== draftSessionRef.current) return;
    if (result.card !== null && previousSavedCard !== null) {
      const calibratedCard = result.card;
      if (savedDraftRef.current?.meta.card_id !== calibratedCard.meta.card_id) return;
      savedDraftRef.current = calibratedCard;
      setDraftCard(currentCard => currentCard?.meta.card_id === calibratedCard.meta.card_id
        ? mergeCharacterCardDraft(currentCard, previousSavedCard, calibratedCard) : currentCard);
    }
    setVoiceReferenceUploadMessage(null);
    setVoiceCaptionCalibrationMessage(result.effect_message ?? "字幕语速已测算");
    setHideVoiceStatusUntilSave(false);
  }

  async function handleCoverSourceSelected(cardId: string, imageFile: File): Promise<void> {
    const validationMessage = validateCoverSourceFile(imageFile);
    if (validationMessage !== null) {
      closeCoverDraft();
      setCoverLocalError(validationMessage);
      return;
    }

    try {
      const preview = await createImagePreview(imageFile);
      closeCoverDraft();
      setCoverLocalError(null);
      setCoverDraft(preview);
      setCoverDraftCardId(cardId);
    } catch (error) {
      closeCoverDraft();
      setCoverLocalError(error instanceof Error ? error.message : "形象照片读取失败");
    }
  }

  async function handleCroppedCoverConfirmed(imageFile: File): Promise<void> {
    if (coverDraftCardId === null || onUploadCover === undefined) {
      return;
    }

    await onUploadCover(coverDraftCardId, imageFile);
    closeCoverDraft();
  }

  function closeCoverDraft(): void {
    setCoverDraft((currentDraft) => {
      if (currentDraft !== null) {
        revokeCoverObjectUrl(currentDraft.objectUrl);
      }
      return null;
    });
    setCoverDraftCardId(null);
  }

  function clearVoiceDraftMessages(): void {
    setVoiceReferenceUploadMessage(null);
    setVoiceReferenceDisplayName(null);
    setVoiceCaptionCalibrationMessage(null);
    setHideVoiceStatusUntilSave(false);
  }

  const savedCard = cardsPayload.cards.find(card => card.meta.card_id === draftCard.meta.card_id);
  const hasUnsavedChanges = savedCard !== undefined && JSON.stringify(draftCard) !== JSON.stringify(savedCard);

  function handleOpenCard(card: CharacterCard, discardCurrentDraft = false): void {
    if (card.meta.card_id !== draftCard?.meta.card_id && hasUnsavedChanges && !discardCurrentDraft) {
      setPendingCard(card);
      return;
    }
    setPendingCard(null);
    if (card.meta.card_id !== draftCard?.meta.card_id || discardCurrentDraft) draftSessionRef.current += 1;
    savedDraftRef.current = cardsPayload?.cards.find(saved => saved.meta.card_id === card.meta.card_id) ?? card;
    // 图库里的卡片已经是完整角色卡，直接进入详情能避免一次不必要的网络请求。
    setDraftCard(cloneCard(card));
    setActiveSectionId("identity");
    setViewMode("detail");
    clearVoiceDraftMessages();
    setStreamVoxExampleMessage(null);
    // 再次打开同一张脏卡时沿用草稿，避免服务端旧快照把未保存编辑覆盖。
    if (onSelectCard !== undefined && !(hasUnsavedChanges && card.meta.card_id === draftCard?.meta.card_id)) {
      void onSelectCard(card.meta.card_id);
    }
  }

  const activeSection = CHARACTER_CARD_SECTIONS.find((section) => section.id === activeSectionId) ?? CHARACTER_CARD_SECTIONS[0];
  const galleryCards = cardsPayload.cards.map((card) => (card.meta.card_id === draftCard.meta.card_id ? draftCard : card));
  const selectedCard = galleryCards.find((card) => card.meta.card_id === draftCard.meta.card_id) ?? draftCard;

  if (viewMode === "gallery") {
    return (
      <section className="space-y-6">
        <PageHeader title="角色卡图库" description="选择一张角色卡进入编辑；激活后会在新会话生效。" />
        {errorMessage !== null ? <p className="text-sm font-medium text-destructive">角色卡操作失败：{errorMessage}</p> : null}
        {warningMessage !== null ? <p className="text-sm text-warning" role="status">{warningMessage}</p> : null}
        {activeCard.effect_message !== null ? <p className="text-sm font-medium text-success">{activeCard.effect_message}</p> : null}
        {hasUnsavedChanges ? <p className="text-sm text-warning">{draftCard.meta.name} 有未保存的修改，重新打开可继续编辑。</p> : null}
        <Dialog open={pendingCard !== null} onOpenChange={open => { if (!open) setPendingCard(null); }}>
          <DialogContent>
            <DialogHeader><DialogTitle>当前角色卡有未保存修改</DialogTitle><DialogDescription>切换角色卡会丢弃这些修改。你可以返回编辑并保存。</DialogDescription></DialogHeader>
            <DialogFooter>
              <Button onClick={() => { setPendingCard(null); setViewMode("detail"); }} variant="outline">继续编辑</Button>
              <Button onClick={() => { if (pendingCard) handleOpenCard(pendingCard, true); }}>放弃修改并切换</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <CharacterCardGallery
          activeCardId={activeCard.active_card_id}
          cards={galleryCards}
          disabled={isSaving}
          selectedCardId={selectedCard.meta.card_id}
          onOpenCard={handleOpenCard}
        />
      </section>
    );
  }

  return (
    <section className="space-y-6">
      <div className="space-y-4">
        <Button onClick={() => setViewMode("gallery")} type="button" variant="outline">
          返回角色库
        </Button>
        <PageHeader title={draftCard.meta.name || "未命名角色"} description={draftCard.meta.title || "未填写卡片标题 / 人物画像"} />
      </div>
      {errorMessage !== null ? <p className="text-sm font-medium text-destructive">角色卡操作失败：{errorMessage}</p> : null}
      {warningMessage !== null ? <p className="text-sm text-warning" role="status">{warningMessage}</p> : null}
      {activeCard.effect_message !== null ? <p className="text-sm font-medium text-success">{activeCard.effect_message}</p> : null}
      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_320px]">
        <form className="min-w-0" onSubmit={(event) => void handleSave(event)}>
          <Card>
            <CardHeader>
              <CardTitle>{activeSection.label}</CardTitle>
              <CardDescription>{activeSection.description}</CardDescription>
            </CardHeader>
            <CardContent className="min-w-0 space-y-5">
              <div className="sticky top-3 z-20 flex flex-wrap items-center gap-2 rounded-lg border bg-card/95 p-3 shadow-sm backdrop-blur">
                <Button disabled={isSaving} type="submit">
                  {isSaving ? "保存中…" : "保存当前卡"}
                </Button>
                <Button disabled={isSaving || hasUnsavedChanges} onClick={() => void onActivate(draftCard.meta.card_id)} type="button" variant="outline">
                  激活卡
                </Button>
                <Button
                  disabled={isSaving || hasUnsavedChanges || onApplyVoice === undefined}
                  onClick={() => void onApplyVoice?.(draftCard.meta.card_id)}
                  type="button"
                  variant="outline"
                >
                  应用此角色语音
                </Button>
                <Button
                  disabled={isSaving || hasUnsavedChanges}
                  onClick={() =>
                    void onClone({
                      source_card_id: draftCard.meta.card_id,
                      card_id: buildCloneCardId(draftCard.meta.card_id, cardsPayload.cards),
                      name: draftCard.meta.name,
                      title: draftCard.meta.title
                    })
                  }
                  type="button"
                  variant="outline"
                >
                  复制当前卡
                </Button>
                {hasUnsavedChanges ? <Badge className="ml-auto" variant="outline">未保存 · 保存后可激活</Badge> : null}
              </div>

              <nav aria-label="角色卡制作阶段" className="flex gap-1 overflow-x-auto border-b pb-3">
                {CHARACTER_CARD_SECTIONS.map((section) => (
                  <Button
                    aria-current={section.id === activeSectionId ? "step" : undefined}
                    className="shrink-0"
                    size="sm"
                    key={section.id}
                    onClick={() => setActiveSectionId(section.id)}
                    type="button"
                    variant={section.id === activeSectionId ? "secondary" : "ghost"}
                  >
                    {section.label}
                  </Button>
                ))}
              </nav>

              <div className="space-y-6">
                {activeSectionId === "identity" ? (
                  <IdentitySectionEditor card={draftCard} onTextChange={handleTextChanged} />
                ) : null}
                {activeSectionId === "background_context" ? (
                  <BackgroundContextSectionEditor card={draftCard} onTermsChange={handleBackgroundContextChanged} />
                ) : null}
                {activeSectionId === "inner_core" ? (
                  <InnerCoreSectionEditor card={draftCard} onListChange={handleListChanged} onTextChange={handleTextChanged} />
                ) : null}
                {activeSectionId === "relationships" ? (
                  <RelationshipsSectionEditor
                    card={draftCard}
                    onListChange={handleListChanged}
                    onRelationshipsChange={handleRelationshipsChanged}
                    onTextChange={handleTextChanged}
                  />
                ) : null}
                {activeSectionId === "voiceprint" ? (
                  <VoiceprintSectionEditor
                    card={draftCard}
                    onEmotionGradingChange={handleEmotionGradingChanged}
                    onExamplesChange={handleExamplesChanged}
                    onListChange={handleListChanged}
                    onTextChange={handleTextChanged}
                  />
                ) : null}
                {activeSectionId === "streamvox_examples" ? (
                  <StreamVoxExamplesSectionEditor
                    card={draftCard}
                    disabled={
                      isSaving ||
                      !characterAuthoringEnabled ||
                      onGenerateStreamVoxExamples === undefined
                    }
                    generationMessage={streamVoxExampleMessage}
                    onExamplesChange={handleStreamVoxExamplesChanged}
                    onGenerate={() => void handleGenerateStreamVoxExamples()}
                    onTargetModelChange={setStreamVoxExampleTargetModel}
                    targetModel={streamVoxExampleTargetModel}
                  />
                ) : null}
                {activeSectionId === "model_constraints" ? (
                  <ModelConstraintsSectionEditor card={draftCard} onTextChange={handleTextChanged} />
                ) : null}
                {activeSectionId === "wake_entry" ? (
                  <WakeEntrySectionEditor
                    card={draftCard}
                    characterAuthoringEnabled={characterAuthoringEnabled}
                    isBusy={isSaving}
                    onDeleteWakeResponseItem={onDeleteWakeResponseItem}
                    onGenerateWakeResponse={onGenerateWakeResponse}
                    onKeywordsChange={handleWakewordKeywordsChanged}
                    onSynthesizeWakeResponse={onSynthesizeWakeResponse}
                    onSaveWakeResponse={onSaveWakeResponse}
                    wakeResponseConfig={wakeResponseConfig}
                  />
                ) : null}
                {activeSectionId === "voice_binding" ? (
                  <VoiceBindingSectionEditor
                    card={draftCard}
                    isBusy={isSaving}
                    calibrationMessage={voiceCaptionCalibrationMessage}
                    onInferenceOptionsChange={handleVoiceInferenceOptionsChanged}
                    onTextChange={handleTextChanged}
                    onVoiceCaptionCalibration={handleVoiceCaptionCalibration}
                    onVoiceReferenceSelected={handleVoiceReferenceSelected}
                    uploadMessage={voiceReferenceUploadMessage}
                    voiceReferenceDisplayName={voiceReferenceDisplayName}
                    voiceMatchStatus={hideVoiceStatusUntilSave ? null : activeCard.voice_match_status}
                  />
                ) : null}
              </div>

            </CardContent>
          </Card>
        </form>

        <aside className="space-y-6">
          <CoverPanel
            card={draftCard}
            disabled={isSaving}
            onDeleteCover={onDeleteCover}
            onUploadCover={handleCoverSourceSelected}
          />
          {coverLocalError !== null ? <p className="text-sm font-medium text-destructive">{coverLocalError}</p> : null}
          <PreviewPanel card={draftCard} sharedWorldCanon={activeCard.shared_world_canon} />
          <ValidationPanel payload={activeCard} />
        </aside>
      </div>
      {coverDraft !== null ? (
        <CoverCropperDialog
          disabled={isSaving}
          onCancel={closeCoverDraft}
          onConfirm={handleCroppedCoverConfirmed}
          onError={setCoverLocalError}
          preview={coverDraft}
        />
      ) : null}
    </section>
  );
}
