import { useEffect, useState, type InputHTMLAttributes, type KeyboardEvent } from "react";
/* eslint-disable react-hooks/set-state-in-effect -- the non-editing draft mirrors an externally committed numeric value. */

type NumericDraftInputProps = Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "type"> & {
  value: number;
  minimum?: number;
  maximum?: number;
  strictlyPositive?: boolean;
  onCommit: (value: number) => void;
};

export function NumericDraftInput({ value, minimum, maximum, strictlyPositive = false, onCommit, ...props }: NumericDraftInputProps) {
  const [draft, setDraft] = useState(String(value));
  const [editing, setEditing] = useState(false);
  useEffect(() => { if (!editing) setDraft(String(value)); }, [editing, value]);

  function commit() {
    const parsed = Number(draft);
    if (!Number.isFinite(parsed) || (strictlyPositive && parsed <= 0)) {
      setDraft(String(value)); setEditing(false); return;
    }
    const normalized = Math.min(maximum ?? Infinity, Math.max(minimum ?? -Infinity, parsed));
    if (strictlyPositive && normalized <= 0) { setDraft(String(value)); setEditing(false); return; }
    onCommit(normalized); setDraft(String(normalized)); setEditing(false);
  }

  function keyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") { event.preventDefault(); commit(); event.currentTarget.blur(); }
    if (event.key === "Escape") { event.preventDefault(); setDraft(String(value)); setEditing(false); event.currentTarget.blur(); }
  }

  return <input {...props} type="text" inputMode="decimal" value={draft} onFocus={() => setEditing(true)} onChange={(event) => setDraft(event.target.value)} onBlur={commit} onKeyDown={keyDown} />;
}
