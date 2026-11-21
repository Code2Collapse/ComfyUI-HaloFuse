// HaloFuse account section (collapsible): email, masked password, save, test connection, status,
// change / clear, configured indicator. No host / endpoint field exists. Credentials live only in the input
// fields until submitted (then the fields are cleared); nothing is written to browser storage.
// Side-effect-free ES module.

import { bindH } from "./dom.js";

const SOURCE_TEXT = {
  keychain: "saved in the system credential store",
  "plaintext-file": "saved unencrypted in the HaloFuse settings file",
  environment: "provided by this machine's deployment settings",
  none: "not saved",
};

export function accountSummary(status) {
  if (!status) return { label: "Checking…", tone: "neutral" };
  if (status.service_configured === false) return { label: "Not configured", tone: "danger" };
  if (status.connector_installed === false) return { label: "Connector missing", tone: "danger" };
  if (status.credentials_read_only && status.credentials_configured) return { label: "Managed by deployment", tone: "success" };
  if (status.credentials_configured) return { label: "Configured", tone: "success" };
  return { label: "Not set up", tone: "warning" };
}

export class AccountSection {
  /** @param {{doc: Document, api: object, onStatus: (status: object) => void, win: Window}} o */
  constructor({ doc, win, api, onStatus }) {
    this.doc = doc;
    this.win = win;
    this.api = api;
    this.onStatus = onStatus || (() => {});
    this.status = null;
    const H = bindH(doc);
    const uid = Math.random().toString(36).slice(2, 8);
    this.badge = H("span", { className: "hf-badge hf-tone-neutral", text: "Checking…" });
    this.stateText = H("div", { className: "hf-hint", style: { margin: "0 0 10px" } });
    this.email = H("input", { className: "hf-input", type: "email", id: `hf-email-${uid}`, name: "halofuse-email",
      autocomplete: "username", spellcheck: false, attrs: { autocapitalize: "off" } });
    this.password = H("input", { className: "hf-input", type: "password", id: `hf-pass-${uid}`, name: "halofuse-password",
      autocomplete: "current-password" });
    this.saveBtn = H("button", { className: "hf-btn hf-primary", type: "button", text: "Save credentials", onclick: () => this.save(false) });
    this.testBtn = H("button", { className: "hf-btn", type: "button", text: "Test connection", onclick: () => this.test() });
    this.clearBtn = H("button", { className: "hf-btn hf-danger", type: "button", text: "Clear saved credentials", onclick: () => this.clear() });
    this.result = H("div", { className: "hf-status", attrs: { role: "status", "aria-live": "polite" } });
    this.unencryptedBox = H("div", { className: "hf-consent", hidden: true },
      H("div", { text: "This computer has no secure credential store. HaloFuse can save the password unencrypted in its settings file, where anyone who can read this user account's files can read it." }),
      H("div", { className: "hf-row" },
        H("button", { className: "hf-btn hf-danger hf-solid", type: "button", text: "Store as plain text on this computer", onclick: () => this.save(true) }),
        H("button", { className: "hf-btn", type: "button", text: "Cancel", onclick: () => { this.unencryptedBox.hidden = true; } })));
    this.form = H("form", { attrs: { "aria-label": "HaloFuse account", novalidate: true }, onsubmit: (ev) => { ev.preventDefault(); this.save(false); } },
      H("label", { className: "hf-field", htmlFor: this.email.id }, H("span", { text: "Account email" }), this.email),
      H("label", { className: "hf-field", htmlFor: this.password.id }, H("span", { text: "Password" }), this.password),
      H("div", { className: "hf-row", style: { marginBottom: "8px" } }, this.saveBtn, this.testBtn),
      H("div", { className: "hf-row" }, this.clearBtn),
      this.unencryptedBox);
    this.el = H("details", { className: "hf-section hf-account" },
      H("summary", {}, "Account", this.badge),
      this.stateText, this.form, this.result);
  }

  async load() {
    try {
      this._apply(await this.api.getAccount());
    } catch (err) {
      // older bridges serve the credential status inside /settings only
      try { this._apply(await this.api.getSettings()); } catch { this._say(err.message, "danger"); }
    }
  }

