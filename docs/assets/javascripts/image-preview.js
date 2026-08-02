(() => {
  const previewImages = document.querySelectorAll("figure.local-ui-shot img");

  if (!previewImages.length) return;

  const dialog = document.createElement("dialog");
  dialog.className = "pf-image-preview";
  dialog.setAttribute("aria-label", "功能截图放大预览");
  dialog.innerHTML = `
    <div class="pf-image-preview__shell">
      <div class="pf-image-preview__toolbar">
        <p class="pf-image-preview__title"></p>
        <div class="pf-image-preview__actions">
          <button type="button" data-action="zoom-out" aria-label="缩小图片">−</button>
          <button type="button" data-action="reset" aria-label="恢复适合窗口大小">适合窗口</button>
          <span class="pf-image-preview__zoom" aria-live="polite">100%</span>
          <button type="button" data-action="zoom-in" aria-label="放大图片">＋</button>
          <button type="button" data-action="close" aria-label="关闭图片预览">关闭</button>
        </div>
      </div>
      <div class="pf-image-preview__viewport">
        <img alt="">
      </div>
    </div>
  `;
  document.body.append(dialog);

  const title = dialog.querySelector(".pf-image-preview__title");
  const viewport = dialog.querySelector(".pf-image-preview__viewport");
  const preview = dialog.querySelector("img");
  const zoomLabel = dialog.querySelector(".pf-image-preview__zoom");
  const zoomSteps = [1, 1.25, 1.5, 2, 3];
  let zoomIndex = 0;

  const fitWidth = () => Math.min(preview.naturalWidth, Math.max(280, dialog.clientWidth - 32));

  const renderZoom = () => {
    const zoom = zoomSteps[zoomIndex];
    preview.style.width = `${Math.round(fitWidth() * zoom)}px`;
    zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
  };

  const openPreview = (source) => {
    zoomIndex = 0;
    preview.src = source.currentSrc || source.src;
    preview.alt = source.alt;
    title.textContent = source.alt || "功能截图";
    preview.onload = renderZoom;
    dialog.showModal();
    renderZoom();
    viewport.scrollTo({ left: 0, top: 0 });
  };

  previewImages.forEach((image) => {
    image.tabIndex = 0;
    image.setAttribute("role", "button");
    image.setAttribute("aria-label", `放大预览：${image.alt || "功能截图"}`);
    image.addEventListener("click", () => openPreview(image));
    image.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openPreview(image);
      }
    });
  });

  dialog.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) {
      if (event.target === dialog) dialog.close();
      return;
    }

    const action = button.dataset.action;
    if (action === "close") {
      dialog.close();
      return;
    }
    if (action === "reset") zoomIndex = 0;
    if (action === "zoom-out") zoomIndex = Math.max(0, zoomIndex - 1);
    if (action === "zoom-in") zoomIndex = Math.min(zoomSteps.length - 1, zoomIndex + 1);
    renderZoom();
  });

  window.addEventListener("resize", () => {
    if (dialog.open) renderZoom();
  });
})();
