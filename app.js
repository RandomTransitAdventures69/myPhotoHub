(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];

  $$("[data-open-modal]").forEach(btn => btn.addEventListener("click", () => {
    const modal = document.getElementById(btn.dataset.openModal);
    if (modal) { modal.hidden = false; const input = $("input:not([type=file])", modal); if (input) setTimeout(() => input.focus(), 40); }
  }));
  $$("[data-close-modal]").forEach(btn => btn.addEventListener("click", () => btn.closest(".modal-backdrop").hidden = true));
  $$(".modal-backdrop").forEach(backdrop => backdrop.addEventListener("click", e => { if (e.target === backdrop) backdrop.hidden = true; }));
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      $$(".modal-backdrop, .lightbox-backdrop").forEach(el => el.hidden = true);
    }
  });

  const fileInput = $("#file-input");
  const uploadForm = $("#upload-form");
  const uploadButton = $("#upload-submit");
  const progressWrap = $("#upload-progress-wrap");
  const progressBar = $("#upload-progress");
  const progressText = $("#upload-progress-text");
  const progressPercent = $("#upload-progress-percent");
  const progressDetail = $("#upload-progress-detail");
  if (fileInput) fileInput.addEventListener("change", () => {
    const count = fileInput.files.length;
    const label = $("#file-count");
    const totalBytes = [...fileInput.files].reduce((sum, f) => sum + f.size, 0);
    const totalMB = Math.max(1, Math.round(totalBytes / (1024 * 1024)));
    if (label && count) label.textContent = `${count.toLocaleString()} photo${count === 1 ? "" : "s"} selected · about ${totalMB.toLocaleString()} MB`;
  });

  // Keep each request comfortably below Flask's 512 MB hard cap. The same
  // selected File objects are reused, so users don't have to reselect batches.
  if (uploadForm && fileInput) uploadForm.addEventListener("submit", async event => {
    event.preventDefault();
    const files = [...fileInput.files];
    if (!files.length) return;
    const maxBatchBytes = 96 * 1024 * 1024;
    const maxBatchFiles = 40;
    const batches = [];
    let batch = [], batchBytes = 0;
    for (const file of files) {
      if (batch.length && (batch.length >= maxBatchFiles || batchBytes + file.size > maxBatchBytes)) {
        batches.push(batch); batch = []; batchBytes = 0;
      }
      batch.push(file); batchBytes += file.size;
      // A single unusually large file is sent alone; Flask still enforces its cap.
      if (file.size > maxBatchBytes) { batches.push(batch); batch = []; batchBytes = 0; }
    }
    if (batch.length) batches.push(batch);
    const allBytes = files.reduce((sum, file) => sum + file.size, 0);
    let finishedFiles = 0, finishedBytes = 0, uploaded = 0, failed = 0;
    const errors = [];
    progressWrap.hidden = false;
    uploadButton.disabled = true;
    fileInput.disabled = true;
    uploadButton.textContent = "Uploading…";
    const formatBytes = bytes => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${Math.round(bytes / (1024 ** 2))} MB`;
    try {
      for (let i = 0; i < batches.length; i++) {
        const group = batches[i];
        const form = new FormData();
        group.forEach(file => form.append("photos", file, file.name));
        progressText.textContent = `Uploading batch ${i + 1} of ${batches.length}`;
        progressDetail.textContent = `${finishedFiles.toLocaleString()} of ${files.length.toLocaleString()} files completed · ${formatBytes(finishedBytes)} of ${formatBytes(allBytes)} sent`;
        const response = await fetch(uploadForm.action, { method: "POST", body: form, headers: { "X-Stillroom-Batch": "1" } });
        let result = {};
        try { result = await response.json(); } catch { /* server returned a non-JSON error */ }
        if (!response.ok && response.status !== 207) {
          throw new Error(response.status === 413 ? "A batch exceeded the server upload limit. Try smaller batches." : (result.error || `Server returned ${response.status}.`));
        }
        uploaded += result.saved || 0;
        const batchErrors = Array.isArray(result.errors) ? result.errors : [];
        failed += batchErrors.length;
        errors.push(...batchErrors.slice(0, 10));
        finishedFiles += group.length;
        finishedBytes += group.reduce((sum, file) => sum + file.size, 0);
        const percent = Math.round((finishedFiles / files.length) * 100);
        progressBar.value = percent;
        progressPercent.textContent = `${percent}%`;
        progressDetail.textContent = `${uploaded.toLocaleString()} saved · ${failed.toLocaleString()} failed · ${finishedFiles.toLocaleString()} of ${files.length.toLocaleString()} files processed`;
      }
      progressText.textContent = "Upload complete";
      progressDetail.textContent = `${uploaded.toLocaleString()} photos saved${failed ? ` · ${failed.toLocaleString()} failed` : ""}. Refreshing your library…`;
      if (errors.length) console.warn("Stillroom upload errors:", errors);
      window.location.reload();
    } catch (error) {
      progressText.textContent = "Upload paused";
      progressDetail.textContent = `${uploaded.toLocaleString()} photos saved so far. ${error.message} Your already-uploaded photos are safe; you can select the remaining photos again.`;
      progressPercent.textContent = `${Math.round((finishedFiles / files.length) * 100)}%`;
      progressBar.value = Math.round((finishedFiles / files.length) * 100);
      alert(`Stillroom upload stopped after ${uploaded} photos were saved.
${error.message}

Already uploaded photos will remain in your library.`);
    } finally {
      uploadButton.disabled = false;
      fileInput.disabled = false;
      uploadButton.textContent = "Upload to Stillroom";
    }
  });

  $$("[data-favorite]").forEach(btn => btn.addEventListener("click", async e => {
    e.stopPropagation();
    try {
      const response = await fetch(`/photo/${btn.dataset.favorite}/favorite`, { method: "POST" });
      if (!response.ok) throw new Error();
      const data = await response.json();
      btn.classList.toggle("is-favorite", data.favorite);
      btn.textContent = data.favorite ? "♥" : "♡";
      if (location.search.includes("view=favorites") && !data.favorite) btn.closest(".photo-card").remove();
    } catch { alert("Couldn't update favorite. Please refresh and try again."); }
  }));

  const cards = $$(".photo-card[data-photo-id]");
  const lightbox = $("#lightbox");
  let activeIndex = -1;
  async function openPhoto(id) {
    activeIndex = cards.findIndex(c => c.dataset.photoId === id);
    if (activeIndex < 0 || !lightbox) return;
    const card = cards[activeIndex];
    const response = await fetch(`/photo/${id}`);
    if (!response.ok) return;
    const p = await response.json();
    $("#lightbox-image").src = `/media/${encodeURIComponent(p.id)}`;
    $("#lightbox-image").alt = p.title;
    $("#lightbox-title").textContent = p.title;
    $("#lightbox-meta").textContent = `${p.date_label || "Recently added"} · ${p.width || "?"} × ${p.height || "?"}`;
    $("#edit-title").value = p.title;
    $("#detail-original").textContent = p.original_name;
    $("#detail-dimensions").textContent = `${p.width || "?"} × ${p.height || "?"}`;
    $("#download-original").href = `/media/${encodeURIComponent(p.id)}`;
    $("#download-original").setAttribute("download", p.original_name);
    $("#delete-form").action = `/photo/${id}/delete`;
    const fav = $("#lightbox-favorite");
    fav.textContent = p.favorite ? "♥ Favorited" : "♡ Favorite";
    fav.classList.toggle("is-favorite", !!p.favorite);
    fav.onclick = async () => {
      const r = await fetch(`/photo/${id}/favorite`, { method: "POST" });
      if (r.ok) { const d = await r.json(); fav.textContent = d.favorite ? "♥ Favorited" : "♡ Favorite"; fav.classList.toggle("is-favorite", d.favorite); const b = $(`[data-favorite="${id}"]`); if (b) { b.textContent = d.favorite ? "♥" : "♡"; b.classList.toggle("is-favorite", d.favorite); } }
    };
    lightbox.hidden = false;
  }
  $$("[data-open-photo]").forEach(btn => btn.addEventListener("click", () => openPhoto(btn.dataset.openPhoto)));
  $$("[data-close-lightbox]").forEach(btn => btn.addEventListener("click", () => lightbox.hidden = true));
  if (lightbox) lightbox.addEventListener("click", e => { if (e.target === lightbox) lightbox.hidden = true; });
  const step = delta => { if (cards.length) { const next = (activeIndex + delta + cards.length) % cards.length; openPhoto(cards[next].dataset.photoId); } };
  const prev = $("[data-prev]"), next = $("[data-next]");
  if (prev) prev.addEventListener("click", () => step(-1));
  if (next) next.addEventListener("click", () => step(1));
  document.addEventListener("keydown", e => {
    if (!lightbox || lightbox.hidden) return;
    if (e.key === "ArrowLeft") step(-1);
    if (e.key === "ArrowRight") step(1);
  });
  const saveTitle = $("#save-title");
  if (saveTitle) saveTitle.addEventListener("click", async () => {
    const id = cards[activeIndex]?.dataset.photoId;
    if (!id) return;
    const form = new FormData(); form.set("title", $("#edit-title").value);
    const response = await fetch(`/photo/${id}/title`, { method: "POST", body: form });
    const data = await response.json();
    if (!response.ok) { alert(data.error || "Couldn't save title."); return; }
    $("#lightbox-title").textContent = data.title;
    const card = cards[activeIndex];
    if (card) { card.dataset.title = data.title; $(".photo-title", card).textContent = data.title; }
  });
})();