  /** Accepts the credential status shape (C-1 §7) or a full settings response. */
  _apply(status) {
    this.status = status || {};
    const s = this.status;
    const sum = accountSummary(s);
    this.badge.className = `hf-badge hf-tone-${sum.tone}`;
    this.badge.textContent = sum.label;
    const ro = !!s.credentials_read_only;
    let text;
    if (s.service_configured === false) text = "HaloFuse is not configured on this machine. Ask your administrator to finish the HaloFuse setup.";
    else if (s.connector_installed === false) text = "HaloFuse connector not installed. Ask your administrator to install the HaloFuse requirements.";
    else if (s.credentials_configured) {
      const where = SOURCE_TEXT[s.credential_source] || "saved";
      const label = s.credential_storage?.label ? ` (${s.credential_storage.label})` : "";
      const plain = s.credential_storage && s.credential_storage.encrypted_at_rest === false ? " They are not encrypted." : "";
      text = `Credentials are ${where}${label}.${plain}${ro ? " They can only be changed there." : " Enter new values to change them; a blank password keeps the saved one."}`;
    } else text = "Enter the HaloFuse account email and password, then save. You can test them first.";
    if (typeof s.migration === "string" && s.migration.startsWith("pending")) text += " Older saved credentials are waiting to be moved to secure storage.";
    this.stateText.textContent = text;
    this.email.placeholder = s.credentials_configured ? "Saved — type a new email to change it" : "you@studio.com";
    this.password.placeholder = s.password_set ? "Saved — leave blank to keep" : "Password";
    for (const el of [this.email, this.password, this.saveBtn, this.clearBtn]) el.disabled = ro;
    this.clearBtn.hidden = !s.credentials_configured;
    this.onStatus(s);
  }

  _say(text, tone = "neutral") {
    this.result.className = `hf-status hf-tone-${tone}`;
    this.result.textContent = text;
  }

  _busy(on) {
    for (const b of [this.saveBtn, this.testBtn, this.clearBtn]) b.disabled = on || (!!this.status?.credentials_read_only && b !== this.testBtn);
  }

  /** Read and immediately clear the inputs, so the password does not linger in the DOM. */
  _take() {
    const email = this.email.value;
    const password = this.password.value;
    this.password.value = "";
    return { email, password };
  }

  async save(storeUnencrypted) {
    const values = storeUnencrypted && this._pending ? this._pending : this._take();
    this._pending = null;
    this.unencryptedBox.hidden = true;
    if (!values.email.trim() && !values.password.trim()) {
      this._say("Nothing to save: enter an email and/or a new password.", "warning");
      return;
    }
    this._busy(true);
    this._say("Saving…", "active");
    try {
      const status = await this.api.saveCredentials({ ...values, storeUnencrypted });
      this.email.value = "";
      this._apply(status);
      this._say("Credentials saved.", "success");
    } catch (err) {
      if (err.code === "no_secure_store") {
        this._pending = values; // kept in memory only until the user decides
        this.unencryptedBox.hidden = false;
        this._say(err.message, "warning");
      } else {
        this._say(err.message || "Could not save the credentials.", "danger");
      }
    } finally { this._busy(false); }
  }

  async test() {
    const typed = this.email.value.trim() || this.password.value.trim();
    const body = typed ? this._take() : null;
    this._busy(true);
    this._say(body ? "Testing the entered credentials (not saved)…" : "Testing the saved credentials…", "active");
    try {
      const r = await this.api.testCredentials(body ? { email: body.email.trim(), password: body.password } : undefined);
      this._say(r.message || (r.ok ? "Connected to HaloFuse." : "The connection test failed."), r.ok ? "success" : "danger");
      if (body && r.ok) this._say(`${r.message || "Connected."} Re-enter the password and press Save credentials to keep it.`, "success");
    } catch (err) {
      this._say(err.message || "The connection test failed.", "danger");
    } finally { this._busy(false); }
  }

  async clear() {
    if (this.clearBtn.dataset.confirm !== "1") {
      this.clearBtn.dataset.confirm = "1";
      this.clearBtn.textContent = "Press again to clear";
      this._confirmTimer = this.win.setTimeout(() => this._resetClear(), 5000);
      return;
    }
    this._resetClear();
    this._busy(true);
    try {
      this._apply(await this.api.clearCredentials());
      this._say("Saved credentials were removed from this computer.", "success");
    } catch (err) {
      this._say(err.message || "Could not clear the credentials.", "danger");
    } finally { this._busy(false); }
  }

  _resetClear() {
    if (this._confirmTimer) this.win.clearTimeout(this._confirmTimer);
    this.clearBtn.dataset.confirm = "";
    this.clearBtn.textContent = "Clear saved credentials";
  }
}
