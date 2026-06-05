const tg = window.Telegram && window.Telegram.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
}

const state = {
  profile: null,
  deals: [],
  withdrawals: [],
  selectedDeal: null,
  pendingAction: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

const setStatus = (message) => {
  const node = $("#appStatus");
  if (node) node.textContent = message;
};

window.addEventListener("error", (event) => setStatus(`JS: ${event.message}`));
window.addEventListener("unhandledrejection", (event) => {
  const message = event.reason && event.reason.message ? event.reason.message : String(event.reason || "unknown");
  setStatus(`API: ${message}`);
});

const api = async (path, options = {}) => {
  const headers = {
    "Content-Type": "application/json",
    "X-Telegram-Init-Data": (tg && tg.initData) || "",
    ...(options.headers || {}),
  };
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok || data.ok === false) {
    throw new Error(data.description || "Ошибка запроса");
  }
  return data;
};

const toast = (message) => {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  if (tg && tg.HapticFeedback) tg.HapticFeedback.notificationOccurred("success");
  setTimeout(() => node.classList.remove("show"), 2400);
};

const roleLabel = (role) => ({
  buyer: "Покупатель",
  seller: "Продавец",
  admin: "Админ",
  guest: "Гость",
})[role] || role;

const tone = (status) => {
  if (["released", "paid", "paid_hold"].includes(status)) return "ok";
  if (["dispute", "payment_failed", "refunded"].includes(status)) return "danger";
  if (["waiting_payment", "shipping_review", "receive_review", "awaiting_acceptance"].includes(status)) return "warn";
  return "";
};

const setView = (view) => {
  $$(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.view === view));
  $$(".view").forEach((node) => node.classList.toggle("active", node.id === `${view}View`));
};

const filteredDeals = () => {
  const query = $("#dealSearch").value.trim().toLowerCase();
  const status = $("#statusFilter").value;
  return state.deals.filter((deal) => {
    const haystack = [deal.id, deal.item, deal.buyer, deal.seller, deal.amount].join(" ").toLowerCase();
    return (!query || haystack.includes(query)) && (!status || deal.status === status);
  });
};

const renderProfile = () => {
  const profile = state.profile;
  if (!profile) return;
  $("#profileName").textContent = profile.name;
  $("#statTotal").textContent = profile.stats.total;
  $("#statActive").textContent = profile.stats.active;
  $("#statDone").textContent = profile.stats.completed;
  $("#statDisputes").textContent = profile.stats.disputes;
  $("#balanceAvailable").textContent = profile.balance.available;
  $("#balancePending").textContent = profile.balance.pending;
  $("#balanceTotal").textContent = profile.balance.totalCredited;
  $("#payoutDetails").value = profile.payoutDetails || "";
  $$(".admin-only").forEach((node) => {
    node.hidden = !profile.isAdmin;
  });
};

const renderDeals = () => {
  const deals = filteredDeals();
  const list = $("#dealList");
  if (!deals.length) {
    list.innerHTML = `<div class="panel empty">Сделок по этому фильтру нет</div>`;
    return;
  }
  list.innerHTML = deals.map((deal) => `
    <button class="deal-card" type="button" data-deal-id="${deal.id}">
      <header>
        <span class="deal-title">#${deal.id} · ${escapeHtml(deal.item || "Без названия")}</span>
        <span class="badge ${tone(deal.status)}">${escapeHtml(deal.statusLabel)}</span>
      </header>
      <div class="deal-meta">
        <span>${escapeHtml(deal.typeLabel)}</span>
        <span>${escapeHtml(deal.amount)}</span>
        <span>${roleLabel(deal.role)}</span>
        <span>${deal.sellerJoined ? "продавец подключен" : "ждет продавца"}</span>
      </div>
    </button>
  `).join("");
};

