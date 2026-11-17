export function sessionTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function avatarNode(userId, name, bot, avatarUrl) {
  const node = document.createElement("div");
  node.className = "chat-avatar";
  node.setAttribute("aria-hidden", "true");
  node.textContent = bot ? "B" : (name || "?").trim().slice(0, 1) || "?";
  if (avatarUrl) {
    const image = document.createElement("img");
    image.alt = "";
    image.loading = "lazy";
    image.referrerPolicy = "no-referrer";
    image.src = avatarUrl;
    image.addEventListener("error", () => image.remove());
    node.textContent = "";
    node.append(image);
  }
  return node;
}

export function chatMarkup(value) {
  const esc = (s) =>
    String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const inline = (s) =>
    esc(s)
      .replace(/`([^`\n]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^\w*])\*([^*\n]+)\*(?![\w*])/g, "$1<em>$2</em>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(（])((?:https?:\/\/)[^\s<>"'）)]+)/g, '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');

  const blocks = [];
  const work = String(value || "").replace(/```[^\n`]*\n?([\s\S]*?)(?:```|$)/g, (_, code) => {
    blocks.push(`<pre class="md-pre"><code>${esc(code.replace(/\n+$/, ""))}</code></pre>`);
    return ` ${blocks.length - 1}`;
  });

  let html = "";
  let listTag = null;
  let quote = [];
  let para = [];
  const flushPara = () => {
    if (para.length) {
      html += `<p>${para.map(inline).join("<br>")}</p>`;
      para = [];
    }
  };
  const flushQuote = () => {
    if (quote.length) {
      html += `<blockquote class="md-quote">${quote.map(inline).join("<br>")}</blockquote>`;
      quote = [];
    }
  };
  const closeList = () => {
    if (listTag) {
      html += `</${listTag}>`;
      listTag = null;
    }
  };
  const flushAll = () => {
    flushPara();
    flushQuote();
    closeList();
  };

  for (const line of work.split("\n")) {
    const holder = line.trim().match(/^ ?(\d+) ?$/);
    if (holder) {
      flushAll();
      html += blocks[Number(holder[1])];
      continue;
    }
    let m;
    if ((m = line.match(/^(#{1,4})\s+(.+)$/))) {
      flushAll();
      html += `<div class="md-h md-h${m[1].length}">${inline(m[2])}</div>`;
    } else if (/^\s*(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushAll();
      html += '<hr class="md-hr">';
    } else if ((m = line.match(/^\s*[-*•]\s+(.+)$/))) {
      flushPara();
      flushQuote();
      if (listTag !== "ul") {
        closeList();
        html += '<ul class="md-list">';
        listTag = "ul";
      }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*\d{1,3}[.)]\s+(.+)$/))) {
      flushPara();
      flushQuote();
      if (listTag !== "ol") {
        closeList();
        html += '<ol class="md-list">';
        listTag = "ol";
      }
      html += `<li>${inline(m[1])}</li>`;
    } else if ((m = line.match(/^\s*>\s?(.*)$/))) {
      flushPara();
      closeList();
      quote.push(m[1]);
    } else if (line.trim() === "") {
      flushAll();
    } else {
      flushQuote();
      closeList();
      para.push(line);
    }
  }
  flushAll();
  return html;
}
