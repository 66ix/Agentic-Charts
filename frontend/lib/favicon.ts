let original: string | null = null;
let dotted: string | null = null;
let want = false;

function iconLink(): HTMLLinkElement | null {
  return document.querySelector<HTMLLinkElement>("link[rel~='icon']");
}

/** Puts a red dot on the page's icon (events waiting unseen) or puts the plain icon back. */
export function setFaviconDot(on: boolean): void {
  const link = iconLink();
  if (!link) return;
  if (original === null) original = link.href;
  want = on;
  if (!on) {
    if (link.href !== original) link.href = original;
    return;
  }
  if (dotted) {
    link.href = dotted;
    return;
  }
  const img = new Image();
  img.onload = () => {
    const size = 64;
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = size;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.drawImage(img, 0, 0, size, size);
    ctx.beginPath();
    ctx.arc(size * 0.76, size * 0.24, size * 0.2, 0, Math.PI * 2);
    ctx.fillStyle = "#ef4444";
    ctx.fill();
    try {
      dotted = canvas.toDataURL("image/png");
    } catch {
      return; // a cross-origin icon taints the canvas: no dot, the title still counts
    }
    const l = iconLink();
    if (l && want) l.href = dotted; // still wanted: the events may have been seen while it drew
  };
  img.src = original;
}
