/* Spice Town — scanner UI logic. Talks to the JSON API from earlier stages. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const scannerCard = $("scanner-card");
  const resultCard = $("result-card");
  const suggestCard = $("suggest-card");
  const customCard = $("custom-card");
  const priceChangesCard = $("price-changes-card");
  const printHistoryCard = $("print-history-card");
  const viewport = $("viewport");

  let scanning = false;
  let lastCode = null;
  let lastAt = 0;
  let currentUpc = null;
  let currentProductId = null; // pins the exact product on shared barcodes

  // ── helpers ────────────────────────────────────────────────────────────
  function toast(msg, ms = 2200) {
    const t = $("toast");
    t.textContent = msg;
    t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => (t.hidden = true), ms);
  }
  function show(card) {
    for (const c of [scannerCard, resultCard, suggestCard, customCard, priceChangesCard, printHistoryCard])
      c.hidden = c !== card;
  }
  function money(v) {
    return v == null ? "" : "$" + Number(v).toFixed(2);
  }
  async function getJSON(url, opts) {
    const r = await fetch(url, opts);
    let body = null;
    try { body = await r.json(); } catch (_) {}
    return { ok: r.ok, status: r.status, body };
  }

  // ── product view ───────────────────────────────────────────────────────
  function renderProduct(p) {
    currentUpc = p.upc;
    currentProductId = p.id != null ? p.id : null;
    const variant = p.label_variant || "standard";
    const wasPrice =
      (p.on_sale || p.clearance) && p.sale_price != null
        ? `<span class="was">${money(p.price)}</span>`
        : "";
    $("product-info").innerHTML = `
      <h2>${escapeHtml(p.name)}
        <span class="variant-tag variant-${variant}">${variant}</span></h2>
      <div class="meta">${escapeHtml([p.department, p.size, p.unit].filter(Boolean).join(" · "))}</div>
      <div><span class="price">${money(p.effective_price ?? p.price)}</span>${wasPrice}</div>
      <div class="meta">UPC ${escapeHtml(p.upc)}</div>`;
    refreshPreview();
    $("print-status").textContent = "";
    $("print-status").className = "print-status";
    show(resultCard);
  }

  // Selected label fields; null when everything is checked (= server default).
  function selectedFields() {
    const boxes = document.querySelectorAll("#field-toggles input[type=checkbox]");
    const picked = [];
    for (const b of boxes) if (b.checked) picked.push(b.dataset.field);
    return picked.length === boxes.length ? null : picked;
  }

  function refreshPreview() {
    const v = $("variant-select").value;
    const params = new URLSearchParams();
    if (currentProductId != null) params.set("id", currentProductId);
    if (v) params.set("variant", v);
    const fields = selectedFields();
    if (fields) params.set("fields", fields.join(","));
    params.set("t", Date.now());
    $("label-preview").src =
      `/api/preview/${encodeURIComponent(currentUpc)}.png?` + params.toString();
  }

  // ── lookup flow ────────────────────────────────────────────────────────
  async function lookup(code) {
    code = (code || "").trim();
    if (!code) return;
    toast("Looking up " + code + "…", 1200);
    const { ok, status, body } = await getJSON(
      `/api/lookup/${encodeURIComponent(code)}`
    );
    if (ok && body && body.found) {
      if (body.multiple && body.products && body.products.length > 1) {
        // Shared barcode (e.g. "XYZ" vs "XYZ B1G1") → let staff pick.
        renderSuggestions(
          code,
          body.products.map((p) => ({ product: p })),
          200,
          `${body.products.length} products share this barcode`
        );
        return;
      }
      renderProduct(body.product);
      return;
    }
    // not found → suggestions
    const suggestions = (body && body.suggestions) || [];
    renderSuggestions(code, suggestions, status);
  }

  function renderSuggestions(query, suggestions, status, title) {
    $("suggest-title").textContent =
      title || (status === 404 ? `No match for “${query}”` : "Pick a product");
    const list = $("suggest-list");
    list.innerHTML = "";
    if (!suggestions.length) {
      list.innerHTML = `<p class="hint">No similar items found. Try typing part of the name.</p>`;
    }
    for (const s of suggestions) {
      const p = s.product;
      const div = document.createElement("button");
      div.className = "suggest-item";
      const match = s.score != null ? ` · match ${Math.round(s.score)}%` : "";
      div.innerHTML = `<div class="s-name">${escapeHtml(p.name)}</div>
        <div class="s-meta">${money(p.effective_price ?? p.price)} · UPC ${escapeHtml(
        p.upc
      )}${match}</div>`;
      div.onclick = () => renderProduct(p);
      list.appendChild(div);
    }
    show(suggestCard);
  }

  async function search(query) {
    query = (query || "").trim();
    if (!query) return;
    const { body } = await getJSON(`/api/search?q=${encodeURIComponent(query)}`);
    const results = (body && body.results) || [];
    renderSuggestions(query, results, 200);
  }

  // ── live (as-you-type) search ──────────────────────────────────────────
  // Debounced fuzzy search under the input; picking a result opens it.
  let liveTimer = null;
  let liveSeq = 0; // drop out-of-order responses

  function hideLiveResults() {
    const box = $("live-results");
    box.hidden = true;
    box.innerHTML = "";
  }

  async function liveSearch(query) {
    const seq = ++liveSeq;
    const { body } = await getJSON(`/api/search?q=${encodeURIComponent(query)}`);
    if (seq !== liveSeq) return; // a newer keystroke superseded this response
    if ($("manual-input").value.trim() !== query) return;
    const results = (body && body.results) || [];
    const box = $("live-results");
    box.innerHTML = "";
    if (!results.length) {
      box.innerHTML = `<p class="hint">No matches yet — keep typing…</p>`;
      box.hidden = false;
      return;
    }
    for (const s of results.slice(0, 8)) {
      const p = s.product;
      const div = document.createElement("button");
      div.type = "button";
      div.className = "suggest-item";
      div.innerHTML = `<div class="s-name">${escapeHtml(p.name)}</div>
        <div class="s-meta">${money(p.effective_price ?? p.price)} · UPC ${escapeHtml(p.upc)}</div>`;
      div.onclick = () => {
        hideLiveResults();
        $("manual-input").value = "";
        renderProduct(p);
      };
      box.appendChild(div);
    }
    box.hidden = false;
  }

  function onManualInput() {
    const q = $("manual-input").value.trim();
    clearTimeout(liveTimer);
    // Short/empty text or a barcode being typed/scanned in: wait for submit.
    if (q.length < 2 || /^\d+$/.test(q)) {
      hideLiveResults();
      return;
    }
    liveTimer = setTimeout(() => liveSearch(q), 250);
  }

  // ── printing ───────────────────────────────────────────────────────────
  async function doPrint() {
    if (!currentUpc) return;
    const copies = Math.max(1, parseInt($("copies-input").value || "1", 10));
    const variant = $("variant-select").value || undefined;
    const st = $("print-status");
    st.className = "print-status pending";
    st.textContent = "Sending to printer…";
    $("btn-print").disabled = true;

    const { ok, status, body } = await getJSON("/api/print", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upc: currentUpc,
        product_id: currentProductId,
        copies,
        variant,
        fields: selectedFields() || undefined,
        wait: true,
      }),
    });
    $("btn-print").disabled = false;

    if (status === 503) {
      st.className = "print-status err";
      st.textContent = "⚠ Printer/worker not available on the server.";
      return;
    }
    const job = body && body.job;
    if (ok && job && job.status === "done") {
      st.className = "print-status ok";
      st.textContent = `✓ Printed ${copies} label(s).`;
    } else if (job) {
      st.className = "print-status pending";
      st.textContent = `Status: ${job.status}…`;
      if (job.id) pollJob(job.id);
    } else {
      st.className = "print-status err";
      st.textContent = "✗ Print failed.";
    }
  }

  async function pollJob(id, tries = 40) {
    const st = $("print-status");
    for (let i = 0; i < tries; i++) {
      await new Promise((r) => setTimeout(r, 250));
      const { body } = await getJSON(`/api/print/${id}`);
      const job = body && body.job;
      if (!job) continue;
      if (job.status === "done") {
        st.className = "print-status ok";
        st.textContent = "✓ Printed.";
        return;
      }
      if (job.status === "error") {
        st.className = "print-status err";
        st.textContent = "✗ " + (job.error || "Print error");
        return;
      }
    }
    st.className = "print-status pending";
    st.textContent = "Still printing… check the printer.";
  }

  // ── custom label ───────────────────────────────────────────────────────
  async function createCustom(e) {
    e.preventDefault();
    const st = $("custom-status");
    const name = $("c-name").value.trim();
    if (!name) return;
    st.className = "print-status pending";
    st.textContent = "Saving…";
    const { ok, body } = await getJSON("/api/products/custom", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name,
        price: $("c-price").value || 0,
        size: $("c-size").value.trim(),
        department: $("c-dept").value.trim(),
        upc: $("c-upc").value.trim(),
      }),
    });
    if (!ok || !body || !body.product) {
      st.className = "print-status err";
      st.textContent = "✗ " + ((body && body.message) || "Could not save.");
      return;
    }
    st.textContent = "";
    $("custom-form").reset();
    renderProduct(body.product);
  }

  // ── price-change review ───────────────────────────────────────────────
  // Every Toast sync appends a PriceHistory row for anything that actually
  // moved; this panel surfaces the unreviewed ones so staff print/dismiss
  // instead of the app silently printing labels on its own.
  async function refreshPriceChangesBanner() {
    const { body } = await getJSON("/api/price-changes");
    const changes = (body && body.changes) || [];
    const btn = $("btn-price-changes");
    if (!changes.length) {
      btn.hidden = true;
      return;
    }
    $("price-changes-count").textContent = changes.length;
    btn.hidden = false;
  }

  function pcRow(c) {
    const up = c.new_price > c.old_price;
    const dir = c.old_price == null ? "" : up ? "up" : "down";
    const was = c.old_price != null
      ? `<span class="was">${money(c.old_price)}</span> → `
      : "";
    const div = document.createElement("label");
    div.className = "pc-row";
    div.innerHTML = `
      <input type="checkbox" class="pc-check" data-id="${c.id}" checked>
      <span class="pc-info">
        <span class="pc-name">${escapeHtml(c.name || c.upc)}</span>
        <span class="pc-meta">UPC ${escapeHtml(c.upc || "")} · ${was}<span class="pc-new ${dir}">${money(c.new_price)}</span></span>
      </span>`;
    return div;
  }

  async function openPriceChanges() {
    show(priceChangesCard);
    $("pc-status").textContent = "";
    const list = $("price-changes-list");
    list.innerHTML = `<p class="hint">Loading…</p>`;
    const { body } = await getJSON("/api/price-changes");
    const changes = (body && body.changes) || [];
    list.innerHTML = "";
    if (!changes.length) {
      list.innerHTML = `<p class="hint">Nothing pending — you're all caught up.</p>`;
      return;
    }
    for (const c of changes) list.appendChild(pcRow(c));
    $("pc-select-all").checked = true;
  }

  function selectedPriceChangeIds() {
    const boxes = document.querySelectorAll("#price-changes-list .pc-check:checked");
    return Array.from(boxes).map((b) => parseInt(b.dataset.id, 10));
  }

  async function printSelectedPriceChanges() {
    const ids = selectedPriceChangeIds();
    const st = $("pc-status");
    if (!ids.length) {
      st.className = "print-status err";
      st.textContent = "Select at least one item.";
      return;
    }
    st.className = "print-status pending";
    st.textContent = `Printing ${ids.length} label(s)…`;
    const { body } = await getJSON("/api/price-changes/print", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    const results = (body && body.results) || [];
    const failed = results.filter((r) => !r.ok);
    st.className = failed.length ? "print-status err" : "print-status ok";
    st.textContent = failed.length
      ? `✓ ${results.length - failed.length} printed, ✗ ${failed.length} failed.`
      : `✓ Printed ${results.length} label(s).`;
    await openPriceChanges();
    await refreshPriceChangesBanner();
  }

  async function dismissSelectedPriceChanges() {
    const ids = selectedPriceChangeIds();
    const st = $("pc-status");
    if (!ids.length) {
      st.className = "print-status err";
      st.textContent = "Select at least one item.";
      return;
    }
    await getJSON("/api/price-changes/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids }),
    });
    st.className = "print-status ok";
    st.textContent = `Dismissed ${ids.length} item(s).`;
    await openPriceChanges();
    await refreshPriceChangesBanner();
  }

  // ── print history ──────────────────────────────────────────────────────
  // Every /api/print and price-change print goes through PrintJob, so this
  // view is just a read of that table (no separate log to keep in sync).
  const STATUS_ICON = { queued: "⏳", printing: "🖨️", done: "✓", error: "✗" };

  function phRow(j) {
    const when = j.completed_at || j.claimed_at || j.created_at;
    const whenStr = when ? new Date(when).toLocaleString() : "";
    const tag = j.reason === "price_change" ? `<span class="pc-tag">price change</span>` : "";
    const icon = STATUS_ICON[j.status] || j.status;
    const div = document.createElement("div");
    div.className = "pc-row ph-row";
    div.innerHTML = `
      <span class="pc-info">
        <span class="pc-name">${icon} ${escapeHtml(j.name || j.upc)} ${tag}</span>
        <span class="pc-meta">UPC ${escapeHtml(j.upc || "")} · ${j.variant || "standard"} × ${j.copies || 1} · ${whenStr}${j.error ? " · " + escapeHtml(j.error) : ""}</span>
      </span>
      <button class="btn ph-reprint" ${j.product_id ? "" : "disabled title=\"product no longer in catalog\""}>🖨️ Reprint</button>`;
    if (j.product_id) {
      div.querySelector(".ph-reprint").onclick = () => reprintJob(j);
    }
    return div;
  }

  async function reprintJob(j) {
    toast(`Reprinting ${j.name || j.upc}…`, 4000);
    const { ok, body } = await getJSON("/api/print", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        product_id: j.product_id,
        variant: j.variant || undefined,
        copies: j.copies || 1,
        fields: j.fields ? j.fields.split(",") : undefined,
      }),
    });
    toast(ok ? `✓ Reprint queued for ${j.name || j.upc}.` : `✗ Reprint failed: ${(body && body.message) || "error"}`);
    await refreshPrintHistory();
  }

  async function openPrintHistory() {
    show(printHistoryCard);
    await refreshPrintHistory();
  }

  async function refreshPrintHistory() {
    const list = $("print-history-list");
    list.innerHTML = `<p class="hint">Loading…</p>`;
    const reason = $("ph-filter").value;
    const qs = reason ? `?reason=${encodeURIComponent(reason)}` : "";
    const { body } = await getJSON(`/api/print-history${qs}`);
    const jobs = (body && body.jobs) || [];
    list.innerHTML = "";
    if (!jobs.length) {
      list.innerHTML = `<p class="hint">No print jobs yet.</p>`;
      return;
    }
    for (const j of jobs) list.appendChild(phRow(j));
  }

  // ── camera (QuaggaJS) ──────────────────────────────────────────────────
  function startScanner() {
    if (scanning) return;
    if (typeof Quagga === "undefined") {
      toast("Camera library not loaded — use manual search.");
      return;
    }
    Quagga.init(
      {
        inputStream: {
          type: "LiveStream",
          target: viewport,
          constraints: {
            facingMode: "environment",
            // Higher resolution dramatically improves decode accuracy on
            // small/curved grocery barcodes.
            width: { ideal: 1280 },
            height: { ideal: 720 },
          },
        },
        locator: { patchSize: "medium", halfSample: true },
        decoder: {
          readers: ["upc_reader", "upc_e_reader", "ean_reader", "ean_8_reader", "code_128_reader"],
        },
        locate: true,
        frequency: 10,
      },
      (err) => {
        if (err) {
          console.error(err);
          toast("Camera unavailable (needs HTTPS or permission). Use manual search.");
          return;
        }
        Quagga.start();
        scanning = true;
        $("btn-start").hidden = true;
        $("btn-stop").hidden = false;
        $("scan-hint").textContent = "Scanning… hold steady over a barcode.";
      }
    );
    Quagga.offDetected(onDetected);
    Quagga.onDetected(onDetected);
  }

  // ── scan validation ──────────────────────────────────────────────────────
  // A single video frame routinely mis-decodes (wrong digits, phantom codes).
  // Accept a code only when: (1) the decoder's own error score is low,
  // (2) the SAME code is read in several frames within a short window, and
  // (3) numeric UPC/EAN codes pass their checksum digit.
  const MAX_DECODE_ERROR = 0.16; // mean per-digit error; >0.16 is unreliable
  const CONFIRMATIONS_NEEDED = 3; // identical reads required
  const CONFIRM_WINDOW_MS = 1800; // ...within this window
  let readVotes = {}; // code -> [timestamps]

  function decodeQualityOk(res) {
    const codes = res.codeResult && res.codeResult.decodedCodes;
    if (!codes || !codes.length) return true; // no scores available → let votes decide
    let sum = 0, n = 0;
    for (const d of codes) {
      if (d.error !== undefined) { sum += d.error; n++; }
    }
    return n === 0 || sum / n < MAX_DECODE_ERROR;
  }

  function gtinChecksumOk(code) {
    // Validate UPC-A / EAN-8 / EAN-13 / GTIN-14 check digit. Non-numeric or
    // other-length codes (our Code128 TG-… labels) skip this test.
    if (!/^\d+$/.test(code)) return true;
    if (![8, 12, 13, 14].includes(code.length)) return true;
    const digits = code.split("").map(Number);
    const check = digits.pop();
    let sum = 0;
    // From the rightmost payload digit leftwards: weights 3,1,3,1,…
    digits.reverse().forEach((d, i) => { sum += d * (i % 2 === 0 ? 3 : 1); });
    return (10 - (sum % 10)) % 10 === check;
  }

  function onDetected(res) {
    const code = res && res.codeResult && res.codeResult.code;
    if (!code) return;
    const now = Date.now();
    // debounce a code we already accepted moments ago
    if (code === lastCode && now - lastAt < 2500) return;

    if (!decodeQualityOk(res)) return; // noisy frame → ignore
    if (!gtinChecksumOk(code)) return; // impossible UPC/EAN → ignore

    // Vote: require the same code in several frames before trusting it.
    const votes = (readVotes[code] = (readVotes[code] || []).filter(
      (t) => now - t < CONFIRM_WINDOW_MS
    ));
    votes.push(now);
    if (votes.length < CONFIRMATIONS_NEEDED) return;

    readVotes = {};
    lastCode = code;
    lastAt = now;
    if (navigator.vibrate) navigator.vibrate(60);
    stopScanner();
    lookup(code);
  }

  function stopScanner() {
    if (!scanning) return;
    try { Quagga.stop(); } catch (_) {}
    scanning = false;
    $("btn-start").hidden = false;
    $("btn-stop").hidden = true;
    $("scan-hint").textContent = "Point the camera at a barcode, or type above.";
  }

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
    );
  }

  // ── wire up ────────────────────────────────────────────────────────────
  $("btn-start").onclick = startScanner;
  $("btn-stop").onclick = stopScanner;
  $("btn-print").onclick = doPrint;
  $("variant-select").onchange = refreshPreview;
  for (const b of document.querySelectorAll("#field-toggles input[type=checkbox]")) {
    b.onchange = refreshPreview;
  }
  $("btn-back").onclick = () => show(scannerCard);
  $("btn-back2").onclick = () => show(scannerCard);
  $("btn-back3").onclick = () => show(scannerCard);
  $("btn-back4").onclick = () => show(scannerCard);
  $("btn-back5").onclick = () => show(scannerCard);
  $("btn-custom").onclick = () => {
    show(customCard);
    $("c-name").focus();
  };
  $("btn-price-changes").onclick = openPriceChanges;
  $("btn-pc-print").onclick = printSelectedPriceChanges;
  $("btn-pc-dismiss").onclick = dismissSelectedPriceChanges;
  $("pc-select-all").onchange = (e) => {
    for (const b of document.querySelectorAll("#price-changes-list .pc-check"))
      b.checked = e.target.checked;
  };
  $("btn-print-history").onclick = openPrintHistory;
  $("btn-ph-refresh").onclick = refreshPrintHistory;
  $("ph-filter").onchange = refreshPrintHistory;
  $("custom-form").onsubmit = createCustom;
  $("manual-input").oninput = onManualInput;
  $("manual-form").onsubmit = (e) => {
    e.preventDefault();
    const q = $("manual-input").value.trim();
    if (!q) return;
    clearTimeout(liveTimer);
    hideLiveResults();
    // digits → treat as a UPC lookup; text → name search
    if (/^\d{6,}$/.test(q)) lookup(q);
    else search(q);
  };

  // health badges
  getJSON("/api/health").then(({ body }) => {
    if (!body) return;
    if (body.print_mode === "remote") {
      // Cloud hosting: the store's print bridge drains the queue; the local
      // transport name ("null") is irrelevant — don't scare the user with it.
      $("printer-badge").textContent = "print bridge";
    } else if (!body.print_worker_alive) {
      $("printer-badge").textContent = (body.printer || "printer") + " (worker off)";
    }
  });

  // price-change banner: check on load and every 2 minutes (syncs land ~every
  // 30 min, so this is just cheap enough to catch one soon after it lands).
  refreshPriceChangesBanner();
  setInterval(refreshPriceChangesBanner, 120000);
})();
