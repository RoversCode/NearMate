import type { Live2dModelCatalogEntry } from "../../../shared/live2dModelTypes";

type ModelPickerProps = {
  /**
   * 可选模型目录列表直接沿用共享 catalog 契约。
   * 组件只消费目录名展示信息，不参与目录发现或模型路径解析。
   */
  models: readonly Live2dModelCatalogEntry[];
  /**
   * 当前已经生效的人物目录名。
   * 为空时表示上层还未完成设置水合，此时组件仍保持纯展示。
   */
  selectedModelId: string | null;
  /**
   * 当前正在切换中的人物目录名。
   * 只要存在 pending，视图层就统一禁用全部切换按钮，避免重复提交。
   */
  pendingModelId: string | null;
  /**
   * 上层传入的人物切换失败文案。
   * 组件只负责显示，不负责生成或清理错误状态。
   */
  errorMessage: string | null;
  /**
   * disabled 表示上层当前只允许静态展示，不允许发起切换。
   * 典型场景是设置尚未完成首次水合，此时不能假设当前人物状态已经可写。
   */
  disabled?: boolean;
  /**
   * 选择某个人物后的上抛回调。
   * 组件只在点击“非当前模型”且没有 pending 锁时通知上层。
   */
  onSelect(modelId: string): void;
};

/**
 * 显示可切换的虚拟形象，并把切换失败放在列表上方，避免长列表遮住结果。
 *
 * Args:
 *   props: 模型目录、当前与待切换人物、失败原因以及选择回调。
 *
 * Returns:
 *   可滚动的人物按钮列表；设置未水合时保持只读。
 */
export function ModelPicker({
  models,
  selectedModelId,
  pendingModelId,
  errorMessage,
  disabled = false,
  onSelect
}: ModelPickerProps) {
  return (
    <section className="model-picker-card" aria-label="人物选择">
      <header className="model-picker-header">
        <h2>人物</h2>
        <p>点击名称立即切换当前桌宠人物。</p>
      </header>

      {errorMessage ? <p className="model-picker-error" role="alert">{errorMessage}</p> : null}

      {models.length > 0 ? (
        <div className="model-picker-list">
          {models.map((model) => {
            /**
             * 当前条目是否已经是持久化生效的人物目录。
             * 这里单独显式计算，避免 JSX 里重复比较，提升可读性。
             */
            const isSelected = model.id === selectedModelId;
            /**
             * 当前条目是否正处于切换中的过渡态。
             * pending 文案只跟随目标目录名展示，避免多个条目同时出现“切换中”。
             */
            const isPending = model.id === pendingModelId;

            return (
              <button
                key={model.id}
                type="button"
                data-model-id={model.id}
                className="model-picker-item"
                data-selected={isSelected ? "true" : "false"}
                data-pending={isPending ? "true" : "false"}
                disabled={disabled || pendingModelId !== null}
                onClick={() => {
                  /**
                   * 当前人物再次点击没有业务意义。
                   * 这里直接吞掉重复操作，避免上层收到无效切换请求。
                   */
                  if (isSelected || disabled || pendingModelId !== null) {
                    return;
                  }

                  onSelect(model.id);
                }}
              >
                <span>{model.label}</span>
                {isPending ? <strong>切换中</strong> : isSelected ? <strong>当前</strong> : null}
              </button>
            );
          })}
        </div>
      ) : (
        <p className="model-picker-empty">当前没有发现可用人物目录。</p>
      )}

    </section>
  );
}
