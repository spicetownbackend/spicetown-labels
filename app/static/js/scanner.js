/* Spice Town — scanner UI logic. Talks to the JSON API from earlier stages. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const scannerCard = $("scanner-card");
  const resultCard = $("result-card");
  const suggestCard = $("suggest-card");
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
    for (const c of [scannerCard, resultCard, suggestCard]) c.hidden = c !== card;
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

  function refreshPreview() {
    const v = $("variant-select").value;
    const params = new URLSearchParams();
    if (currentProductId != null) params.set("id", currentProductId);
    if (v) params.set("variant", v);
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
          constraints: { facingMode: "environment" },
        },
        decoder: {
          readers: ["upc_reader", "upc_e_reader", "ean_reader", "ean_8_reader", "code_128_reader"],
        },
        locate: true,
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

  function onDetected(res) {
    const code = res && res.codeResult && res.codeResult.code;
    if (!code) return;
    const now = Date.now();
    // debounce duplicate reads
    if (code === lastCode && now - lastAt < 2500) return;
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
  $("btn-back").onclick = () => show(scannerCard);
  $("btn-back2").onclick = () => show(scannerCard);
  $("manual-form").onsubmit = (e) => {
    e.preventDefault();
    const q = $("manual-input").value.trim();
    if (!q) return;
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
})();
