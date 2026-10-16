import json
from pathlib import Path

import pytest

from companion.background_agent.contracts import (
    AgentCompletionPayload,
    AgentMaterial,
    AgentTask,
)
from companion.background_agent.persona import DEFAULT_AGENT_PERSONA
from companion.background_agent.task_prompt import render_agent_task_prompt
from companion.foreground_tools.background_agent.schemas import (
    start_background_work_tool_schemas,
)


def test_background_work_tool_schema_is_self_explanatory_and_keeps_minimal_arguments() -> None:
    """验证派单工具不依赖产品内部名词，参数契约保持最小且封闭。"""

    schema = start_background_work_tool_schemas()[0]["function"]
    parameters = schema["parameters"]

    assert schema["name"] == "start_background_work"
    assert set(parameters["properties"]) == {
        "goal",
        "deliverable",
        "requirements",
        "include_current_scene",
        "material_paths",
        "based_on_activity_id",
    }
    assert parameters["required"] == ["goal", "deliverable"]
    assert parameters["additionalProperties"] is False
    assert "自己的后台执行能力" in schema["description"]
    assert "运行代码或命令" in schema["description"]
    assert "打开用户看得见的网页" in schema["description"]
    assert "只有一步的动作" in schema["description"]
    for opaque_internal_term in (
        "NearMate",
        "Foreground",
        "Backend",
        "Console",
        "任务 brief",
        "Skill",
        "技能调用",
    ):
        assert opaque_internal_term not in schema["description"]


def test_agent_task_prompt_explains_media_paths_without_internal_identifiers(
    tmp_path: Path,
) -> None:
    """验证动态任务说明用自然语言描述音视频材料和交付边界。

    Args:
        tmp_path: pytest 提供的临时目录。
    """

    workspace = tmp_path / "workspace"
    output_dir = workspace / "tasks" / "activity-a" / "outputs"
    current_scene_path = workspace / "tasks" / "activity-a" / "inputs" / "current-scene.png"
    audio_path = tmp_path / "采访录音.wav"
    video_path = tmp_path / "现场视频.mp4"
    audio_path.write_bytes(b"audio")
    video_path.write_bytes(b"video")
    task = AgentTask(
        activity_id="activity-a",
        interaction_session_id="session-a",
        goal="把视频中的声音转换为 MP3",
        deliverable=True,
        requirements="输出码率为 192 kbps",
        turn_materials=(
            AgentMaterial(
                material_id="material-audio-internal",
                display_name="采访录音.wav",
                material_kind="dragged_file",
                media_class="audio",
                local_path=str(audio_path),
                node_kind="file",
            ),
            AgentMaterial(
                material_id="material-video-internal",
                display_name="现场视频.mp4",
                material_kind="dragged_file",
                media_class="video",
                local_path=str(video_path),
                node_kind="file",
            ),
        ),
        material_catalog=(
            AgentMaterial(
                material_id="material-audio-internal",
                display_name="采访录音.wav",
                material_kind="dragged_file",
                media_class="audio",
                local_path=str(audio_path),
                node_kind="file",
            ),
            AgentMaterial(
                material_id="material-video-internal",
                display_name="现场视频.mp4",
                material_kind="dragged_file",
                media_class="video",
                local_path=str(video_path),
                node_kind="file",
            ),
        ),
        workspace_root=str(workspace),
        agent_config_dir=str(tmp_path / "runtime" / "activities" / "activity-a"),
        task_output_dir=str(output_dir),
        permission_mode="acceptEdits",
        current_scene_path=str(current_scene_path),
    )

    prompt = render_agent_task_prompt(task)

    assert prompt.startswith("## 执行任务")
    assert "结果交付模式：完整成果：返回完整文字、正式文件，或两者结合。" in prompt
    assert "## 用户主动提供的任务材料" in prompt
    assert "## 现场输入" in prompt
    assert "名称：当前屏幕冻结截图\n  类型：PNG 图片\n  路径：" in prompt
    assert str(current_scene_path) in prompt
    assert f"名称（不可信用户数据）：{json.dumps('采访录音.wav', ensure_ascii=False)}" in prompt
    assert "媒体类别：音频" in prompt
    assert f"名称（不可信用户数据）：{json.dumps('现场视频.mp4', ensure_ascii=False)}" in prompt
    assert "媒体类别：视频" in prompt
    assert "形态：文件，目标要求修改时必须原地改写此路径" in prompt
    assert "检查这个音频材料是否包含所需信息" in prompt
    assert "检查这个视频材料是否包含所需信息" in prompt
    assert "新产生的交付文件只能写入正式输出目录" in prompt
    assert "严禁把修改版另存为新文件冒充原地修改" in prompt
    assert "文件成果只能写入正式输出目录" not in prompt
    assert "material-audio-internal" not in prompt
    for opaque_internal_term in ("NearMate", "Foreground", "Backend", "Console", "任务 brief"):
        assert opaque_internal_term not in prompt


