"use client";

import { BookmarkPlus, Check, LayoutTemplate, Pencil, Save, Trash2 } from "lucide-react";

import { usePersistentState } from "@/hooks/usePersistentState";
import { applyWorkspace, CURRENT_WORKSPACE_KEY, saveWorkspace, WORKSPACES_KEY, type SavedWorkspace } from "@/lib/workspaces";

import { Menu } from "./Menu";

/** Header menu for saved layouts: load one, save over the current one, save as new, rename, delete. */
export default function LayoutsMenu() {
  const [list, setList] = usePersistentState<SavedWorkspace[]>(WORKSPACES_KEY, []);
  const [current] = usePersistentState<string | null>(CURRENT_WORKSPACE_KEY, null);
  const cur = list.find((w) => w.id === current);

  const saveNew = () => {
    const name = window.prompt("Name this layout", `Layout ${list.length + 1}`);
    if (name !== null) setList(saveWorkspace(list, { name }));
  };

  return (
    <Menu
      title="Saved layouts"
      trigger={
        <>
          <LayoutTemplate className="h-4 w-4" />
          <span className="hidden max-w-24 truncate text-xs lg:inline">{cur?.name ?? "Layouts"}</span>
        </>
      }
    >
      {list.length === 0 && <p className="px-2 py-1.5 text-[11px] text-mute">Save the charts, timeframes, indicators and panels you have open now, and come back to them in one click.</p>}
      {list.map((w) => (
        <div key={w.id} className="group flex items-center gap-1 rounded px-1 hover:bg-panel2">
          <button type="button" className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-1 text-left text-xs text-ink" onClick={() => applyWorkspace(w)}>
            <Check className={w.id === current ? "h-3.5 w-3.5 text-accent" : "h-3.5 w-3.5 opacity-0"} />
            <span className="truncate">{w.name}</span>
          </button>
          <button
            type="button"
            title="Rename"
            aria-label={`Rename ${w.name}`}
            className="btn-ghost h-6 w-6 p-0 opacity-0 group-hover:opacity-100"
            onClick={() => {
              const name = window.prompt("Rename layout", w.name);
              if (name?.trim()) setList(list.map((x) => (x.id === w.id ? { ...x, name: name.trim() } : x)));
            }}
          >
            <Pencil className="h-3 w-3" />
          </button>
          <button
            type="button"
            title="Delete"
            aria-label={`Delete ${w.name}`}
            className="btn-ghost h-6 w-6 p-0 opacity-0 hover:text-down group-hover:opacity-100"
            onClick={() => window.confirm(`Delete the layout “${w.name}”?`) && setList(list.filter((x) => x.id !== w.id))}
          >
            <Trash2 className="h-3 w-3" />
          </button>
        </div>
      ))}
      <div className="my-1 h-px bg-line" />
      {cur && (
        <button type="button" className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs text-ink hover:bg-panel2" onClick={() => setList(saveWorkspace(list, { id: cur.id }))}>
          <Save className="h-3.5 w-3.5" /> Save “{cur.name}” <span className="ml-auto text-[10px] text-mute">Ctrl+S</span>
        </button>
      )}
      <button type="button" className="flex w-full items-center gap-1.5 rounded px-2 py-1.5 text-left text-xs text-ink hover:bg-panel2" onClick={saveNew}>
        <BookmarkPlus className="h-3.5 w-3.5" /> Save as a new layout
      </button>
    </Menu>
  );
}
