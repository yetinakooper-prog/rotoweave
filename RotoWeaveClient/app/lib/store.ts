import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

export type WorkflowStep = "range" | "matte" | "anchor" | "complete";
export type WorkspaceView = "global" | "animation";
export type ToolBackground = "current" | "bright";
export type CanvasViewport = { x: number; y: number; scale: number };
export type Theme = "light" | "dark";

type WorkspaceState = {
  selectedCharacterId: string | null;
  selectedAnimationId: string | null;
  selectedFrameId: string | null;
  activeStep: WorkflowStep;
  workspaceView: WorkspaceView;
  toolBackground: ToolBackground;
  canvasViewports: Record<string, CanvasViewport>;
  disclosures: Record<string, boolean>;
  theme: Theme;
  selectCharacter: (id: string | null) => void;
  selectAnimation: (id: string | null) => void;
  selectFrame: (id: string | null) => void;
  setActiveStep: (step: WorkflowStep) => void;
  showGlobalSettings: () => void;
  setToolBackground: (background: ToolBackground) => void;
  setCanvasViewport: (
    key: string,
    update:
      | CanvasViewport
      | ((current: CanvasViewport) => CanvasViewport),
    fallback: CanvasViewport,
  ) => void;
  setDisclosure: (key: string, open: boolean) => void;
  setTheme: (theme: Theme) => void;
};

export const useWorkspaceStore = create<WorkspaceState>()(
  persist(
    (set) => ({
      selectedCharacterId: null,
      selectedAnimationId: null,
      selectedFrameId: null,
      activeStep: "range",
      workspaceView: "global",
      toolBackground: "current",
      canvasViewports: {},
      disclosures: {},
      theme: "dark",
      selectCharacter: (selectedCharacterId) =>
        set((current) => selectedCharacterId === current.selectedCharacterId
          ? { selectedCharacterId, workspaceView: "global" }
          : {
              selectedCharacterId,
              selectedAnimationId: null,
              selectedFrameId: null,
              activeStep: "range",
              workspaceView: "global",
            }),
      selectAnimation: (selectedAnimationId) =>
        set({ selectedAnimationId, selectedFrameId: null, workspaceView: "animation" }),
      selectFrame: (selectedFrameId) => set({ selectedFrameId }),
      setActiveStep: (activeStep) => set({ activeStep }),
      showGlobalSettings: () => set({ workspaceView: "global" }),
      setToolBackground: (toolBackground) => set({ toolBackground }),
      setCanvasViewport: (key, update, fallback) =>
        set((current) => {
          const previous = current.canvasViewports[key] ?? fallback;
          const next = typeof update === "function" ? update(previous) : update;
          return {
            canvasViewports: {
              ...current.canvasViewports,
              [key]: next,
            },
          };
        }),
      setDisclosure: (key, open) =>
        set((current) => ({
          disclosures: {
            ...current.disclosures,
            [key]: open,
          },
        })),
      setTheme: (theme) => set({ theme }),
    }),
    {
      name: "rotoweave-workspace-location",
      version: 1,
      storage: createJSONStorage(() => window.localStorage),
      partialize: (state) => ({
        selectedCharacterId: state.selectedCharacterId,
        selectedAnimationId: state.selectedAnimationId,
        activeStep: state.activeStep,
        workspaceView: state.workspaceView,
        toolBackground: state.toolBackground,
        theme: state.theme,
      }),
    },
  ),
);
