import { type FormEvent, useEffect, useState } from "react";
import type { ConsoleSettings } from "../../api/contracts/settings";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";

type UserProfileSettingsFormProps = {
  settings: ConsoleSettings;
  isSaving: boolean;
  onSave: (settings: ConsoleSettings) => Promise<void | boolean>;
};

/**
 * 渲染用户资料设置表单卡片。
 *
 * Args:
 *   settings: 后端返回的完整设置快照；isSaving 表示当前设置保存状态；onSave 负责提交完整快照。
 *
 * Returns:
 *   用户可以编辑自我简介和自由补充，并通过一次按钮点击保存到后端的表单卡片；
 *   保存期间禁用输入和按钮，提交前裁剪首尾空白，避免不可见字符进入 prompt 用户资料。
 */
export function UserProfileSettingsForm({ settings, isSaving, onSave }: UserProfileSettingsFormProps): JSX.Element {
  const [userSelfIntro, setUserSelfIntro] = useState(settings.user_self_intro);
  const [userExtraNotes, setUserExtraNotes] = useState(settings.user_extra_notes);

  useEffect(() => {
    // 父级保存成功后会替换 settings 快照；这里同步本地草稿，保证界面显示后端最终接受的内容。
    setUserSelfIntro(settings.user_self_intro);
    setUserExtraNotes(settings.user_extra_notes);
  }, [settings.user_self_intro, settings.user_extra_notes]);

  /**
   * 提交用户资料设置。
   *
   * Args:
   *   event: 表单提交事件，用于阻止默认刷新。
   *
   * Returns:
   *   无；将两项用户资料合并进完整设置快照并交给父组件保存。
   *   保存中直接返回，避免重复请求覆盖顺序不确定。
   */
  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();

    // 保存中不再提交第二次，业务意图是保持本地用户设置文件中的用户资料变更顺序清晰。
    if (isSaving) {
      return;
    }

    // 提交完整设置快照，避免只保存用户资料时覆盖提醒开关或模型选择字段。
    await onSave({
      ...settings,
      user_self_intro: userSelfIntro.trim(),
      user_extra_notes: userExtraNotes.trim()
    });
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>我的信息</CardTitle>
        <CardDescription>
          这些内容只来自你在控制台的手动填写；每个角色怎么称呼你，请在角色卡里配置。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="space-y-5" onSubmit={(event) => void handleSubmit(event)}>
          <div className="space-y-2">
            <h3 className="text-sm font-medium">自我简介</h3>
            <Textarea
              aria-label="自我简介"
              disabled={isSaving}
              onChange={(event) => setUserSelfIntro(event.target.value)}
              rows={3}
              value={userSelfIntro}
            />
            <p className="text-sm text-muted-foreground">简短介绍你是谁、现在主要在做什么。</p>
          </div>

          <div className="space-y-2">
            <h3 className="text-sm font-medium">自由补充</h3>
            <Textarea
              aria-label="自由补充"
              disabled={isSaving}
              onChange={(event) => setUserExtraNotes(event.target.value)}
              rows={4}
              value={userExtraNotes}
            />
            <p className="text-sm text-muted-foreground">补充少量你希望桌宠了解的稳定背景。</p>
          </div>

          <div className="flex justify-end">
            <Button disabled={isSaving} type="submit">
              保存我的信息
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
