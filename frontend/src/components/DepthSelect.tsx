import { useEffect, useRef, useState } from "react";
import type { Depth } from "../types";

const OPTIONS: { value: Depth; name: string; dur: string }[] = [
  { value: "quick", name: "快速", dur: "约 1 分钟" },
  { value: "std", name: "标准", dur: "约 3 分钟" },
  { value: "deep", name: "深度", dur: "约 6 分钟" },
];

export function DepthSelect({ value, onChange }: { value: Depth; onChange: (v: Depth) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const esc = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const current = OPTIONS.find((o) => o.value === value);

  return (
    <div className="depth-sel" ref={ref}>
      <button
        type="button"
        className={`sel-trigger ${open ? "open" : ""}`}
        onClick={() => setOpen((v) => !v)}
      >
        <span>{current?.name}</span>
        <span className="caret">▼</span>
      </button>
      {open && (
        <div className="sel-menu">
          {OPTIONS.map((o) => (
            <button
              type="button"
              key={o.value}
              className={`sel-opt ${o.value === value ? "on" : ""}`}
              onClick={() => {
                onChange(o.value);
                setOpen(false);
              }}
            >
              <span className="check">✓</span>
              <span className="name">{o.name}</span>
              <span className="dur">{o.dur}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
