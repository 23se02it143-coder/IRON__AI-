// Test-mode checkout page (used when PAYMENTS_PROVIDER=mock). No card data is collected and no money moves.
import { state, openAuth, refreshSession } from "../shell.js";
import { api, ApiError } from "../lib/api.js";
import { $, esc, icon, money, setBusy, toast } from "../lib/dom.js";

const root = $("#checkout-root");
const id = location.pathname.split("/").filter(Boolean).pop();

async function load() {
  const user = state.ready ? state.user : await refreshSession();
  if (!user) {
    root.innerHTML = `<section class="panel"><h2>Sign in to continue</h2><p class="sub">Checkout is tied to your account.</p><button class="btn btn--primary" id="signin">Sign in</button></section>`;
    $("#signin").addEventListener("click", async () => { if (await openAuth("login")) load(); });
    return;
  }
  let c;
  try {
    c = await api(`/api/billing/mock/${encodeURIComponent(id)}`);
  } catch (err) {
    root.innerHTML = `<section class="panel"><h2>We couldn't find that checkout</h2><p class="sub">${esc(err.message)}</p><a class="btn btn--ghost" href="/#pricing">Back to plans</a></section>`;
    return;
  }
  if (c.status !== "open") {
    const msg = { complete: "This checkout is already complete.", canceled: "This checkout was cancelled.", expired: "This checkout expired. Start again from the pricing section." }[c.status] || "This checkout is no longer available.";
    root.innerHTML = `<section class="panel"><h2>${esc(msg)}</h2><div class="row mt-l"><a class="btn btn--primary" href="${c.status === "complete" ? "/account" : "/#pricing"}">${c.status === "complete" ? "Go to your account" : "Back to plans"}</a></div></section>`;
    return;
  }
  const per = c.interval === "year" ? "year" : "month";
  root.innerHTML = `
    <section class="summary"><h2>${esc(c.plan.name)} plan</h2>
      <dl><div><dt>Billing</dt><dd>${c.interval === "year" ? "Yearly" : "Monthly"}</dd></div><div><dt>Renews</dt><dd>Every ${per} until cancelled</dd></div>
      <div><dt>Total today</dt><dd class="tnum">${money(c.amount_cents, c.currency)}</dd></div></dl></section>
    <section class="panel"><h2>Confirm</h2>
      <div class="testmode">${icon("info")}<span><b>Test mode.</b> No card is collected and nothing is charged. Confirming activates the plan on your account so you can try the full flow.</span></div>
      <div class="stack mt-l"><button class="btn btn--primary btn--block" id="pay">Confirm ${esc(c.plan.name)} for ${money(c.amount_cents, c.currency)}</button>
      <button class="btn btn--ghost btn--block" id="cancel">Cancel and go back</button></div>
      <p class="form-note mt-l">You can cancel any time from your account. By confirming you agree to the <a class="link" href="/terms" target="_blank">Terms</a>.</p></section>`;
  const finish = async (outcome, btn) => {
    setBusy(btn, true, outcome === "success" ? "Activating" : "Cancelling");
    try {
      await api(`/api/billing/mock/${encodeURIComponent(id)}/complete`, { method: "POST", body: { outcome } });
      location.href = outcome === "success" ? "/account?checkout=success" : "/?checkout=cancelled#pricing";
    } catch (err) {
      setBusy(btn, false);
      toast(err instanceof ApiError ? err.message : "Something went wrong.", "error");
    }
  };
  $("#pay").addEventListener("click", (e) => finish("success", e.currentTarget));
  $("#cancel").addEventListener("click", (e) => finish("cancel", e.currentTarget));
}
load();
