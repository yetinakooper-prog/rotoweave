import { createContext, useCallback, useContext, useEffect, useMemo, useRef, type ReactNode } from "react";
/* eslint-disable react-refresh/only-export-components -- provider and hooks form one local page-command contract. */

export type PageSaveCommandV4 = () => boolean | Promise<boolean>;

type PageSaveCommandContextValue = {
  register: (command: PageSaveCommandV4) => () => void;
  execute: () => Promise<boolean>;
};

const PageSaveCommandContext = createContext<PageSaveCommandContextValue | null>(null);

export function PageSaveCommandProviderV4({ children }: { children: ReactNode }) {
  const commandRef = useRef<PageSaveCommandV4 | null>(null);
  const register = useCallback((command: PageSaveCommandV4) => {
    commandRef.current = command;
    return () => { if (commandRef.current === command) commandRef.current = null; };
  }, []);
  const execute = useCallback(async () => commandRef.current ? await commandRef.current() : true, []);

  useEffect(() => {
    const keyDown = (event: KeyboardEvent) => {
      if (!(event.ctrlKey || event.metaKey) || event.key.toLowerCase() !== "s") return;
      event.preventDefault();
      void execute();
    };
    window.addEventListener("keydown", keyDown, { capture: true });
    return () => window.removeEventListener("keydown", keyDown, { capture: true });
  }, [execute]);

  const value = useMemo(() => ({ register, execute }), [execute, register]);
  return <PageSaveCommandContext.Provider value={value}>{children}</PageSaveCommandContext.Provider>;
}

function usePageSaveContextV4(): PageSaveCommandContextValue {
  const context = useContext(PageSaveCommandContext);
  if (!context) throw new Error("PageSaveCommandProviderV4 is required.");
  return context;
}

export function usePageSaveCommandV4(command: PageSaveCommandV4): void {
  const { register } = usePageSaveContextV4();
  const commandRef = useRef(command);
  useEffect(() => { commandRef.current = command; }, [command]);
  useEffect(() => register(() => commandRef.current()), [register]);
}

export function usePageSaveExecutorV4(): () => Promise<boolean> {
  return usePageSaveContextV4().execute;
}