const renderAdmin = () => {
  if (!state.profile || !state.profile.isAdmin) return;
  const active = state.deals.filter((deal) => !["released", "refunded"].includes(deal.status));
  $("#adminActive").textContent = active.length;
  $("#adminReview").textContent = state.deals.filter((deal) => ["shipping_review", "receive_review"].includes(deal.status)).length;
  $("#adminDisputes").textContent = state.deals.filter((deal) => deal.status === "dispute").length;
  $("#adminPayouts").textContent = state.withdrawals.filter((item) => item.status === "pending_admin").length;
  const list = $("#withdrawalList");
  if (!state.withdrawals.length) {
    list.innerHTML = `<div class="panel empty">Заявок на вывод нет</div>`;
    return;
  }
  list.innerHTML = state.withdrawals.map((item) => `
    <div class="withdrawal-card" data-withdrawal-id="${item.id}">
      <strong>Заявка #${escapeHtml(item.id)} · ${escapeHtml(item.status)}</strong>
      <span>${escapeHtml(item.seller || "")} · ${Math.round((item.amount || 0) / 100)} грн</span>
      <span class="muted">${escapeHtml(item.details || "реквизиты не указаны")}</span>
      <div class="row-actions">
        <button class="primary" data-withdrawal-action="paid" type="button">Выплачено</button>
        <button class="danger" data-withdrawal-action="reject" type="button">Отклонить</button>
      </div>
    </div>
  `).join("");
};

const timelineItems = (deal) => [
  ["Покупатель согласовал", deal.confirmations && deal.confirmations.buyer_terms],
  ["Продавец согласовал", deal.confirmations && deal.confirmations.seller_terms],
  ["Оплата подтверждена", ["success", "hold"].includes(deal.payment && deal.payment.status)],
  ["Результат передан", deal.handoff || ["delivery", "released"].includes(deal.status)],
  ["Принятие результата", ["receive_review", "released"].includes(deal.status)],
  ["Сделка закрыта", deal.status === "released"],
];

const renderPayment = (deal) => {
  const payment = deal.payment || {};
  if (!payment.provider) return "";
  if (payment.provider === "monobank") {
    const link = payment.pageUrl ? `<a href="${payment.pageUrl}" target="_blank" rel="noreferrer">Открыть оплату</a>` : "";
    return `<strong>Monobank</strong><span>${escapeHtml(payment.status || "создана")}</span>${link}`;
  }
  return `
    <strong>${escapeHtml(payment.assetLabel || "Crypto")}</strong>
    <span>${escapeHtml(payment.status || "ожидает перевод")}</span>
    ${payment.address ? `<code>${escapeHtml(payment.address)}</code>` : ""}
    ${payment.txHash ? `<span>TX: ${escapeHtml(payment.txHash)}</span>` : ""}
  `;
};

const openDeal = (dealId) => {
  const deal = state.deals.find((item) => item.id === dealId);
  if (!deal) return;
  state.selectedDeal = deal;
  $("#dialogRole").textContent = roleLabel(deal.role);
  $("#dialogTitle").textContent = `#${deal.id} · ${deal.item || "Сделка"}`;
  $("#dialogStatus").textContent = deal.statusLabel;
  $("#dialogStatus").className = `badge ${tone(deal.status)}`;
  $("#dialogAmount").textContent = deal.amount;
  $("#dialogBuyer").textContent = deal.buyer || "-";
  $("#dialogSeller").textContent = deal.seller || "-";
  $("#dialogJoinId").textContent = deal.id;
  $("#dialogInspection").textContent = deal.inspectionTime || "-";
  $("#dialogTerms").textContent = deal.successTerms || "-";
  $("#paymentBox").innerHTML = renderPayment(deal);
  $("#timeline").innerHTML = timelineItems(deal).map(([label, done]) => `
    <div class="step ${done ? "done" : ""}">
      <span class="dot"></span>
      <span>${label}</span>
    </div>
  `).join("");
  $("#dialogActions").innerHTML = deal.actions.length
    ? deal.actions.map((action) => `<button class="${action.tone || "secondary"}" data-action="${action.id}" data-needs-text="${action.needsText ? "1" : "0"}" type="button">${escapeHtml(action.label)}</button>`).join("")
    : `<div class="muted">Сейчас нет доступных действий</div>`;
  const dialog = $("#dealDialog");
  if (!dialog.open) dialog.showModal();
};

