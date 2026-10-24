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
  if (role === "bot") {
    bubble.classList.add("md");
    // служебные блоки [Показаны товары: …] / [В корзине: …] видит только LLM
    const visible = String(content)
      .replace(/\n*\[Показаны товары:[^\]]*\]/g, "")
      .replace(/\n*\[В корзине:[^\]]*\]/g, "")
      .trim();
    bubble.innerHTML = renderMarkdown(visible);
  } else {
    bubble.textContent = content;
  }
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

function finishBotMessage(bubble, answer) {
  const cursor = bubble.querySelector(".cursor");
  if (cursor) cursor.remove();
  // финальный ответ перерисовываем как markdown (во время стрима — сырой текст)
  if (answer && answer.trim()) {
    bubble.classList.add("md");
    bubble.innerHTML = renderMarkdown(answer);
  }
}

// ---------- markdown (без зависимостей) ----------

function renderMarkdown(src) {
  const inline = s => s
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,!?;:])/g, "$1<i>$2</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');

  let html = "";
  let list = null; // "ul" | "ol"
  let para = [];
  const flushPara = () => {
    if (para.length) { html += `<p>${para.map(inline).join("<br>")}</p>`; para = []; }
  };
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };

  for (const line of escapeHtml(src).split("\n")) {
    const t = line.trim();
    if (!t) { flushPara(); closeList(); continue; }
    const h = t.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      flushPara(); closeList();
      const lvl = Math.min(h[1].length + 2, 5); // # → h3
      html += `<h${lvl}>${inline(h[2])}</h${lvl}>`;
      continue;
    }
    if (/^(-{3,}|\*{3,})$/.test(t)) { flushPara(); closeList(); html += "<hr>"; continue; }
    const ul = t.match(/^[-*•]\s+(.*)$/);
    if (ul) {
      flushPara();
      if (list !== "ul") { closeList(); html += "<ul>"; list = "ul"; }
      html += `<li>${inline(ul[1])}</li>`;
      continue;
    }
    const ol = t.match(/^\d+[.)]\s+(.*)$/);
    if (ol) {
      flushPara();
      if (list !== "ol") { closeList(); html += "<ol>"; list = "ol"; }
      html += `<li>${inline(ol[1])}</li>`;
      continue;
    }
    para.push(t);
  }
  flushPara();
  closeList();
  return html;
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
        <div class="name" title="${escapeHtml(p.name || "")}">${escapeHtml(p.name || "")}</div>
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
  let shownProducts = []; // id/названия карточек — для контекста следующего запроса
  let cartItems = [];

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
          shownProducts.push(...(ev.data.items || []));
        } else if (ev.type === "cart") {
          renderCart(ev.data);
          cartItems.push(...(ev.data.items || []));
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
    finishBotMessage(bubble, answer);
    // в историю добавляем служебный блок с id показанных товаров и корзины —
    // по нему LLM на следующем ходу понимает, что такое «этот товар»
    let content = answer || bubble.textContent;
    const notes = [];
    if (shownProducts.length) {
      notes.push("[Показаны товары: " + shownProducts.map(p => `id=${p.id} ${p.name}`).join("; ") + "]");
    }
    if (cartItems.length) {
      notes.push("[В корзине: " + cartItems.map(p => `id=${p.id} ${p.name} ×${p.quantity}`).join("; ") + "]");
    }
    if (notes.length) content += "\n\n" + notes.join("\n");
    history.push({ role: "assistant", content });
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
