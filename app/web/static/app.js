// Чат-клиент: история в localStorage, стриминг через fetch + ReadableStream
// (EventSource не умеет POST).

const chatEl = document.getElementById("chat");
const inputEl = document.getElementById("input");
const sendEl = document.getElementById("send");
const STORAGE_KEY = "vv-chat";

let history = []; // [{role, content}]
let busy = false;

// ---------- история ----------

function loadHistory() {
  try {
    history = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
  } catch { history = []; }
  history.forEach(m => renderMessage(m.role, m.content));
}

function saveHistory() {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(history.slice(-40)));
}

document.getElementById("clear").addEventListener("click", () => {
  history = [];
  localStorage.removeItem(STORAGE_KEY);
  chatEl.innerHTML = "";
  chatEl.appendChild(buildEmptyState());
});

// ---------- рендер ----------

function buildEmptyState() {
  const div = document.createElement("div");
  div.className = "empty-state";
  div.innerHTML = `
    <div class="empty-emoji">🛒</div>
    <p>Опишите, что нужно — например:</p>
    <div class="examples">
      <button class="example">Собери перекус на работу, бюджет 500 ₽, без лактозы</button>
      <button class="example">Что приготовить на ужин из курицы максимум за 40 минут?</button>
      <button class="example">Положи в корзину сырники, сметану и кофе</button>
    </div>`;
  div.querySelectorAll(".example").forEach(b =>
    b.addEventListener("click", () => { inputEl.value = b.textContent; send(); }));
  return div;
}

function scrollDown() { chatEl.scrollTop = chatEl.scrollHeight; }

function renderMessage(role, content) {
  const row = document.createElement("div");
  row.className = `msg ${role}`;
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = content;
  row.appendChild(bubble);
  chatEl.appendChild(row);
  scrollDown();
  return row;
}

function startBotMessage() {
  const row = document.createElement("div");
  row.className = "msg bot";
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.innerHTML = '<span class="cursor">▍</span>';
  row.appendChild(bubble);
  chatEl.appendChild(row);
  scrollDown();
  return { row, bubble };
}

function appendToken(bubble, text) {
  const cursor = bubble.querySelector(".cursor");
  bubble.insertBefore(document.createTextNode(text), cursor);
  scrollDown();
}

function finishBotMessage(bubble) {
  const cursor = bubble.querySelector(".cursor");
  if (cursor) cursor.remove();
}

function renderToolChip(label) {
  const chip = document.createElement("div");
  chip.className = "tool-chip";
  chip.innerHTML = `<span class="spin"></span>${label}`;
  chatEl.appendChild(chip);
  scrollDown();
  return chip;
}

function renderProducts(items) {
  const wrap = document.createElement("div");
  wrap.className = "products";
  for (const p of items) {
    const a = document.createElement("a");
    a.className = "product-card";
    a.href = p.url;
    a.target = "_blank";
    a.rel = "noopener";
    a.innerHTML = `
      ${p.image ? `<img src="${p.image}" loading="lazy" alt="">`
                : `<div class="ph">🛍️</div>`}
      <div class="info">
        <div class="name">${escapeHtml(p.name || "")}</div>
        <div class="price-row">
          <span class="price">${p.price ?? "—"} ₽</span>
          ${p.old_price ? `<span class="old-price">${p.old_price} ₽</span>` : ""}
        </div>
        ${p.rating ? `<div class="rating">★ ${p.rating}</div>` : ""}
      </div>`;
    wrap.appendChild(a);
  }
  chatEl.appendChild(wrap);
  scrollDown();
}

function renderCart(data) {
  const block = document.createElement("div");
  block.className = "cart-block";
  const itemsHtml = (data.items || [])
    .map(i => `${escapeHtml(i.name || `товар ${i.id}`)} × ${i.quantity}`).join("<br>");
  block.innerHTML = `
    <div class="cart-title">🧺 Корзина собрана</div>
    <div class="cart-items">${itemsHtml}</div>
    <div class="cart-total">Итого: ${data.total} ₽</div>
    <a class="cart-btn" href="${data.url}" target="_blank" rel="noopener">Открыть корзину ВкусВилл →</a>`;
  chatEl.appendChild(block);
  scrollDown();
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- отправка ----------

async function send() {
  const text = inputEl.value.trim();
  if (!text || busy) return;
  busy = true;
  sendEl.disabled = true;
  inputEl.value = "";
  autosize();

  const empty = chatEl.querySelector(".empty-state");
  if (empty) empty.remove();

  history.push({ role: "user", content: text });
  renderMessage("user", text);

  const { bubble } = startBotMessage();
  let answer = "";
  let lastChip = null;

  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: history }),
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE-сообщения разделяются \n\n; парсим только целые
      let idx;
      while ((idx = buffer.indexOf("\n\n")) >= 0) {
        const rawEvent = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);
        const ev = parseSse(rawEvent);
        if (!ev) continue;

        if (ev.type === "token") {
          if (lastChip) { lastChip.remove(); lastChip = null; }
          answer += ev.data.text;
          appendToken(bubble, ev.data.text);
        } else if (ev.type === "tool") {
          if (lastChip) lastChip.remove();
          lastChip = renderToolChip(ev.data.label || ev.data.name);
        } else if (ev.type === "products") {
          renderProducts(ev.data.items || []);
        } else if (ev.type === "cart") {
          renderCart(ev.data);
        } else if (ev.type === "error") {
          answer += `\n\n⚠ ${ev.data.message}`;
          appendToken(bubble, `\n\n⚠ ${ev.data.message}`);
        }
      }
    }
  } catch (err) {
    answer = answer || "Не удалось получить ответ. Попробуйте ещё раз.";
    if (!bubble.textContent.includes("⚠")) {
      bubble.textContent += `\n\n⚠ ${err.message}`;
    }
  } finally {
    if (lastChip) lastChip.remove();
    finishBotMessage(bubble);
    history.push({ role: "assistant", content: answer || bubble.textContent });
    saveHistory();
    busy = false;
    sendEl.disabled = false;
    inputEl.focus();
  }
}

function parseSse(raw) {
  let type = null, dataRaw = null;
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) type = line.slice(7).trim();
    else if (line.startsWith("data: ")) dataRaw = line.slice(6);
  }
  if (!type || dataRaw == null) return null;
  try { return { type, data: JSON.parse(dataRaw) }; }
  catch { return null; }
}

// ---------- ввод ----------

sendEl.addEventListener("click", send);
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
});
function autosize() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
}
inputEl.addEventListener("input", autosize);

loadHistory();
