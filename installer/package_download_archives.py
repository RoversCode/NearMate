"""将已经验收的 Inno 安装文件归档，并实际解压核对交付内容。"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import stat
import struct
import zipfile


READ_CHUNK_BYTES = 8 * 1024 * 1024
GITHUB_ASSET_LIMIT_BYTES = 2 * 1024**3
GITHUB_LIMIT_SOURCE = (
    "https://docs.github.com/en/repositories/releasing-projects-on-github/"
    "about-releases#storage-and-bandwidth-quotas"
)
# 固定 ZIP 时间戳，让同一份安装文件重复归档时得到相同的下载摘要。
ARCHIVE_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def hash_file(path: Path) -> str:
    """逐块读取文件，计算 SHA256，避免把数 GB 安装文件放进内存。

    Args:
        path: 要核对的普通文件路径。

    Returns:
        小写十六进制 SHA256 摘要。
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def load_verified_installer(
    installer_root: Path, verification_root: Path, profile: str
) -> tuple[dict[str, Path], dict[str, dict[str, object]]]:
    """只接纳与通过验收的报告完全一致的安装程序、分卷和说明文件。

    Args:
        installer_root: 包含 cuda、universal 子目录的安装产物根目录。
        verification_root: 包含 <profile>-installer-verification.json 的目录。
        profile: 本次归档的 cuda 或 universal 版本。

    Returns:
        归档成员的来源路径，以及每个成员的大小和 SHA256。
    """
    directory = installer_root / profile
    report_path = verification_root / f"{profile}-installer-verification.json"
    report_stat = report_path.lstat()
    if not stat.S_ISREG(report_stat.st_mode) or (
        getattr(report_stat, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise ValueError(f"{profile}: 验收报告不是普通文件")
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if report.get("status") != "passed" or report.get("profile") != profile:
        raise ValueError(f"{profile}: 安装产物报告未通过或版本不匹配")
    if report.get("failures") != []:
        raise ValueError(f"{profile}: 验收报告仍包含失败记录")

    artifacts = report.get("artifacts", [])
    bin_count = report.get("contiguous_bin_count")
    if not isinstance(bin_count, int) or isinstance(bin_count, bool) or bin_count < 1:
        raise ValueError(f"{profile}: 报告缺少有效的连续分卷数量")
    installer_names = {f"NearMate-{profile}-setup.exe"} | {
        f"NearMate-{profile}-setup-{number}.bin" for number in range(1, bin_count + 1)
    }
    if not isinstance(artifacts, list) or len(artifacts) != len(installer_names):
        raise ValueError(f"{profile}: 报告中的安装文件数量不完整")
    expected = {item["name"]: item for item in artifacts}
    if set(expected) != installer_names:
        raise ValueError(f"{profile}: 报告中的安装文件名称或分卷编号不完整")
    for name, record in expected.items():
        if type(record.get("bytes")) is not int or record["bytes"] <= 0:
            raise ValueError(f"{profile}: {name} 的报告大小无效")
        if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))):
            raise ValueError(f"{profile}: {name} 的报告摘要无效")
    if sum(item["bytes"] for item in artifacts) != report.get("total_bytes"):
        raise ValueError(f"{profile}: 报告总大小与安装文件不一致")

    allowed_names = installer_names | {"README.txt", "SHA256SUMS.txt", "dependencies.json"}
    # 目录里多出的文件也可能属于旧构建或含有私有配置，不能顺手带入下载包。
    if set(path.name for path in directory.iterdir()) != allowed_names:
        raise ValueError(f"{profile}: 安装目录缺文件或包含未登记文件")
    sources = {name: directory / name for name in sorted(allowed_names)}
    records = {}
    for name, path in sources.items():
        file_stat = path.lstat()
        if not stat.S_ISREG(file_stat.st_mode) or (
            getattr(file_stat, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError(f"{profile}: {name} 不是普通文件")
        record = {"bytes": file_stat.st_size, "sha256": hash_file(path)}
        if name in expected and record != {
            "bytes": expected[name]["bytes"], "sha256": expected[name]["sha256"]
        }:
            raise ValueError(f"{profile}: {name} 与通过验收的报告不一致")
        records[name] = record

    checksums = {}
    for line in sources["SHA256SUMS.txt"].read_text(encoding="utf-8-sig").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\]+)", line)
        if not match or match[2] in checksums:
            raise ValueError(f"{profile}: SHA256SUMS.txt 格式无效或重复")
        checksums[match[2]] = match[1]
    if checksums != {name: expected[name]["sha256"] for name in installer_names}:
        raise ValueError(f"{profile}: SHA256SUMS.txt 与验收报告不一致")
    return sources, records


