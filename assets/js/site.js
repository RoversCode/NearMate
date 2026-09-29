import { formatFileSize, validateReleaseManifest } from "./release-manifest.js";

const menuButton = document.querySelector(".menu-toggle");
const navigation = document.querySelector("#site-nav");
const profileButtons = document.querySelectorAll("[data-profile]");
const profilePanels = document.querySelectorAll("[data-download-panel]");

document.documentElement.classList.add("js");
document.querySelector("[data-year]").textContent = String(new Date().getFullYear());

/**
 * 同步移动端导航的显示状态和读屏提示。
 * @param {boolean} isOpen 是否展开导航。
 * @returns {void}
 */
function setMenuOpen(isOpen) {
  menuButton.setAttribute("aria-expanded", String(isOpen));
  menuButton.setAttribute("aria-label", isOpen ? "关闭导航" : "打开导航");
  navigation.classList.toggle("is-open", isOpen);
}

menuButton.addEventListener("click", () => {
  setMenuOpen(menuButton.getAttribute("aria-expanded") !== "true");
});
navigation.addEventListener("click", (event) => {
  if (event.target.closest("a")) setMenuOpen(false);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && menuButton.getAttribute("aria-expanded") === "true") {
    setMenuOpen(false);
    menuButton.focus();
  }
});
document.addEventListener("click", (event) => {
  if (!event.target.closest(".header-inner")) setMenuOpen(false);
});
window.matchMedia("(min-width: 601px)").addEventListener("change", () => setMenuOpen(false));

for (const button of profileButtons) {
  button.addEventListener("click", () => {
    const selectedProfile = button.dataset.profile;
    for (const choice of profileButtons) {
      const isSelected = choice === button;
      choice.setAttribute("aria-pressed", String(isSelected));
      choice.classList.toggle("is-selected", isSelected);
    }
    for (const panel of profilePanels) {
      panel.hidden = panel.dataset.downloadPanel !== selectedProfile;
    }
  });
}

/**
 * 读取已核对的发布清单，为每个分卷建立一个明确的下载入口。
 * @returns {Promise<void>} 下载区更新完成后结束。
 */
async function loadReleaseDownloads() {
  const abortController = new AbortController();
  const timeoutId = window.setTimeout(() => abortController.abort(), 10000);
  try {
    const response = await fetch("./data/release.json", {
      cache: "no-cache",
      signal: abortController.signal,
    });
    if (!response.ok) throw new Error("发行清单请求失败。");
    const release = validateReleaseManifest(await response.json());
    const versionBadge = document.querySelector("[data-release-version]");
    versionBadge.textContent = `v${release.version} · ${release.status === "published" ? "现已发布" : "发布准备中"}`;

    if (release.status !== "published") {
      for (const status of document.querySelectorAll("[data-profile-status]")) {
        status.textContent = "安装包正在准备中。发布后可在这里逐个下载，也可通过 GitHub Releases 查看版本记录。";
      }
      return;
    }

    for (const [profileName, profile] of Object.entries(release.profiles)) {
      const fileList = document.querySelector(`[data-assets="${profileName}"]`);
      const status = document.querySelector(`[data-profile-status="${profileName}"]`);
      const totalSize = profile.assets.reduce((total, asset) => total + asset.size, 0);
      status.textContent = `${profile.assets.length} 个文件 · 共 ${formatFileSize(totalSize)} · ${release.publishedAt} 发布`;

      // 每次点击只下载一个文件，避免浏览器拦截自动多文件下载。
      for (const asset of profile.assets) {
        const item = document.createElement("li");
        const download = document.createElement("a");
        download.className = "asset-download";
        download.href = asset.url;
        download.setAttribute("download", asset.name);
        download.setAttribute("aria-label", `下载 ${asset.name}，${formatFileSize(asset.size)}`);

        const filename = document.createElement("span");
        filename.className = "asset-name";
        filename.textContent = asset.name;
        const metadata = document.createElement("span");
        metadata.className = "asset-meta";
        metadata.textContent = formatFileSize(asset.size);
        const downloadLabel = document.createElement("span");
        downloadLabel.textContent = "下载 ↓";
        metadata.append(downloadLabel);
        download.append(filename, metadata);
        item.append(download);
        fileList.append(item);
      }
    }
    const checksumLink = document.querySelector("[data-checksums]");
    checksumLink.href = release.checksumsUrl;
    checksumLink.setAttribute("download", "SHA256SUMS.txt");
    checksumLink.hidden = false;
  } catch {
    // 网络失败或清单错误时保留可信的 Releases 入口，不猜测安装包地址。
    for (const status of document.querySelectorAll("[data-profile-status]")) {
      status.textContent = "暂时无法读取下载信息，请通过 GitHub Releases 查看可用版本。";
    }
  } finally {
    window.clearTimeout(timeoutId);
  }
}

void loadReleaseDownloads();