const reload = async () => {
  setStatus("API: подключение...");
  const me = await api("/api/me");
  const deals = await api("/api/deals");
  state.profile = me.profile;
  state.deals = deals.deals;
  if (state.profile.isAdmin) {
    const withdrawals = await api("/api/admin/withdrawals");
    state.withdrawals = withdrawals.withdrawals || [];
  } else {
    state.withdrawals = [];
  }
  renderProfile();
  renderDeals();
  renderAdmin();
  setStatus(`API: ok · ${state.profile.name}`);
};

const runDealAction = async (action, text = "") => {
  const deal = state.selectedDeal;
  if (!deal) return;
  await api(`/api/deals/${deal.id}/action`, {
    method: "POST",
    body: JSON.stringify({ action, text }),
  });
  await reload();
  const fresh = state.deals.find((item) => item.id === deal.id);
  if (fresh) openDeal(fresh.id);
  toast("Готово");
};

$$(".tab").forEach((tab) => tab.addEventListener("click", () => setView(tab.dataset.view)));
$("#refreshBtn").addEventListener("click", () => reload().then(() => toast("Обновлено")).catch((error) => toast(error.message)));
$("#dealSearch").addEventListener("input", renderDeals);
$("#statusFilter").addEventListener("change", renderDeals);

$("#dealList").addEventListener("click", (event) => {
  const card = event.target.closest("[data-deal-id]");
  if (card) openDeal(card.dataset.dealId);
});

$("#createForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  try {
    const result = await api("/api/deals", { method: "POST", body: JSON.stringify(payload) });
    event.currentTarget.reset();
    event.currentTarget.querySelector("input[value=buyer]").checked = true;
    event.currentTarget.querySelector("input[value=digital]").checked = true;
    await reload();
    setView("deals");
    openDeal(result.deal.id);
    toast("Сделка создана");
  } catch (error) {
    toast(error.message);
  }
});

$("#joinForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const dealId = new FormData(event.currentTarget).get("dealId").trim();
  try {
    const result = await api(`/api/deals/${dealId}/join`, { method: "POST", body: "{}" });
    await reload();
    setView("deals");
    openDeal(result.deal.id);
    toast("Подключено");
  } catch (error) {
    toast(error.message);
  }
});

$("#dialogActions").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button) return;
  const action = button.dataset.action;
  if (button.dataset.needsText === "1") {
    state.pendingAction = action;
    $("#textDialogTitle").textContent = button.textContent;
    $("#actionText").value = "";
    $("#textDialog").showModal();
    return;
  }
  try {
    await runDealAction(action);
  } catch (error) {
    toast(error.message);
  }
});

$("#textActionForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = $("#actionText").value.trim();
  if (!state.pendingAction || !text) return;
  $("#textDialog").close();
  try {
    await runDealAction(state.pendingAction, text);
  } catch (error) {
    toast(error.message);
  }
});

$("#saveProfileBtn").addEventListener("click", async () => {
  try {
    const result = await api("/api/profile", {
      method: "POST",
      body: JSON.stringify({ payoutDetails: $("#payoutDetails").value }),
    });
    state.profile = result.profile;
    renderProfile();
    toast("Профиль сохранен");
  } catch (error) {
    toast(error.message);
  }
});

$("#withdrawBtn").addEventListener("click", async () => {
  try {
    const result = await api("/api/withdrawals", { method: "POST", body: "{}" });
    state.profile = result.profile;
    renderProfile();
    toast("Заявка создана");
  } catch (error) {
    toast(error.message);
  }
});

$("#withdrawalList").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-withdrawal-action]");
  const card = event.target.closest("[data-withdrawal-id]");
  if (!button || !card) return;
  try {
    await api(`/api/withdrawals/${card.dataset.withdrawalId}/action`, {
      method: "POST",
      body: JSON.stringify({ action: button.dataset.withdrawalAction }),
    });
    await reload();
    toast("Выплата обновлена");
  } catch (error) {
    toast(error.message);
  }
});

reload().catch((error) => {
  setStatus(`API: ${error.message}`);
  $("#dealList").innerHTML = `<div class="panel empty">${escapeHtml(error.message)}</div>`;
});
