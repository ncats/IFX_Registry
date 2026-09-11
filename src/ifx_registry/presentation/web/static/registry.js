"use strict";

function initializeAsyncForms(root = document) {
    root.querySelectorAll(".js-async-form:not([data-ready])").forEach((form) => {
        form.dataset.ready = "true";
        form.addEventListener("submit", async (event) => {
            event.preventDefault();
            const button = form.querySelector("button[type='submit']");
            const originalLabel = button ? button.textContent : "";
            let requestStarted = false;
            if (button) {
                button.disabled = true;
                button.textContent = form.dataset.workingLabel || "Working…";
            }
            form.closest("[aria-live]")?.setAttribute("aria-busy", "true");

            try {
                const response = await fetch(form.action, {
                    method: form.method || "POST",
                    body: new FormData(form),
                    headers: {"X-Requested-With": "IFX-Registry"},
                });
                const html = await response.text();
                const target = document.querySelector(form.dataset.target);
                if (!target) throw new Error("The page could not find the update target.");

                if (form.dataset.mode === "prepend") {
                    target.querySelector(".empty-state")?.remove();
                    target.insertAdjacentHTML("afterbegin", html);
                    const activityPanel = target.closest("#registration-panel");
                    if (activityPanel) {
                        activityPanel.hidden = false;
                        activityPanel.closest(".catalog-layout")?.classList.add("has-activity");
                    }
                    initializePage(target);
                    if (response.ok) {
                        requestStarted = true;
                        // The returned job card updates the activity rail, but the source
                        // row is rendered separately. Reload once so that it immediately
                        // reflects the persisted queued/running job instead of retaining
                        // the action that started the registration.
                        window.location.reload();
                        return;
                    }
                } else {
                    target.insertAdjacentHTML("afterend", html.trim());
                    const replacement = target.nextElementSibling;
                    if (!replacement) throw new Error("The server returned an empty update.");
                    target.remove();
                    initializePage(document);
                    replacement.setAttribute("aria-busy", "false");
                    if (form.hasAttribute("data-focus-result")) {
                        const result = replacement.querySelector(".status, button");
                        if (result) {
                            result.setAttribute("tabindex", "-1");
                            result.focus();
                        }
                    }
                }
            } catch (error) {
                window.alert(`Registry request failed: ${error}`);
            } finally {
                form.closest("[aria-live]")?.setAttribute("aria-busy", "false");
                if (button && button.isConnected && !requestStarted) {
                    button.disabled = false;
                    button.textContent = originalLabel;
                } else if (button && button.isConnected) {
                    button.textContent = "Download in progress";
                }
            }
        });
    });
}

function initializeJobPolling(root = document) {
    root.querySelectorAll("[data-job-poll]:not([data-polling])").forEach((card) => {
        card.dataset.polling = "true";
        window.setTimeout(async () => {
            if (!card.isConnected) return;
            try {
                const response = await fetch(card.dataset.jobPoll, {
                    headers: {"X-Requested-With": "IFX-Registry"},
                });
                const html = await response.text();
                card.insertAdjacentHTML("afterend", html.trim());
                const replacement = card.nextElementSibling;
                if (!replacement) throw new Error("Empty job update");
                card.remove();
                const succeeded = replacement.classList.contains("status-succeeded");
                const failed = replacement.classList.contains("status-failed");
                if ((succeeded || failed) && replacement.matches("[data-build-job]")) {
                    window.location.reload();
                    return;
                }
                if (((succeeded || failed)
                        && (document.querySelector("#catalog-table")
                            || document.querySelector("#available-source-table")))
                    || ((succeeded || failed) && document.querySelector("#dataset-update"))) {
                    window.location.reload();
                    return;
                }
                initializePage(document);
            } catch (error) {
                card.dataset.polling = "";
                window.setTimeout(() => initializeJobPolling(card.parentElement || document), 5000);
            }
        }, 1500);
    });
}

