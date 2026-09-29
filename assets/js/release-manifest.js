const repositoryUrl = "https://github.com/RoversCode/NearMate";
const profileNames = ["universal", "cuda"];

/**
 * 检查发行清单，避免页面给用户提供不完整或指向错误仓库的下载。
 * @param {unknown} value 从 JSON 读取的发行数据。
 * @returns {object} 已通过校验的发行清单。
 * @throws {Error} 版本、地址、文件大小或校验值不符合发行要求时抛出。
 */
export function validateReleaseManifest(value) {
  if (!value || typeof value !== "object" || value.schemaVersion !== 1) {
    throw new Error("发行清单 schemaVersion 必须为 1。");
  }
  if (typeof value.version !== "string" || !/^\d+\.\d+\.\d+(?:-[a-zA-Z0-9.-]+)?$/.test(value.version)) {
    throw new Error("发行版本必须是有效的版本号。");
  }
  if (!["pending", "published"].includes(value.status)) {
    throw new Error("发行状态必须是 pending 或 published。");
  }
  if (!value.profiles || Object.keys(value.profiles).length !== profileNames.length || profileNames.some((name) => !Array.isArray(value.profiles[name]?.assets))) {
    throw new Error("发行清单必须同时包含 Universal 与 CUDA 文件列表。");
  }

  // 发布前只保留有效的 Releases 入口，避免先把尚未上传的文件链接放到官网。
  if (value.status === "pending") {
    if (value.releaseUrl !== `${repositoryUrl}/releases` || value.checksumsUrl !== null || value.publishedAt !== null || profileNames.some((name) => value.profiles[name].assets.length > 0)) {
      throw new Error("待发布清单不得包含下载文件、校验链接或发布日期。");
    }
    return value;
  }

  const downloadPrefix = `${repositoryUrl}/releases/download/v${value.version}/`;
  if (value.releaseUrl !== `${repositoryUrl}/releases/tag/v${value.version}`) {
    throw new Error("已发布清单必须指向同版本 GitHub Release。");
  }
  if (typeof value.publishedAt !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value.publishedAt) || Number.isNaN(Date.parse(value.publishedAt)) || new Date(value.publishedAt).toISOString().slice(0, 10) !== value.publishedAt) {
    throw new Error("发布日期必须采用 YYYY-MM-DD 格式。");
  }
  if (![`${downloadPrefix}SHA256SUMS`, `${downloadPrefix}SHA256SUMS.txt`].includes(value.checksumsUrl)) {
    throw new Error("已发布清单必须提供同版本的 SHA256SUMS 校验文件。");
  }

  const seenNames = new Set();
  for (const profileName of profileNames) {
    const assets = value.profiles[profileName].assets;
    if (!assets.length || assets.filter((asset) => typeof asset?.name === "string" && asset.name.endsWith(".exe")).length !== 1) {
      throw new Error(`${profileName} 必须有且只有一个安装程序。`);
    }
    for (const asset of assets) {
      if (!asset || typeof asset.name !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]*\.(?:exe|bin)$/.test(asset.name) || !asset.name.toLowerCase().includes(profileName) || seenNames.has(asset.name)) {
        throw new Error(`${profileName} 存在无效、重复或版本混用的文件名。`);
      }
      seenNames.add(asset.name);
      if (asset.url !== `${downloadPrefix}${encodeURIComponent(asset.name)}`) {
        throw new Error(`${asset.name} 的地址必须指向本仓库同版本的 Release 附件。`);
      }
      if (!Number.isSafeInteger(asset.size) || asset.size <= 0 || typeof asset.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(asset.sha256)) {
        throw new Error(`${asset.name} 缺少有效的文件大小或 SHA256。`);
      }
    }

    // Inno Setup 的分卷不能漏号；缺少任何一卷，用户都无法完成安装。
    const installerBase = assets.find((asset) => asset.name.endsWith(".exe")).name.slice(0, -4);
    const volumes = assets.filter((asset) => asset.name.endsWith(".bin"));
    if (volumes.length === 0) {
      throw new Error(`${profileName} 的分卷安装包至少需要一个 .bin 文件。`);
    }
    for (let index = 1; index <= volumes.length; index += 1) {
      if (!volumes.some((asset) => asset.name === `${installerBase}-${index}.bin`)) {
        throw new Error(`${profileName} 的分卷名称与安装程序不匹配，或存在缺失的分卷。`);
      }
    }
  }
  return value;
}

/**
 * 把文件字节数转换为便于用户判断下载体积的单位。
 * @param {number} bytes 文件的实际字节数。
 * @returns {string} 使用 MiB 或 GiB 的文件大小。
 */
export function formatFileSize(bytes) {
  const unitSize = bytes >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2;
  const unitName = bytes >= 1024 ** 3 ? "GiB" : "MiB";
  return `${(bytes / unitSize).toFixed(unitName === "GiB" ? 2 : 1)} ${unitName}`;
}
