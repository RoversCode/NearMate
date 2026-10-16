from __future__ import annotations

from pathlib import Path


TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html", ".htm",
    ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".java", ".go", ".rs", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".php", ".rb", ".swift", ".kt", ".kts", ".sh", ".ps1", ".bat", ".sql", ".toml",
    ".ini", ".cfg", ".conf", ".env", ".log", ".vue", ".svelte", ".graphql", ".proto",
}
TEXT_FILE_NAMES = {"dockerfile", "makefile", "gemfile", "procfile", ".gitignore", ".editorconfig"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
DOCUMENT_SUFFIXES = {".pdf", ".docx"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
IGNORED_DIRECTORY_NAMES = {
    ".git", ".svn", ".hg", ".cache", ".next", ".nuxt", ".venv", "venv", "node_modules", "dist", "build",
    "coverage", "__pycache__", "target", "out",
}


class MaterialTypeRegistry:
    """集中维护接纳、预检与执行共享的材料类型白名单。"""

    def is_supported_path(self, path: Path) -> bool:
        """判断路径名称是否属于受支持白名单。"""

        supported_suffixes = (
            TEXT_SUFFIXES
            | IMAGE_SUFFIXES
            | DOCUMENT_SUFFIXES
            | AUDIO_SUFFIXES
            | VIDEO_SUFFIXES
        )
        return path.suffix.lower() in supported_suffixes or path.name.casefold() in TEXT_FILE_NAMES

    def classify_path_media(self, path: Path) -> str:
        """把白名单路径归入对应的材料媒体类别。"""

        suffix = path.suffix.lower()
        if suffix in AUDIO_SUFFIXES:
            return "audio"
        if suffix in VIDEO_SUFFIXES:
            return "video"
        if suffix in TEXT_SUFFIXES or path.name.casefold() in TEXT_FILE_NAMES:
            return "text"
        return "mixed_or_document"


MATERIAL_TYPES = MaterialTypeRegistry()