function applySourceFilter() {
    const input = document.querySelector("#source-search");
    if (!input) return;
    const query = input.value.trim().toLocaleLowerCase();
    const rows = [...document.querySelectorAll("[data-source-row]")];
    let visible = 0;
    rows.forEach((row) => {
        const matches = !query || row.dataset.search.toLocaleLowerCase().includes(query);
        row.hidden = !matches;
        if (matches) visible += 1;
    });
    const count = document.querySelector("#source-search-count");
    if (count) count.textContent = `${visible} ${visible === 1 ? "source" : "sources"}`;
    const empty = document.querySelector("#source-search-empty");
    if (empty) empty.hidden = visible !== 0;
}

function initializeSourceSearch(root = document) {
    const input = root.querySelector("#source-search") || document.querySelector("#source-search");
    if (!input) return;
    if (!input.dataset.ready) {
        input.dataset.ready = "true";
        input.addEventListener("input", applySourceFilter);
        input.addEventListener("search", applySourceFilter);
    }
    applySourceFilter();
}

function initializeCatalogSearch(root = document) {
    const input = root.querySelector("#catalog-search") || document.querySelector("#catalog-search");
    if (!input || input.dataset.ready) return;
    input.dataset.ready = "true";
    let activeKind = "all";
    const filter = () => {
        const query = input.value.trim().toLocaleLowerCase();
        const rows = [...document.querySelectorAll("[data-catalog-row]")];
        let visible = 0;
        rows.forEach((row) => {
            const matchesSearch = !query || row.dataset.search.toLocaleLowerCase().includes(query);
            const matchesKind = activeKind === "all" || row.dataset.kind === activeKind;
            const matches = matchesSearch && matchesKind;
            row.hidden = !matches;
            if (matches) visible += 1;
        });
        const count = document.querySelector("#catalog-search-count");
        if (count) count.textContent = `${visible} ${visible === 1 ? "dataset" : "datasets"}`;
        const empty = document.querySelector("#catalog-search-empty");
        if (empty) empty.hidden = visible !== 0;
    };
    input.addEventListener("input", filter);
    input.addEventListener("search", filter);
    document.querySelectorAll("[data-kind-filter]").forEach((button) => {
        button.addEventListener("click", () => {
            activeKind = button.dataset.kindFilter;
            document.querySelectorAll("[data-kind-filter]").forEach((candidate) => {
                const selected = candidate === button;
                candidate.classList.toggle("is-active", selected);
                candidate.setAttribute("aria-pressed", selected ? "true" : "false");
            });
            filter();
        });
    });
    document.querySelector("#catalog-clear-filter")?.addEventListener("click", () => {
        input.value = "";
        activeKind = "all";
        document.querySelectorAll("[data-kind-filter]").forEach((candidate) => {
            const selected = candidate.dataset.kindFilter === "all";
            candidate.classList.toggle("is-active", selected);
            candidate.setAttribute("aria-pressed", selected ? "true" : "false");
        });
        input.focus();
        filter();
    });
    filter();
}

