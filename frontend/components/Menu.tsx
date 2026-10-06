"use client";

import clsx from "clsx";
import { useEffect, useRef, useState, type ReactNode } from "react";

export function Menu({
  trigger,
  title,
  children,
  align = "left",
}: {
  trigger: ReactNode;
  title: string;
  children: ReactNode;
  align?: "left" | "right";
}) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);

  // The dropdown is fixed-positioned under the trigger: the header row scrolls horizontally, and
  // overflow-x on it would otherwise clip anything that hangs below it.
  useEffect(() => {
    if (!open) return;
    const place = () => {
      const r = buttonRef.current?.getBoundingClientRect();
      if (!r) return;
      const width = 220;
      const left = align === "right" ? r.right - width : r.left;
      setPos({ top: r.bottom + 4, left: Math.max(8, Math.min(left, window.innerWidth - width - 8)) });
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open, align]);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => !ref.current?.contains(e.target as Node) && setOpen(false);
    const esc = (e: KeyboardEvent) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        ref={buttonRef}
        type="button"
        title={title}
        aria-label={title}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={clsx("btn-ghost", open && "bg-panel2 text-ink")}
      >
        {trigger}
      </button>
      {open && pos && (
        <div
          className="fixed z-50 w-[220px] rounded-md border border-line bg-panel p-1 shadow-xl"
          style={{ top: pos.top, left: pos.left }}
        >
          {children}
        </div>
      )}
    </div>
  );
}

export function MenuToggle({ label, checked, onChange, hint }: {
  label: string;
  checked: boolean;
  onChange(v: boolean): void;
  hint?: string;
}) {
  return (
    <label className="flex cursor-pointer items-center justify-between gap-4 rounded px-2 py-1.5 text-xs text-ink hover:bg-panel2">
      <span>
        {label}
        {hint && <span className="ml-1 text-mute">{hint}</span>}
      </span>
      <input type="checkbox" className="accent-blue-500" checked={checked} onChange={(e) => onChange(e.target.checked)} />
    </label>
  );
}
