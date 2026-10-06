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
  const ref = useRef<HTMLDivElement>(null);

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
        type="button"
        title={title}
        aria-label={title}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className={clsx("btn-ghost", open && "bg-panel2 text-ink")}
      >
        {trigger}
      </button>
      {open && (
        <div
          className={clsx(
            "absolute top-full z-40 mt-1 min-w-[200px] rounded-md border border-line bg-panel p-1 shadow-xl",
            align === "right" ? "right-0" : "left-0",
          )}
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
