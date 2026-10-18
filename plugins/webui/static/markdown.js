"use strict";
/* A small Markdown renderer that is safe by construction: the text is HTML-escaped first and only the
   renderer's own tags are added (links only to http/https). Math ($…$, $$…$$, \(…\), \[…\]) is cut out
   before anything else and typeset by KaTeX when it has loaded, else shown as raw TeX. Mentions of the given
   names (@Luna) become chips. */
(function () {
  const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  const escape = (text) => String(text).replace(/[&<>"']/g, (c) => ESCAPES[c]);
  const BLOCK = /^\u0001(\d+)\u0001$/;

  function inline(text) {
    return text
      .replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, "<strong>$1</strong>")
      .replace(/__(?=\S)([\s\S]*?\S)__/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*(?=[^\s*])([^*]*?[^\s*])\*(?!\w)/g, "$1<em>$2</em>")
      .replace(/(^|[^_\w])_(?=[^\s_])([^_]*?[^\s_])_(?!\w)/g, "$1<em>$2</em>")
      .replace(/~~(?=\S)([\s\S]*?\S)~~/g, "<del>$1</del>")
      .replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)\u0000]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
      .replace(/(^|[\s(])(https?:\/\/[^\s<\u0000]*[^\s<.,;:!?)\]'"&\u0000])/g,
        '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  }

  function cells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => inline(cell.trim()));
  }

  const regexEscape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

  function render(source, names = []) {
    const stash = [];
    const maths = [];
    const keep = (html) => "\u0000" + (stash.push(html) - 1) + "\u0000";
    const block = (html) => "\n\u0001" + (stash.push(html) - 1) + "\u0001\n";
    const math = (tex, display) => {
      maths.push({ tex: tex.trim(), display });
      const html = `<span class="math${display ? " math-display" : ""}" data-math="${maths.length - 1}"></span>`;
      return display ? block(html) : keep(html);
    };
    let text = String(source || "").replace(/\r\n?/g, "\n").replace(/[\u0000\u0001]/g, "");

    text = text.replace(/(^|\n)[ \t]*```([\w+#.-]*)[^\n]*\n([\s\S]*?)(?:\n[ \t]*```[ \t]*(?=\n|$)|$)/g,
      (match, before, lang, code) => before + block(
        `<pre class="code"><div class="code-head"><span>${escape(lang || "code")}</span>` +
        `<button type="button" class="copy">Copy</button></div><code>${escape(code)}</code></pre>`));
    text = text.replace(/`([^`\n]+)`/g, (match, code) => keep(`<code>${escape(code)}</code>`));
    if (names.length) {
      const pattern = new RegExp(`(^|[^\\w@])@(${names.map(regexEscape).join("|")})(?![\\w])`, "gi");
      text = text.replace(pattern, (match, before, name) => {
        const who = names.find((item) => item.toLowerCase() === name.toLowerCase());
        const key = who.toLowerCase().replace(/[^a-z0-9]/g, "");
        return before + keep(`<span class="mention" data-who="${key}">@${escape(who)}</span>`);
      });
    }
    text = text.replace(/\$\$([\s\S]+?)\$\$/g, (match, tex) => math(tex, true));
    text = text.replace(/\\\[([\s\S]+?)\\\]/g, (match, tex) => math(tex, true));
    text = text.replace(/\\\(([\s\S]+?)\\\)/g, (match, tex) => math(tex, false));
    // $…$ inline: no space just inside the dollars, and no digit right after the closing one ($5 and $10).
    text = text.replace(/(^|[^\\$\w])\$(?=[^\s$])([^\n$]*?[^\s\\$])\$(?![\d$\w])/g,
      (match, before, tex) => before + math(tex, false));

    const lines = escape(text).split("\n");
    const out = [];
    let paragraph = [];
    let list = null;
    let quote = [];
    const flushParagraph = () => {
      if (paragraph.length) out.push(`<p>${paragraph.map(inline).join("<br>")}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (list) out.push(`<${list.tag}>${list.items.map((item) => `<li>${inline(item)}</li>`).join("")}</${list.tag}>`);
      list = null;
    };
    const flushQuote = () => {
      if (quote.length) out.push(`<blockquote>${render(quote.join("\n"), names).html}</blockquote>`);
      quote = [];
    };
    const flush = () => { flushParagraph(); flushList(); flushQuote(); };

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      let match;
      if (BLOCK.test(line.trim())) { flush(); out.push(line.trim()); continue; }
      if ((match = /^&gt;\s?(.*)$/.exec(line))) { flushParagraph(); flushList(); quote.push(match[1]); continue; }
      flushQuote();
      if (!line.trim()) { flushParagraph(); flushList(); continue; }
      if ((match = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line))) {
        flush();
        const level = Math.min(6, match[1].length + 2);
        out.push(`<h${level}>${inline(match[2])}</h${level}>`);
        continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { flush(); out.push("<hr>"); continue; }
      if (line.trim().startsWith("|") && i + 1 < lines.length &&
          /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        flush();
        const head = cells(line);
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
        i--;
        out.push(`<div class="table"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead>` +
          `<tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      if ((match = /^\s*([-*+]|\d{1,3}[.)])\s+(.*)$/.exec(line))) {
        flushParagraph();
        const tag = /\d/.test(match[1]) ? "ol" : "ul";
        if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
        list.items.push(match[2]);
        continue;
      }
      if (list && /^\s{2,}\S/.test(line)) { list.items[list.items.length - 1] += " " + line.trim(); continue; }
      flushList();
      paragraph.push(line);
    }
    flush();

    let html = out.join("");
    for (let round = 0; round < 3 && /[\u0000\u0001]/.test(html); round++) {
      html = html.replace(/[\u0000\u0001](\d+)[\u0000\u0001]/g, (match, index) => stash[Number(index)] ?? "");
    }
    return { html, maths };
  }

  function typeset(root, maths) {
    root.querySelectorAll("[data-math]").forEach((node) => {
      const item = maths[Number(node.dataset.math)];
      if (!item) return;
      if (window.katex) {
        try {
          window.katex.render(item.tex, node, { displayMode: item.display, throwOnError: false, output: "htmlAndMathml" });
          return;
        } catch (error) { /* fall through to raw TeX */ }
      }
      node.classList.add("math-raw");
      node.textContent = item.display ? `$$${item.tex}$$` : `$${item.tex}$`;
    });
  }

  window.Markdown = {
    into(node, text, names = []) {
      const { html, maths } = render(text, names);
      node.innerHTML = html;
      typeset(node, maths);
      return node;
    },
    render,
  };
})();
