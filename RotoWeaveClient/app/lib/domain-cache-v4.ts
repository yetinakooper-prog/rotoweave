import type { QueryClient } from "@tanstack/react-query";

import type { DomainActionV4, DomainCharacterV4, WorkspaceDomainV4 } from "./types";

export const DOMAIN_V4_QUERY_KEY = ["domain-v4"] as const;

function replaceById<T extends { id: string }>(items: T[], entity: T): T[] | null {
  const index = items.findIndex((item) => item.id === entity.id);
  if (index < 0) return null;
  const next = [...items];
  next[index] = entity;
  return next;
}

export function mergeDomainActionV4(
  queryClient: QueryClient,
  action: DomainActionV4,
  revisionId: string,
): boolean {
  let merged = false;
  queryClient.setQueryData<WorkspaceDomainV4>(DOMAIN_V4_QUERY_KEY, (current) => {
    if (!current) return current;
    const actions = replaceById(current.actions, action);
    if (!actions) return current;
    merged = true;
    return { ...current, revisionId, actions };
  });
  return merged;
}

export function mergeDomainCharacterV4(
  queryClient: QueryClient,
  character: DomainCharacterV4,
  revisionId: string,
): boolean {
  let merged = false;
  queryClient.setQueryData<WorkspaceDomainV4>(DOMAIN_V4_QUERY_KEY, (current) => {
    if (!current) return current;
    const characters = replaceById(current.characters, character);
    if (!characters) return current;
    merged = true;
    return { ...current, revisionId, characters };
  });
  return merged;
}