def write_download_archive(
    archive_path: Path,
    sources: dict[str, Path],
    source_records: dict[str, dict[str, object]],
    profile: str,
    release_label: str,
) -> dict[str, dict[str, object]]:
    """把完整安装目录与中文安装说明写入一个可独立解压的 ZIP64。

    Args:
        archive_path: 尚不存在的归档输出路径。
        sources: 安装文件和发行说明的来源路径。
        source_records: 已核对的来源大小和 SHA256。
        profile: CUDA 或 Universal 的内部版本名称。
        release_label: 用于文件命名和追溯的发行标签。

    Returns:
        归档中所有成员的大小和 SHA256，供后续实际解压核对。
    """
    setup_name = f"NearMate-{profile}-setup.exe"
    instructions = (
        f"NearMate {release_label} Windows x64（{profile}）安装说明\r\n\r\n"
        "1. 下载完整 ZIP 后，右键选择“全部解压”，解压到较短的本地路径。\r\n"
        "2. 等待全部文件解压完成。安装程序和所有 .bin 必须在同一文件夹，保留原名。\r\n"
        f"3. 在解压后的文件夹运行 {setup_name}，选择默认安装目录或较短路径。\r\n"
        "   请不要从 ZIP 内直接运行安装程序，也不要只取出 EXE。\r\n"
        "4. 首次启动时配置模型服务，准备语音并完成试听。\r\n"
        "   NearMate 与内置 StreamVox 免费使用，模型服务凭据由用户自行配置。\r\n\r\n"
        "README.txt 包含发行说明；SHA256SUMS.txt 可核对安装程序与分卷。\r\n"
        "DOWNLOAD-CONTENTS-SHA256SUMS.txt 可核对说明文件等归档内容。\r\n"
        "请为 ZIP、完整解压内容和安装后的程序预留足够磁盘空间。\r\n"
    ).encode("utf-8-sig")
    payloads: dict[str, Path | bytes] = dict(sources)
    payloads["INSTALL.txt"] = instructions
    records = dict(source_records)
    records["INSTALL.txt"] = {
        "bytes": len(instructions), "sha256": hashlib.sha256(instructions).hexdigest()
    }
    manifest = {
        "schema_version": 1,
        "product": "NearMate",
        "release_label": release_label,
        "profile": profile,
        "setup_filename": setup_name,
        "compression": "ZIP_STORED",
        "original_installer_bytes_preserved": True,
        "contents": records,
    }
    payloads["download-manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    manifest_bytes = payloads["download-manifest.json"]
    records["download-manifest.json"] = {
        "bytes": len(manifest_bytes), "sha256": hashlib.sha256(manifest_bytes).hexdigest()
    }
    payloads["DOWNLOAD-CONTENTS-SHA256SUMS.txt"] = "".join(
        f"{record['sha256']}  {name}\r\n" for name, record in sorted(records.items())
    ).encode("ascii")
    checksum_bytes = payloads["DOWNLOAD-CONTENTS-SHA256SUMS.txt"]
    records["DOWNLOAD-CONTENTS-SHA256SUMS.txt"] = {
        "bytes": len(checksum_bytes), "sha256": hashlib.sha256(checksum_bytes).hexdigest()
    }

    # Inno 分卷已经是 LZMA2/max；外层只归档，避免重压数 GB 文件而损失时间和兼容性。
    with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for name, payload in sorted(payloads.items()):
            member = zipfile.ZipInfo(name, ARCHIVE_TIMESTAMP)
            member.create_system = 0
            member.external_attr = 0x20
            member.file_size = records[name]["bytes"]
            # 显式使用 ZIP64 本地头，后续版本变大也不会在写到一半时超出传统 ZIP 限制。
            with archive.open(member, "w", force_zip64=True) as destination:
                if isinstance(payload, bytes):
                    destination.write(payload)
                else:
                    with payload.open("rb") as source:
                        shutil.copyfileobj(source, destination, READ_CHUNK_BYTES)
    return records


def verify_download_archive(
    archive_path: Path, expected_records: dict[str, dict[str, object]], extraction_directory: Path
) -> dict[str, object]:
    """实际解压并核对每个成员，同时检查 Windows 解压所需的基础 ZIP 格式。

    Args:
        archive_path: 已写完的单文件 ZIP 归档。
        expected_records: 归档前确定的成员大小和 SHA256。
        extraction_directory: 尚不存在的核验目录；保留解压结果作为证据。

    Returns:
        解压结果、ZIP64 记录和基础格式兼容性检查报告。
    """
    extraction_directory.mkdir(parents=True, exist_ok=False)
    extracted_records = {}
    with zipfile.ZipFile(archive_path) as archive, archive_path.open("rb") as raw_archive:
        members = archive.infolist()
        if len(members) != len(expected_records) or {item.filename for item in members} != set(expected_records):
            raise ValueError("ZIP 的成员集合与交付清单不一致")
        for member in members:
            if member.filename != Path(member.filename).name or member.flag_bits & 1:
                raise ValueError("ZIP 含目录穿越或加密成员，无法按普通安装流程解压")
            if member.compress_type != zipfile.ZIP_STORED:
                raise ValueError("ZIP 没有使用预定的标准 Stored 压缩方法")
            raw_archive.seek(member.header_offset)
            header = struct.unpack("<4s5H3L2H", raw_archive.read(30))
            raw_archive.read(header[9])
            extra = raw_archive.read(header[10])
            if header[0] != b"PK\x03\x04" or header[1] != 45 or extra[:2] != b"\x01\x00":
                raise ValueError("ZIP 成员缺少标准 ZIP64 本地头")
            target = extraction_directory / member.filename
            # 完整读至成员末尾会同时触发 zipfile 的 CRC 检查；随后重读落盘文件核对 SHA256。
            with archive.open(member) as source, target.open("xb") as destination:
                shutil.copyfileobj(source, destination, READ_CHUNK_BYTES)
            record = {"bytes": target.stat().st_size, "sha256": hash_file(target)}
            if record != expected_records[member.filename]:
                raise ValueError(f"解压后的 {member.filename} 与原始交付内容不一致")
            extracted_records[member.filename] = record

        raw_archive.seek(-22, 2)
        end = struct.unpack("<4s4H2LH", raw_archive.read(22))
        if end[0] != b"PK\x05\x06" or end[1:3] != (0, 0) or end[7] != 0:
            raise ValueError("ZIP 不是无注释的单磁盘归档")
        raw_archive.seek(-42, 2)
        locator = struct.unpack("<4sLQL", raw_archive.read(20))
        has_zip64_end_records = locator[0] == b"PK\x06\x07"
        if has_zip64_end_records:
            if locator[1] != 0 or locator[3] != 1:
                raise ValueError("ZIP64 结束记录包含多磁盘信息")
            raw_archive.seek(locator[2])
            zip64_end = struct.unpack("<4sQ2H2L4Q", raw_archive.read(56))
            if zip64_end[0] != b"PK\x06\x06" or zip64_end[4:6] != (0, 0):
                raise ValueError("ZIP64 中央目录不是单磁盘格式")
            if zip64_end[6] != len(members) or zip64_end[7] != len(members):
                raise ValueError("ZIP64 中央目录成员数量不一致")
        elif archive_path.stat().st_size > zipfile.ZIP64_LIMIT:
            raise ValueError("大文件 ZIP 缺少 ZIP64 结束记录")

    return {
        "status": "passed",
        "extraction_directory": str(extraction_directory.absolute()),
        "extracted_members": extracted_records,
        "zip64_local_headers": True,
        "zip64_end_records": has_zip64_end_records,
        "single_disk": True,
        "crc_and_extracted_sha256_verified": True,
        "windows_explorer_format_checks": {
            "compression_method": "Stored (0)",
            "encryption": False,
            "ascii_flat_filenames": True,
            "zip64_version_needed": "4.5",
            "actual_explorer_extraction_tested": False,
        },
        "real_installation_acceptance_performed": False,
    }


def main() -> int:
    """从命令行生成两版完整下载归档、核验报告和总 SHA256 清单。

    Args:
        无；归档来源、验收目录、输出目录和发行标签由命令行传入。

    Returns:
        全部生成和核验成功时返回 0；遇到不一致时抛出异常并保留现场。
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installer-root", type=Path, required=True)
    parser.add_argument("--verification-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--release-label", required=True)
    parser.add_argument("--extraction-root", type=Path)
    arguments = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", arguments.release_label):
        parser.error("发行标签只能包含英文、数字、点、下划线和连字符")
    output_root = arguments.output_root.absolute()
    extraction_root = arguments.extraction_root or output_root.parent / "archive-verification"
    output_root.mkdir(parents=True, exist_ok=True)
    profiles = ("cuda", "universal")
    planned_names = ["downloads-manifest.json", "SHA256SUMS.txt"]
    for profile in profiles:
        archive_name = f"NearMate-{profile}-{arguments.release_label}-windows-x64.zip"
        planned_names.extend([
            archive_name, archive_name + ".partial", f"{profile}-archive-verification.json",
            f"{profile}-installer-verification.json",
        ])
        if (extraction_root / profile).exists():
            raise FileExistsError(f"核验目录已存在，请使用新的输出位置：{extraction_root / profile}")
    for name in planned_names:
        if (output_root / name).exists():
            raise FileExistsError(f"拒绝覆盖已有产物：{output_root / name}")

    downloads = []
    for profile in profiles:
        print(f"{profile}: 核对通过验收的安装文件", flush=True)
        sources, records = load_verified_installer(arguments.installer_root, arguments.verification_root, profile)
        archive_name = f"NearMate-{profile}-{arguments.release_label}-windows-x64.zip"
        archive_path = output_root / archive_name
        partial_path = output_root / (archive_name + ".partial")
        print(f"{profile}: 写入完整 ZIP64", flush=True)
        archive_records = write_download_archive(partial_path, sources, records, profile, arguments.release_label)
        print(f"{profile}: 实际解压并核对大小、CRC、SHA256", flush=True)
        verification = verify_download_archive(partial_path, archive_records, extraction_root / profile)
        archive_bytes = partial_path.stat().st_size
        archive_sha256 = hash_file(partial_path)
        partial_path.rename(archive_path)
        download = {
            "profile": profile,
            "name": archive_name,
            "bytes": archive_bytes,
            "sha256": archive_sha256,
            "github_release_upload_eligible": archive_bytes < GITHUB_ASSET_LIMIT_BYTES,
            "verification_report": f"{profile}-archive-verification.json",
            "installer_verification_report": f"{profile}-installer-verification.json",
            "installer_artifacts": {
                name: record for name, record in records.items() if name.endswith((".exe", ".bin"))
            },
        }
        verification.update({"archive": download, "release_label": arguments.release_label})
        # 安装产物报告含有构建机路径，保留在下载包旁供审计，不放入用户安装归档。
        shutil.copyfile(
            arguments.verification_root / download["installer_verification_report"],
            output_root / download["installer_verification_report"],
        )
        (output_root / download["verification_report"]).write_text(
            json.dumps(verification, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        downloads.append(download)
        print(f"{profile}: passed, {archive_bytes} bytes, SHA256 {archive_sha256}", flush=True)

    manifest = {
        "schema_version": 1,
        "product": "NearMate",
        "release_label": arguments.release_label,
        "status": "passed",
        "downloads": downloads,
        "github_release_asset_limit": {
            "exclusive_max_bytes": GITHUB_ASSET_LIMIT_BYTES,
            "official_source": GITHUB_LIMIT_SOURCE,
            "official_text": "Each file included in a release must be under 2 GiB.",
        },
    }
    (output_root / "downloads-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_root / "SHA256SUMS.txt").write_text(
        "".join(f"{item['sha256']}  {item['name']}\n" for item in downloads), encoding="ascii"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
