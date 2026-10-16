"""生成无需服务端即可浏览的 Prompt Audit 时序索引。"""

from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Sequence


@dataclass(frozen=True, slots=True)
class PromptAuditIndexEntry:
    """保存运行索引展示一条 LLM 调用所需的轻量信息。

    Args:
        sequence: 当前运行内从一开始递增的调用序号。
        captured_at: 最终请求准备完成时的 UTC ISO 时间。
        role: LLM 职责或内部用途。
        source: 触发本次调用的业务来源。
        operation: 当前调用阶段。
        model: 本次请求使用的真实模型。
        llm_call_id: 本次模型调用的唯一编号。
        request_id: 可选助手请求编号。
        generation_id: 可选输出 generation 编号。
        activity_id: 可选 Activity 编号。
        filename: 同目录下的 Markdown 文件名。
    """

    sequence: int
    captured_at: str
    role: str
    source: str
    operation: str
    model: str
    llm_call_id: str
    request_id: str
    generation_id: str
    activity_id: str
    filename: str


def render_prompt_audit_run_index(
    *,
    process_role: str,
    run_id: str,
    entries: Sequence[PromptAuditIndexEntry],
) -> str:
    """生成一次进程运行内按请求发起顺序排列的导航页。

    Args:
        process_role: Backend 主进程或助手运行时角色。
        run_id: 当前进程生命周期对应的运行编号。
        entries: 按 sequence 升序排列的调用摘要。

    Returns:
        可直接写入本地文件的完整 HTML。
    """

    navigation_items = "\n".join(_render_navigation_item(entry) for entry in entries)
    empty_state = (
        '<p class="empty-state">本次运行还没有记录 LLM 请求。</p>'
        if not entries
        else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Prompt Audit - {escape(run_id)}</title>
  <style>
    :root {{ color-scheme: light; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; color: #202124; background: #f7f8f9; }}
    header {{ height: 64px; padding: 10px 16px; border-bottom: 1px solid #d9dde2; background: #fff; }}
    h1 {{ margin: 0; font-size: 18px; line-height: 24px; letter-spacing: 0; }}
    .run-meta {{ margin-top: 4px; color: #5f6368; font: 12px/16px Consolas, monospace; }}
    .layout {{ display: grid; grid-template-columns: minmax(300px, 380px) minmax(0, 1fr); height: calc(100vh - 64px); }}
    nav {{ overflow: auto; border-right: 1px solid #d9dde2; background: #fff; }}
    .toolbar {{ position: sticky; top: 0; z-index: 1; display: flex; gap: 6px; padding: 8px; border-bottom: 1px solid #e5e7eb; background: #fff; }}
    .toolbar button {{ width: 32px; height: 32px; border: 1px solid #c9ced6; border-radius: 4px; background: #fff; color: #30343b; font-size: 18px; cursor: pointer; }}
    .toolbar button:disabled {{ color: #a8adb5; cursor: default; }}
    .entry {{ display: block; width: 100%; min-height: 88px; padding: 10px 12px; border: 0; border-bottom: 1px solid #eceff2; background: #fff; color: inherit; text-align: left; cursor: pointer; }}
    .entry:hover {{ background: #f2f6f4; }}
    .entry.active {{ background: #e9f4ee; box-shadow: inset 3px 0 #26864c; }}
    .entry-title {{ display: flex; align-items: center; gap: 8px; font-size: 14px; font-weight: 600; line-height: 20px; }}
    .sequence {{ flex: 0 0 46px; color: #356043; font: 12px/20px Consolas, monospace; }}
    .entry-detail {{ margin-top: 5px; color: #5f6368; font: 12px/17px Consolas, monospace; overflow-wrap: anywhere; }}
    main {{ min-width: 0; background: #fff; }}
    iframe {{ display: block; width: 100%; height: 100%; border: 0; background: #fff; }}
    .empty-state {{ padding: 20px; color: #6b7077; font-size: 13px; }}
    @media (max-width: 760px) {{
      header {{ height: auto; min-height: 64px; }}
      .layout {{ grid-template-columns: 1fr; grid-template-rows: minmax(180px, 38vh) minmax(0, 1fr); height: calc(100vh - 72px); }}
      nav {{ border-right: 0; border-bottom: 1px solid #d9dde2; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>LLM Prompt 时序</h1>
    <div class="run-meta">{escape(process_role)} / {escape(run_id)}</div>
  </header>
  <div class="layout">
    <nav aria-label="LLM 调用顺序">
      <div class="toolbar">
        <button id="previous" type="button" title="上一条" aria-label="上一条">&larr;</button>
        <button id="next" type="button" title="下一条" aria-label="下一条">&rarr;</button>
      </div>
      {empty_state}
      {navigation_items}
    </nav>
    <main>
      <iframe id="detail" title="Prompt Markdown" sandbox></iframe>
    </main>
  </div>
  <script>
    const entries = Array.from(document.querySelectorAll('[data-audit-file]'));
    const detail = document.getElementById('detail');
    const previous = document.getElementById('previous');
    const next = document.getElementById('next');
    let activeIndex = -1;

    function selectEntry(index) {{
      if (index < 0 || index >= entries.length) return;
      entries.forEach((entry, entryIndex) => entry.classList.toggle('active', entryIndex === index));
      activeIndex = index;
      detail.src = entries[index].dataset.auditFile;
      previous.disabled = index === 0;
      next.disabled = index === entries.length - 1;
      entries[index].scrollIntoView({{ block: 'nearest' }});
    }}

    entries.forEach((entry, index) => entry.addEventListener('click', () => selectEntry(index)));
    previous.addEventListener('click', () => selectEntry(activeIndex - 1));
    next.addEventListener('click', () => selectEntry(activeIndex + 1));
    document.addEventListener('keydown', (event) => {{
      if (event.key === 'ArrowUp') selectEntry(activeIndex - 1);
      if (event.key === 'ArrowDown') selectEntry(activeIndex + 1);
    }});
    document.querySelectorAll('time[data-utc]').forEach((node) => {{
      const parsed = new Date(node.dataset.utc);
      if (!Number.isNaN(parsed.valueOf())) node.textContent = parsed.toLocaleString();
    }});
    if (entries.length) selectEntry(entries.length - 1);
    else {{ previous.disabled = true; next.disabled = true; }}
  </script>
</body>
</html>
"""


def render_prompt_audit_process_index(
    *,
    process_role: str,
    run_ids: Sequence[str],
) -> str:
    """生成一个进程角色下最近运行的入口页。

    Args:
        process_role: Backend 主进程或助手运行时角色。
        run_ids: 按新到旧排序的运行目录名。

    Returns:
        可直接写入本地文件的完整 HTML。
    """

    links = "\n".join(
        (
            '<li><a href="runs/'
            f'{escape(run_id, quote=True)}/index.html">{escape(run_id)}</a></li>'
        )
        for run_id in run_ids
    )
    if not links:
        links = "<li>还没有 Prompt Audit 运行记录。</li>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Prompt Audit - {escape(process_role)}</title>
  <style>
    :root {{ color-scheme: light; font-family: "Segoe UI", "Microsoft YaHei", sans-serif; }}
    body {{ max-width: 920px; margin: 0 auto; padding: 24px; color: #202124; background: #f7f8f9; }}
    h1 {{ margin: 0 0 6px; font-size: 22px; letter-spacing: 0; }}
    p {{ margin: 0 0 20px; color: #5f6368; }}
    ul {{ margin: 0; padding: 0; list-style: none; border: 1px solid #d9dde2; background: #fff; }}
    li {{ min-height: 44px; padding: 0 14px; border-bottom: 1px solid #eceff2; font: 13px/44px Consolas, monospace; overflow-wrap: anywhere; }}
    li:last-child {{ border-bottom: 0; }}
    a {{ color: #1f6f43; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
  </style>
</head>
<body>
  <h1>{escape(process_role)} Prompt Audit</h1>
  <p>当前可浏览 {len(run_ids)} 个运行，新运行排在最前；活动运行不会被清理。</p>
  <ul>{links}</ul>
</body>
</html>
"""


def _render_navigation_item(entry: PromptAuditIndexEntry) -> str:
    """生成运行索引中的一条可点击调用摘要。

    Args:
        entry: 当前调用的轻量索引信息。

    Returns:
        已完成 HTML 转义的按钮元素。
    """

    identity = next(
        (
            value
            for value in (
                entry.request_id,
                entry.activity_id,
                entry.generation_id,
                entry.llm_call_id,
            )
            if value
        ),
        entry.llm_call_id,
    )
    return f"""<button class="entry" type="button" data-audit-file="{escape(entry.filename, quote=True)}">
  <span class="entry-title"><span class="sequence">#{entry.sequence:06d}</span>{escape(_display_label(entry))}</span>
  <span class="entry-detail"><time data-utc="{escape(entry.captured_at, quote=True)}">{escape(entry.captured_at)}</time><br>{escape(entry.model)}<br>{escape(identity)}</span>
</button>"""


def _display_label(entry: PromptAuditIndexEntry) -> str:
    """把稳定职责字段转换成一眼能辨认的中文调用名称。

    Args:
        entry: 当前调用的轻量索引信息。

    Returns:
        面向开发者的简短调用名称。
    """

    source_label = {
        "text_input": "用户文字",
        "final_asr": "语音输入",
        "system_notice": "System Notice",
        "timed_reminder_notice": "Reminder Notice",
        "self_reflection_entry": "Self-Reflection 入场",
        "self_reflection_recovery": "Self-Reflection Recovery",
        "self_reflection_interruption_reply": "Self-Reflection 插话回应",
    }.get(entry.source, entry.source)
    if entry.role == "foreground" and entry.operation == "initial":
        return f"{source_label} · Foreground 首轮"
    if entry.role == "foreground" and entry.operation == "tool_followup":
        return f"{source_label} · Foreground 工具后二轮"
    if entry.role == "summary" and entry.operation == "compaction":
        return "运行期记忆 · Summary 压缩"
    if entry.role == "cognitive" and entry.operation == "execute":
        return "Cognitive · 任务执行"
    if entry.role == "cognitive_result_repair" and entry.operation == "repair":
        return "Cognitive · Flash 结果修复"
    if entry.role == "character_authoring" and entry.operation == "wake_response_generation":
        return "角色创作 · 唤醒回复"
    if entry.role == "character_authoring" and entry.operation == "streamvox_example_generation":
        return "角色创作 · StreamVox 示例"
    return f"{entry.role} · {source_label} · {entry.operation}"
