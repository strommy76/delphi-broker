(function () {
  const renderMarkdown = (rawText) => {
    if (!window.marked || !window.DOMPurify) {
      return null;
    }
    const html = window.marked.parse(rawText, { breaks: true, gfm: true });
    return window.DOMPurify.sanitize(html);
  };

  const highlightCode = (container) => {
    if (!window.hljs) {
      return;
    }
    container.querySelectorAll("pre code").forEach((block) => {
      try {
        window.hljs.highlightElement(block);
      } catch (err) {
        console.warn("Code highlight failed", err);
      }
    });
  };

  const renderInto = (container, rawText) => {
    const output = container.querySelector("[data-collab-output]");
    const fallback = container.querySelector("[data-collab-fallback]");
    if (!output) {
      return;
    }
    const rendered = renderMarkdown(rawText);
    if (rendered === null) {
      if (fallback) {
        fallback.hidden = false;
      }
      output.hidden = true;
      output.innerHTML = "";
      return;
    }
    output.innerHTML = rendered;
    output.hidden = false;
    if (fallback) {
      fallback.hidden = true;
    }
    highlightCode(output);
  };

  const sourceText = (container) => {
    const source = container.querySelector("[data-collab-source]");
    if (source instanceof HTMLTemplateElement) {
      return source.content.textContent || "";
    }
    return source ? source.textContent || "" : "";
  };

  const renderStaticBlocks = () => {
    document.querySelectorAll("[data-collab-render]").forEach((container) => {
      renderInto(container, sourceText(container));
    });
  };

  const attachPreview = () => {
    const preview = document.querySelector("[data-collab-preview]");
    const input = document.querySelector("[data-collab-preview-source]");
    if (!preview || !input) {
      return;
    }
    const update = () => {
      renderInto(preview, input.value || "");
    };
    input.addEventListener("input", update);
    update();
  };

  document.addEventListener("DOMContentLoaded", () => {
    renderStaticBlocks();
    attachPreview();
  });
})();