function initializeLineageMaps(root = document) {
    root.querySelectorAll("[data-lineage-map]:not([data-ready])").forEach((map) => {
        map.dataset.ready = "true";
        const svg = map.querySelector("[data-lineage-connectors]");
        if (!svg) return;

        const draw = () => {
            svg.replaceChildren();
            const rows = [...map.querySelectorAll("[data-lineage-node]")];
            const rowsByKey = new Map(rows.map((row) => [row.dataset.lineageKey, row]));
            const mapRect = map.getBoundingClientRect();
            const width = map.offsetWidth;
            const height = map.offsetHeight;
            svg.setAttribute("width", String(width));
            svg.setAttribute("height", String(height));
            svg.setAttribute("viewBox", `0 0 ${width} ${height}`);

            rows.forEach((target) => {
                const dependencyKeys = (target.dataset.lineageDependencies || "")
                    .split(",")
                    .filter(Boolean);
                dependencyKeys.forEach((sourceKey) => {
                    const source = rowsByKey.get(sourceKey);
                    if (!source) return;
                    const sourceRect = source.getBoundingClientRect();
                    const targetRect = target.getBoundingClientRect();
                    const startX = sourceRect.right - mapRect.left;
                    const startY = sourceRect.top + sourceRect.height / 2 - mapRect.top;
                    const endX = targetRect.left - mapRect.left;
                    const endY = targetRect.top + targetRect.height / 2 - mapRect.top;
                    const bend = Math.max(22, (endX - startX) * .45);
                    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
                    path.setAttribute("d", `M ${startX} ${startY} C ${startX + bend} ${startY}, ${endX - bend} ${endY}, ${endX} ${endY}`);
                    path.setAttribute("class", `lineage-connector${source.hasAttribute("data-lineage-current") || target.hasAttribute("data-lineage-current") ? " is-current" : ""}`);
                    path.dataset.sourceKey = sourceKey;
                    path.dataset.targetKey = target.dataset.lineageKey;
                    svg.append(path);
                });
            });
        };

        const trace = (key) => {
            svg.querySelectorAll(".lineage-connector").forEach((path) => {
                const connected = path.dataset.sourceKey === key || path.dataset.targetKey === key;
                path.classList.toggle("is-tracing", connected);
                path.classList.toggle("is-muted", !connected);
            });
        };
        const clearTrace = () => {
            svg.querySelectorAll(".lineage-connector").forEach((path) => {
                path.classList.remove("is-tracing", "is-muted");
            });
        };
        map.querySelectorAll("[data-lineage-node]").forEach((row) => {
            row.addEventListener("pointerenter", () => trace(row.dataset.lineageKey));
            row.addEventListener("pointerleave", clearTrace);
            row.addEventListener("focusin", () => trace(row.dataset.lineageKey));
            row.addEventListener("focusout", clearTrace);
        });

        window.requestAnimationFrame(draw);
        if ("ResizeObserver" in window) new ResizeObserver(draw).observe(map);
        else window.addEventListener("resize", draw);
    });
}

function initializeDerivedBuildPreviews(root = document) {
    root.querySelectorAll("[data-build-preview-url]:not([data-preview-ready])").forEach((form) => {
        form.dataset.previewReady = "true";
        let requestNumber = 0;
        let controller = null;

        const update = async () => {
            const preview = form.querySelector("#derived-build-preview");
            if (!preview) return;
            const currentRequest = ++requestNumber;
            controller?.abort();
            controller = new AbortController();
            preview.setAttribute("aria-busy", "true");
            preview.querySelector("button[type='submit']")?.setAttribute("disabled", "");
            const status = document.createElement("span");
            status.className = "build-preview-calculating";
            status.textContent = "Calculating version…";
            preview.prepend(status);

            try {
                const response = await fetch(form.dataset.buildPreviewUrl, {
                    method: "POST",
                    body: new FormData(form),
                    headers: {"X-Requested-With": "IFX-Registry"},
                    signal: controller.signal,
                });
                const html = await response.text();
                if (currentRequest !== requestNumber) return;
                preview.insertAdjacentHTML("afterend", html.trim());
                const replacement = preview.nextElementSibling;
                if (!replacement) throw new Error("The server returned an empty preview.");
                preview.remove();
                replacement.setAttribute("aria-busy", "false");
            } catch (error) {
                if (error.name === "AbortError" || currentRequest !== requestNumber) return;
                preview.replaceChildren();
                const message = document.createElement("p");
                message.className = "inline-message error";
                message.setAttribute("role", "alert");
                message.textContent = `Could not calculate the Registry version: ${error.message}`;
                preview.append(message);
                preview.setAttribute("aria-busy", "false");
            }
        };

        form.querySelectorAll("select[name^='input.']").forEach((select) => {
            select.addEventListener("change", update);
        });
    });
}

function initializePage(root = document) {
    initializeAsyncForms(root);
    initializeJobPolling(root);
    initializeSourceSearch(root);
    initializeCatalogSearch(root);
    initializeLineageMaps(root);
    initializeDerivedBuildPreviews(root);
}

document.addEventListener("DOMContentLoaded", () => initializePage());
