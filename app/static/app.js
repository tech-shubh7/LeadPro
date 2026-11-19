/* Small, dependency-free behaviours. Every page works without JavaScript; this only makes waiting and
   watching nicer: poll a drafting plan, follow a live run, guard forms against double submits, copy. */
(() => {
  "use strict";

  const BAR_STEP = 5; // CSS width classes come in 5% steps (w0 … w100)
  const STAGE = { queued: 1, discovering: 1, researching: 2, scoring: 3, finding_contacts: 4, revalidating: 4,
                  done: 6, done_with_shortfall: 6 };
  const widthClass = (part, whole) =>
    `w${whole ? Math.round((Math.min(part / whole, 1) * 100) / BAR_STEP) * BAR_STEP : 0}`;

  /** Polls `url` with a widening gap; `onData` returns true when done. Gives up after repeated failures. */
  const poll = (url, onData, firstDelay = 2000) => {
    let misses = 0;
    const tick = async () => {
      try {
        const res = await fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
        if (!res.ok) throw new Error(String(res.status));
        misses = 0;
        if (onData(await res.json())) return;
      } catch {
        if (++misses > 8) return; // the server is down: stop rather than hammer it
      }
      window.setTimeout(tick, firstDelay + misses * 2000);
    };
    window.setTimeout(tick, firstDelay);
  };

  // 1. Drafting the plan: show elapsed time, move on once the plan is ready (or drafting failed).
  const drafting = document.getElementById("drafting");
  if (drafting) {
    const started = Date.now();
    const elapsed = drafting.querySelector("[data-elapsed]");
    const timer = window.setInterval(() => {
      if (elapsed) elapsed.textContent = `${Math.round((Date.now() - started) / 1000)}s`;
    }, 1000);
    poll(`/new/${drafting.dataset.job}/status`, (data) => {
      if (!data.ready) return false;
      window.clearInterval(timer);
      window.location.reload();
      return true;
    }, 1500);
  }

  // 2. A run in flight: update counters, bar and stepper in place, reload once when it finishes so the
  //    status, cost and lead table are all read from the database together.
  const live = document.getElementById("live");
  if (live && live.dataset.running === "1") {
    const target = Number(live.dataset.target) || 0;
    const nodes = {};
    live.querySelectorAll("[data-live]").forEach((n) => (nodes[n.dataset.live] = n));

    poll(`/jobs/${live.dataset.job}/progress`, (data) => {
      for (const key of ["qualified", "icp_only", "no_contact", "watchlist", "rejected"]) {
        if (nodes[key]) nodes[key].textContent = data[key];
      }
      if (nodes.candidates) {
        const seen = data.qualified + data.icp_only + data.no_contact + (data.watchlist || 0) + data.rejected;
        nodes.candidates.textContent = Math.max(data.candidates || 0, seen);
      }
      if (nodes.cost) nodes.cost.textContent = `₹${Number(data.cost_inr).toFixed(2)}`;
      if (nodes.headline) {
        nodes.headline.textContent = data.running
          ? `Researching… ${data.qualified} qualified so far`
          : `${data.qualified} of ${target} qualified leads`;
      }
      if (nodes.bar && target) nodes.bar.className = widthClass(data.qualified, target);
      if (nodes.stepper) {
        const stage = STAGE[data.status] ?? 0;
        nodes.stepper.querySelectorAll("li[data-step]").forEach((li) => {
          const i = Number(li.dataset.step);
          li.className = i < stage || stage === 6 ? "done" : i === stage ? "now live" : "";
        });
      }
      if (!data.running) {
        window.location.reload();
        return true;
      }
      return false;
    }, 2500);
  }

  // 2b. Whole table rows open their page; real links and buttons inside a row keep their own behaviour,
  //     and Ctrl/Cmd/middle click opens a new tab as on a link.
  const openRow = (event) => {
    const row = event.target.closest("tr[data-href]");
    if (!row || event.target.closest("a, button, input, select, label")) return;
    if (window.getSelection().toString()) return; // the user was selecting text, not clicking
    if (event.ctrlKey || event.metaKey || event.button === 1) window.open(row.dataset.href, "_blank");
    else window.location.href = row.dataset.href;
  };
  document.addEventListener("click", openRow);
  document.addEventListener("auxclick", (event) => event.button === 1 && openRow(event));

  // 3. Example asks fill the textarea they belong to.
  document.addEventListener("click", (event) => {
    const chip = event.target.closest("button[data-fill]");
    if (!chip) return;
    const field = document.getElementById(chip.dataset.fill);
    if (!field) return;
    field.value = chip.textContent.trim();
    field.focus();
  });

  // 4. Forms: confirm destructive ones, and show progress / block a second submit on the rest.
  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
      event.preventDefault();
      return;
    }
    if (!form.hasAttribute("data-busy")) return;
    if (form.dataset.sent) {
      event.preventDefault();
      return;
    }
    form.dataset.sent = "1";
    const button = form.querySelector("button[type=submit]");
    if (button) button.classList.add("is-loading");
    // If the browser restores this page from the back/forward cache, let it be used again.
    window.addEventListener("pageshow", () => {
      delete form.dataset.sent;
      if (button) button.classList.remove("is-loading");
    }, { once: true });
  });

  // 5. Copy a draft or an address, with a selection fallback where the clipboard is unavailable.
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("button.copy");
    if (!button) return;
    const source = button.dataset.copy ? document.getElementById(button.dataset.copy) : null;
    const text = button.dataset.copyText || (source ? source.innerText.trim() : "");
    if (!text) return;
    const label = button.innerHTML;
    try {
      await navigator.clipboard.writeText(text);
      button.textContent = "Copied";
    } catch {
      if (source) {
        const range = document.createRange();
        range.selectNodeContents(source);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
      }
      button.textContent = "Press Ctrl+C";
    }
    window.setTimeout(() => (button.innerHTML = label), 1600);
  });
})();
