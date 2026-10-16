from __future__ import annotations

import hashlib
from pathlib import Path

from companion.background_agent.skill_manifest import scan_skill_manifests


def test_managed_control_skills_are_valid_and_default_disabled() -> None:
    """官方 CUA Skill 必须作为一项受管能力识别，且不能默认开放。"""

    backend_root = Path(__file__).resolve().parents[2]
    asset_root = backend_root / "assets" / "agent-workspace"
    skills_root = asset_root / ".claude" / "skills"

    manifests, issues = scan_skill_manifests(skills_root, asset_root=asset_root)

    assert issues == ()
    assert "browser-use" not in manifests
    assert "computer-use" not in manifests
    manifest = manifests["cua-driver"]
    assert manifest.default_enabled is False
    assert manifest.version == "0.22.1"
    assert len(manifest.dependencies) == 1
    assert manifest.dependencies[0].kind == "executable"
    assert manifest.dependencies[0].name == "cua-driver"
    assert manifest.dependencies[0].minimum_version == "0.22.1"


def test_managed_cua_skill_matches_the_pinned_official_pack() -> None:
    """受管工作流内容必须与固定版本官方发布包一致。"""

    backend_root = Path(__file__).resolve().parents[2]
    skill_root = (
        backend_root
        / "assets"
        / "agent-workspace"
        / ".claude"
        / "skills"
        / "cua-driver"
    )
    expected_hashes = {
        "SKILL.md": "D9CC9580DD29F0AEE04671E95517EFFFAAE3286D6E7A3D333FA13B7F53A538C0",
        "WINDOWS.md": "14230D64CC8C1993DE242D916E693FF192CE4DD5BED773DCF15061E953D35712",
        "BROWSER.md": "376960A136247D8AC2850F789FB7761C9619967C85E0F30C410F32890902E5D4",
        "EMBEDDING.md": "412187F1800226A92CF4D026AE5CE0AE33C1858072941276E95EB935593A6039",
        "RECORDING.md": "A97F9E7CE53D22E6E8F82AD1A741787E126F6BF746781D4C49488B48E657DB52",
        "README.md": "07580CC0A7DF49DC088939E88D8C95AE387AEB4A94D61142F4527FADE4B4328C",
    }

    for file_name, expected_hash in expected_hashes.items():
        # apply_patch 会在官方文件已有 EOF 换行后再补一个空行。统一成单个 LF
        # 后再比对，正文和换行风格的任何其它变化仍会失败。
        normalized_content = (
            (skill_root / file_name).read_bytes().rstrip(b"\r\n") + b"\n"
        )
        actual_hash = hashlib.sha256(normalized_content).hexdigest()
        assert actual_hash.casefold() == expected_hash.casefold()
