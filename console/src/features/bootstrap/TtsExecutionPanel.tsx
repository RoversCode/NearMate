import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import type { useTtsSetupController } from "./useTtsSetupController";
type Controller = ReturnType<typeof useTtsSetupController>;

/**
 * 把设备检测结果与用户选择分开展示，只有明确确认才保存配置。
 * Args:
 *   controller: 共享语音配置状态与操作。
 *   guided: 是否只展示新用户需要确认的建议，把专业参数放到高级设置。
 *   onContinue: 保存成功后进入下一步；未提供时只保存当前配置。
 * Returns:
 *   检测、选择和模型目录设置区域。
 */
export function TtsExecutionPanel({ controller, guided = false, onContinue }: {
  controller: Controller;
  guided?: boolean;
  onContinue?: () => void;
}): JSX.Element {
  const { capabilities, selection, configurationMatches, busy, anyTaskRunning } = controller;
  const disabled = busy || anyTaskRunning;
  const selectionUnavailable = capabilities !== null && selection !== null && !controller.selectedBackendsAvailable;
  useEffect(() => {
    // 新用户不必理解两套推理引擎；检测建议只填入草稿，点击继续才真正保存。
    if (guided && capabilities && !selection) controller.setSelection(capabilities.recommendation);
  }, [guided, capabilities, selection, controller.setSelection]);
  const advanced = <>
    {capabilities ? <div className="grid gap-3 sm:grid-cols-2">
      <label className="space-y-1 text-sm">ONNX 执行方式<select aria-label="ONNX 执行方式" className="w-full rounded border bg-background p-2" disabled={disabled} value={selection?.onnx_provider ?? ""}
        onChange={event => controller.setSelection({ onnx_provider: event.target.value as "cuda" | "directml" | "cpu", llama_backend: selection?.llama_backend ?? capabilities.recommendation.llama_backend })}>
        <option value="" disabled>请选择</option>{capabilities.backends.filter(item => item.kind === "onnx").map(item => <option key={item.backend} value={item.backend} disabled={!item.available}>{item.backend}{item.available ? "" : "（不可用）"}</option>)}
      </select></label>
      <label className="space-y-1 text-sm">llama.cpp 执行方式<select aria-label="llama.cpp 执行方式" className="w-full rounded border bg-background p-2" disabled={disabled} value={selection?.llama_backend ?? ""}
        onChange={event => controller.setSelection({ onnx_provider: selection?.onnx_provider ?? capabilities.recommendation.onnx_provider, llama_backend: event.target.value as "cuda" | "vulkan" | "cpu" })}>
        <option value="" disabled>请选择</option>{capabilities.backends.filter(item => item.kind === "llama").map(item => <option key={item.backend} value={item.backend} disabled={!item.available}>{item.backend}{item.available ? "" : "（不可用）"}</option>)}
      </select></label>
    </div> : null}
    {capabilities?.backends.filter(item => !item.available).map(item => <p key={`${item.kind}:${item.backend}`} className="text-xs text-muted-foreground">{item.kind} / {item.backend}：{item.reason ?? "检测未通过"}</p>)}
    <label className="block space-y-1 text-sm">模型保存目录<input aria-label="模型保存目录" className="w-full rounded border bg-background p-2" disabled={disabled} value={controller.modelDirectory} onChange={event => controller.setModelDirectory(event.target.value)} placeholder="D:\NearMate\models" /></label>
    <p className="text-xs text-muted-foreground">填写完整的 Windows 绝对路径，且不能放在 NearMate 安装目录内。更换目录或执行方式后，需要重新准备并试听语音。</p>
  </>;
  if (guided) return <section className="space-y-6" aria-label="检查电脑与保存位置">
    <div className="space-y-2">
      <h2 className="text-xl font-semibold">让语音适合这台电脑</h2>
      <p className="text-sm leading-6 text-muted-foreground">NearMate 会检查可用的显卡加速方式。你可以直接使用检测建议，不需要了解显卡或模型参数。</p>
    </div>
    <div className="rounded-xl border bg-muted/30 p-5">
      <p className="font-medium">{selection ? selection.onnx_provider === "cpu" && selection.llama_backend === "cpu" ? "将使用处理器运行语音" : "已选用显卡加速方案" : "先检查这台电脑"}</p>
      <p className="mt-2 text-sm text-muted-foreground">{selection ? "检测结果不代表所有模型都已试听验证；下一步会实际准备并试听。" : "检测可能需要一点时间，不会下载模型或更改系统设置。"}</p>
      <Button className="mt-4" variant="outline" disabled={disabled} onClick={() => void controller.probe()}>{busy ? "正在处理…" : capabilities || selection ? "重新检测电脑" : "检测我的电脑"}</Button>
      {capabilities && selection && (selection.onnx_provider !== capabilities.recommendation.onnx_provider || selection.llama_backend !== capabilities.recommendation.llama_backend) ? <Button className="ml-2 mt-4" variant="outline" disabled={disabled} onClick={() => controller.setSelection(capabilities.recommendation)}>使用检测建议</Button> : null}
      {selectionUnavailable ? <p role="alert" className="mt-3 text-sm text-destructive">上次选择的运行方式在本次检测中不可用，请使用检测建议或在高级设置中重新选择。</p> : null}
    </div>
    <div className="space-y-2 text-sm">
      <p className="font-medium">模型会保存在这里</p>
      <p className="break-all rounded-lg bg-muted/50 p-3 text-muted-foreground">{controller.modelDirectory || "正在读取默认位置…"}</p>
      <p className="text-muted-foreground">模型需要数 GB 空间；下一步会显示准确下载大小。空间不足时，可以在下方换到其他磁盘。</p>
    </div>
    <details className="rounded-lg border p-4"><summary className="cursor-pointer text-sm font-medium">更改保存位置或高级运行设置</summary><div className="mt-4 space-y-3">{advanced}</div></details>
    <div className="flex justify-end"><Button disabled={disabled || selectionUnavailable || (!configurationMatches && !controller.canSaveConfiguration)} onClick={() => {
      if (configurationMatches) onContinue?.();
      else void controller.saveConfiguration().then(saved => { if (saved) onContinue?.(); });
    }}>{busy ? "正在处理…" : configurationMatches ? "下一步：选择语音模型" : "使用此设置并继续"}</Button></div>
  </section>;
  return <section className="space-y-3 rounded-md border p-4" aria-label="执行方式与模型目录">
    <h3 className="font-medium">1. 检测并确认执行方式</h3>
    <Button type="button" variant="outline" disabled={disabled} onClick={() => void controller.probe()}>检测本机执行方式</Button>
    {capabilities ? <>
      <p className="text-sm text-muted-foreground">检测建议：ONNX {capabilities.recommendation.onnx_provider}，llama.cpp {capabilities.recommendation.llama_backend}。</p>
      <Button type="button" variant="outline" disabled={disabled} onClick={() => controller.setSelection(capabilities.recommendation)}>采用检测建议</Button>
    </> : <p className="text-sm text-muted-foreground">{selection ? `当前已确认：ONNX ${selection.onnx_provider}，llama.cpp ${selection.llama_backend}。重新检测可更换执行方式。` : "检测只检查当前电脑，不会替你确认执行方式。"}</p>}
    {advanced}
    <Button type="button" disabled={!controller.canSaveConfiguration || configurationMatches} onClick={() => void controller.saveConfiguration()}>{configurationMatches ? "执行方式与目录已确认" : "确认执行方式与目录"}</Button>
  </section>;
}
