"use strict";

// Highlight only code text, after Markdown sanitization. Unknown languages and
// failed optional assets fall back to readable source, without changing it.
window.AgentCode = (() => {
  const aliases = {py: "python", python3: "python", js: "javascript", jsx: "javascript",
    ts: "typescript", tsx: "typescript", sh: "bash", shell: "bash", zsh: "bash",
    html: "xml", svg: "xml", tex: "latex", tikz: "latex", cxx: "cpp", "c++": "cpp", yml: "yaml"};
  function completeTokens(code, language) {
    if (!['python', 'bash'].includes(language)) return;
    // Highlight.js already recognizes strings, comments, keywords and literals.
    // Supplement only its remaining text; never re-tokenize protected content.
    const walker = document.createTreeWalker(code, NodeFilter.SHOW_TEXT), nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    const tokens = language === 'python'
      ? /[\p{L}_][\p{L}\p{N}\p{M}_]*|\*\*=|\/\/=|<<=|>>=|:=|==|!=|<=|>=|\*\*|\/\/|<<|>>|->|[+\-*\/%&|^]=|[+\-*\/%&|^~<>=@]|[()[\]{},.:;]/gu
      : /&&|\|\||>>|<<|;;|[|&;<>]|[(){}]/g;
    for (const node of nodes) {
      const parent = node.parentElement;
      if (!parent || parent.closest('.hljs-keyword,.hljs-number,.hljs-literal,.hljs-title,.hljs-built_in,.hljs-type,.hljs-meta,.hljs-doctag')) continue;
      const protectedNode = parent.closest('.hljs-string,.hljs-comment,.hljs-regexp');
      const substitution = parent.closest('.hljs-subst');
      if (protectedNode && (!substitution || substitution.contains(protectedNode))) continue;
      const source = node.textContent, fragment = document.createDocumentFragment();
      let end = 0;
      for (const match of source.matchAll(tokens)) {
        fragment.append(document.createTextNode(source.slice(end, match.index)));
        const value = match[0], token = document.createElement('span');
        token.textContent = value;
        if (/^[\p{L}_]/u.test(value)) {
          token.className = /^\s*\(/.test(source.slice(match.index + value.length)) ? 'hljs-title function_'
            : /\.\s*$/.test(source.slice(0, match.index)) ? 'hljs-property' : 'hljs-variable';
        } else token.className = /^[()[\]{},.:;]$/.test(value) ? 'hljs-punctuation' : 'hljs-operator';
        fragment.append(token);end = match.index + value.length;
      }
      if (end) {fragment.append(document.createTextNode(source.slice(end)));node.replaceWith(fragment);}
    }
  }
  function highlight(code, requested = "") {
    const source = code.textContent;
    const name = requested.toLowerCase().split(/\s+/)[0], language = aliases[name] || name;
    code.classList.remove("hljs"); delete code.dataset.highlighted;
    for (const item of [...code.classList]) if (item.startsWith('language-')) code.classList.remove(item);
    if (name) code.classList.add('language-' + name);
    if (language) code.classList.add('language-' + language);
    if (!window.hljs || (!language && source.length > 100000) || /^(text|plaintext|txt|none)$/.test(language)) return language || "text";
    try {
      if (language && !hljs.getLanguage(language)) return name;
      const result = language ? hljs.highlight(source, {language, ignoreIllegals: true})
        : hljs.highlightAuto(source, ["python", "javascript", "typescript", "bash", "json", "css", "xml", "sql", "cpp"].filter(item => hljs.getLanguage(item)));
      code.replaceChildren(DOMPurify.sanitize(result.value, {RETURN_DOM_FRAGMENT: true,
        ALLOWED_TAGS: ["span"], ALLOWED_ATTR: ["class"], ALLOW_DATA_ATTR: false}));
      completeTokens(code, result.language || language);
      code.classList.add("hljs"); code.dataset.highlighted = "yes";
      return result.language || name || "text";
    } catch {code.textContent = source;return name || "text";}
  }
  return {highlight};
})();

// Local libraries only. Sanitize Markdown before inserting trusted KaTeX nodes.
window.AgentMarkdown = (() => {
  function render(source, options = {}) {
    const formulas = [], pictures = [], marker = "AGENTMATH" + crypto.randomUUID().replaceAll("-", "") + "TOKEN";
    const protectedSource = String(source ?? "").replace(
      /```[^\n]*\n[\s\S]*?(?:```|$)|~~~[^\n]*\n[\s\S]*?(?:~~~|$)|`[^`\n]*`|\\\[[\s\S]*?\\\]|\\\([\s\S]*?\\\)|(?<!\\)\$\$[\s\S]*?\$\$|(?<!\\)\$(?!\s)(?:\\.|[^$\\\n])+?\$(?!\d)/g,
      match => {
        if (match.startsWith("`") || match.startsWith("~~~")) return match;
        const display = match.startsWith("$$") || match.startsWith("\\[");
        const size = match.startsWith("\\") || display ? 2 : 1;
        formulas.push({source: match.slice(size, -size), display, original: match});
        return marker + (formulas.length - 1) + "END";
      });
    const literal = value => String(value ?? "").replace(new RegExp(marker + "(\\d+)END", "g"), (_, index) => formulas[Number(index)].original);
    const renderer = new marked.Renderer();
    renderer.image = token => {
      pictures.push({url: literal(token.href), alt: literal(token.text), title: literal(token.title)});
      return '<span data-agent-image="' + (pictures.length - 1) + '"></span>';
    };
    const fragment = DOMPurify.sanitize(marked.parse(protectedSource, {gfm: true, renderer}), {
      RETURN_DOM_FRAGMENT: true,
      ALLOWED_TAGS: ["p", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "li", "strong", "em", "del", "s", "blockquote", "pre", "code", "hr", "br", "table", "thead", "tbody", "tr", "th", "td", "a", "span", "input"],
      ALLOWED_ATTR: ["href", "title", "class", "start", "align", "type", "checked", "disabled", "data-agent-image"],
      ALLOW_DATA_ATTR: false,
    });
    for (const link of fragment.querySelectorAll("a")) {
      if (!/^https?:\/\//i.test(link.getAttribute("href") || "")) link.replaceWith(...link.childNodes);
      else { link.target = "_blank"; link.rel = "noopener noreferrer"; }
    }
    for (const input of fragment.querySelectorAll("input")) { input.type = "checkbox"; input.disabled = true; }
    for (const slot of fragment.querySelectorAll("[data-agent-image]")) {
      const picture = pictures[Number(slot.dataset.agentImage)];
      const url = picture && options.imageURL?.(picture.url);
      if (!url) { slot.replaceWith(document.createTextNode(picture?.alt || "")); continue; }
      const figure = document.createElement("figure"), link = document.createElement("a"), image = document.createElement("img");
      figure.className = "message-image"; link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
      image.src = url; image.alt = picture.alt || "图片"; image.title = picture.title || picture.alt || "点击查看原图";
      image.loading = "lazy"; image.decoding = "async"; image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => {image.hidden = true; link.textContent = picture.alt ? `无法加载图片：${picture.alt}` : "无法加载图片，点击检查原图"; figure.classList.add("image-failed");});
      link.append(image); figure.append(link);
      if (picture.alt) {const caption = document.createElement("figcaption"); caption.textContent = picture.alt; figure.append(caption);}
      slot.replaceWith(figure);
    }
    const walker = document.createTreeWalker(fragment, NodeFilter.SHOW_TEXT), nodes = [];
    while (walker.nextNode()) if (walker.currentNode.textContent.includes(marker)) nodes.push(walker.currentNode);
    for (const node of nodes) {
      if (node.parentElement?.closest("pre,code")) {
        node.textContent = node.textContent.replace(new RegExp(marker + "(\\d+)END", "g"), (_, index) => formulas[Number(index)].original);
        continue;
      }
      const replacement = document.createDocumentFragment();
      let start = 0;
      for (const match of node.textContent.matchAll(new RegExp(marker + "(\\d+)END", "g"))) {
        replacement.append(document.createTextNode(node.textContent.slice(start, match.index)));
        const formula = formulas[Number(match[1])], span = document.createElement("span");
        try { katex.render(formula.source, span, {displayMode: formula.display, throwOnError: false,
          trust: false, strict: "ignore", maxExpand: 1000, maxSize: 20}); }
        catch { span.textContent = formula.original; }
        replacement.append(span); start = match.index + match[0].length;
      }
      replacement.append(document.createTextNode(node.textContent.slice(start))); node.replaceWith(replacement);
    }
    for (const pre of [...fragment.querySelectorAll("pre")]) {
      const code = pre.querySelector("code"), block = document.createElement("div"), heading = document.createElement("div");
      block.className = "code-block"; heading.className = "code-heading";
      const language = code?.className.match(/\blanguage-([^\s]+)/)?.[1] || "";
      const detected = code ? AgentCode.highlight(code, language) : "text";
      const label = document.createElement("span"); label.textContent = language || detected;
      const copy = document.createElement("button"); copy.type = "button"; copy.className = "copy-code"; copy.textContent = "Copy";
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(code?.textContent ?? pre.textContent); copy.textContent = "Copied"; }
        catch { copy.textContent = "请手动复制"; }
      });
      heading.append(label, copy); pre.replaceWith(block); block.append(heading, pre);
    }
    for (const table of [...fragment.querySelectorAll("table")]) {
      const wrap = document.createElement("div"); wrap.className = "table-wrap"; table.replaceWith(wrap); wrap.append(table);
    }
    return fragment;
  }
  return {render};
})();
