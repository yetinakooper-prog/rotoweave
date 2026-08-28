import {
  useCallback,
  type Dispatch,
  type SetStateAction,
} from "react";

import {
  useWorkspaceStore,
  type CanvasViewport,
} from "./store";

export function useSessionViewport(
  key: string,
  fallback: CanvasViewport,
): [CanvasViewport, Dispatch<SetStateAction<CanvasViewport>>, boolean] {
  const stored = useWorkspaceStore((state) => state.canvasViewports[key]);
  const setStored = useWorkspaceStore((state) => state.setCanvasViewport);
  const setViewport = useCallback<Dispatch<SetStateAction<CanvasViewport>>>(
    (update) => setStored(key, update, fallback),
    [fallback, key, setStored],
  );
  return [stored ?? fallback, setViewport, stored !== undefined];
}