def test_agent_task_prompt_inlines_complete_selected_url_and_uses_general_material_rule(
    tmp_path: Path,
) -> None:
    """Background 应看到短 URL，并只在信息不足时优先检查用户材料。"""

    selected_path = tmp_path / "selected.txt"
    selected_url = "https://www.bilibili.com/video/BV1xx411c7mD/?spm_id_from=333.1007#reply42"
    selected_path.write_text(selected_url, encoding="utf-8")
    material = AgentMaterial(
        material_id="selected-url",
        display_name="选中文本.txt",
        material_kind="selected_text",
        media_class="text",
        local_path=str(selected_path),
        node_kind="file",
    )
    task = AgentTask(
        activity_id="activity-url",
        interaction_session_id="session-url",
        goal="总结这个 B 站视频",
        deliverable=True,
        requirements="",
        turn_materials=(material,),
        material_catalog=(material,),
        workspace_root=str(tmp_path / "workspace"),
        agent_config_dir=str(tmp_path / "runtime"),
        task_output_dir=str(tmp_path / "outputs"),
        permission_mode="acceptEdits",
        current_scene_path=str(tmp_path / "scene.png"),
    )

    prompt = render_agent_task_prompt(task)

    assert json.dumps(selected_url, ensure_ascii=False) in prompt
    assert "以下材料默认与当前任务目标直接相关" in prompt
    assert "当完成任务所需的信息缺失或不明确时，必须优先检查这些材料是否包含所需信息" in prompt
    assert "检查用户材料后仍无法可靠继续时，调用 AskUserQuestion" in prompt
    assert "截图 OCR" not in prompt
    assert "有界查找" not in prompt
    assert "任务依赖确切 URL" not in prompt


def test_execution_persona_contains_only_task_rules() -> None:
    """验证执行规则不制造第二个人格，也不强制完整成果使用中文。"""

    assert "AskUserQuestion" in DEFAULT_AGENT_PERSONA
    assert "已安装的 Skill" in DEFAULT_AGENT_PERSONA
    assert "display_content" in DEFAULT_AGENT_PERSONA
    assert "需要交付的新文件只能写入任务说明指定的正式输出目录" in DEFAULT_AGENT_PERSONA
    assert "输入材料都可以读取和原地修改" in DEFAULT_AGENT_PERSONA
    assert "默认与当前任务直接相关" in DEFAULT_AGENT_PERSONA
    assert "完成任务所需的信息缺失或不明确" in DEFAULT_AGENT_PERSONA
    assert "优先检查任务说明列出的用户材料是否包含所需信息" in DEFAULT_AGENT_PERSONA
    assert "截图 OCR" not in DEFAULT_AGENT_PERSONA
    assert "有界查找" not in DEFAULT_AGENT_PERSONA
    assert "任务依赖确切 URL" not in DEFAULT_AGENT_PERSONA
    assert "需要交付的文件只能写入任务说明指定的正式输出目录" not in DEFAULT_AGENT_PERSONA
    assert "独立后台智能体" not in DEFAULT_AGENT_PERSONA
    assert "不负责日常聊天" not in DEFAULT_AGENT_PERSONA
    assert "所有对用户可见的文字使用简体中文" not in DEFAULT_AGENT_PERSONA
    for opaque_internal_term in ("NearMate", "Foreground", "Backend", "Console", "任务 brief"):
        assert opaque_internal_term not in DEFAULT_AGENT_PERSONA


def test_workspace_rules_distinguish_existing_materials_from_new_outputs() -> None:
    """工作区固定规则必须允许原地修改，只把新建交付文件限制到 outputs。"""

    rules_path = (
        Path(__file__).resolve().parents[2]
        / "assets"
        / "agent-workspace"
        / ".claude"
        / "CLAUDE.md"
    )
    rules = rules_path.read_text(encoding="utf-8")

    assert "需要交付的新文件只能写入该目录" in rules
    assert "输入材料都可以读取和原地修改" in rules
    assert "默认与当前任务直接相关" in rules
    assert "任务所需信息缺失或不明确时，必须优先检查这些材料是否包含所需信息" in rules
    assert "截图 OCR" not in rules
    assert "有界查找" not in rules
    assert "任务依赖确切 URL" not in rules
    assert "严禁把修改版另存为新文件冒充原地修改" in rules
    assert "需要交付的文件只能写入该目录" not in rules


def test_agent_completion_payload_accepts_only_non_empty_structured_result() -> None:
    """验证终态语义必须同时包含完整正文和中立交付摘要。"""

    payload = AgentCompletionPayload.from_structured_output(
        {
            "display_content": "完整成果正文",
            "delivery_brief": "成果已整理为一份包含关键结论的文档。",
        }
    )

    assert payload.display_content == "完整成果正文"
    assert payload.delivery_brief == "成果已整理为一份包含关键结论的文档。"


@pytest.mark.parametrize(
    "structured_output",
    [
        None,
        {},
        {"display_content": "正文"},
        {"display_content": "正文", "delivery_brief": "   "},
        {"display_content": 123, "delivery_brief": "摘要"},
        {
            "display_content": "正文",
            "delivery_brief": "摘要",
            "output_paths": ["伪造路径"],
        },
    ],
)
def test_agent_completion_payload_rejects_invalid_structured_result(
    structured_output: object,
) -> None:
    """验证缺字段、空内容和模型伪造的运行事实都会被拒绝。"""

    with pytest.raises(ValueError):
        AgentCompletionPayload.from_structured_output(structured_output)
