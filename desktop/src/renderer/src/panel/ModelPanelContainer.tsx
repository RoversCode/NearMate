import { ModelPicker } from "../components/ModelPicker";
import { usePanelRuntime } from "./usePanelRuntime";

/** ModelPanelContainer 独立拥有人物设置水合与切换状态。 */
export function ModelPanelContainer() {
  const runtime = usePanelRuntime();

  return (
    <section className="panel-model-view" aria-label="人物切换面板">
      <button
        className="panel-actions-button"
        type="button"
        onClick={runtime.resetAvatarLayout}
      >
        重置位置
      </button>
      <ModelPicker
        models={runtime.modelCatalog}
        selectedModelId={runtime.selectedModelId}
        pendingModelId={runtime.pendingModelId}
        errorMessage={runtime.modelSwitchError}
        disabled={!runtime.settingsHydrated}
        onSelect={runtime.onSelectModel}
      />
    </section>
  );
}
