import { useCallback, useEffect, useRef, useState } from "react";

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(
    target.closest(
      "input,textarea,select,button,[contenteditable='true'],[role='textbox']",
    ),
  );
}

export function useCanvasSpacePan(enabled = true) {
  const hoveredRef = useRef(false);
  const pressedRef = useRef(false);
  const [spacePressed, setSpacePressed] = useState(false);

  const onCanvasEnter = useCallback(() => {
    hoveredRef.current = true;
    // Mouse users commonly click a tool and immediately move into the canvas.
    // Release the residual button focus so Space controls the hovered canvas;
    // text-entry controls keep focus and retain their normal Space behavior.
    if (document.activeElement instanceof HTMLButtonElement) {
      document.activeElement.blur();
    }
  }, []);
  const onCanvasLeave = useCallback(() => {
    hoveredRef.current = false;
  }, []);
  const cancelSpacePan = useCallback(() => {
    pressedRef.current = false;
    setSpacePressed(false);
  }, []);
  const isSpacePressed = useCallback(
    () => enabled && pressedRef.current,
    [enabled],
  );

  useEffect(() => {
    if (!enabled) return;
    const keyDown = (event: KeyboardEvent) => {
      if (event.code !== "Space") return;
      const ownsSpace = pressedRef.current || (
        hoveredRef.current
        && !isEditableTarget(event.target)
        && !isEditableTarget(document.activeElement)
      );
      if (!ownsSpace) return;
      event.preventDefault();
      if (!event.repeat) {
        pressedRef.current = true;
        setSpacePressed(true);
      }
    };
    const keyUp = (event: KeyboardEvent) => {
      if (event.code !== "Space") return;
      if (pressedRef.current || hoveredRef.current) event.preventDefault();
      cancelSpacePan();
    };
    const clear = () => cancelSpacePan();
    window.addEventListener("keydown", keyDown, { passive: false });
    window.addEventListener("keyup", keyUp, { passive: false });
    window.addEventListener("blur", clear);
    return () => {
      window.removeEventListener("keydown", keyDown);
      window.removeEventListener("keyup", keyUp);
      window.removeEventListener("blur", clear);
    };
  }, [cancelSpacePan, enabled]);

  return {
    spacePressed: enabled && spacePressed,
    isSpacePressed,
    onCanvasEnter,
    onCanvasLeave,
    cancelSpacePan,
  };
}
