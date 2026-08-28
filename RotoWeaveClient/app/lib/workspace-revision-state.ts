const DOMAIN_RESOURCE = "workspace:domain";
const SIZE_PROFILE_RESOURCE = "global:size-profiles";

/** Keeps the two current Format 3 aggregate revisions monotonic. */
export class WorkspaceRevisionState {
  private readonly revisionByResource = new Map<string, string>();
  private readonly revisionOrderByResource = new Map<string, number>();
  private observationClock = 0;
  private minimumObservation = 0;

  beginObservation(): number {
    this.observationClock += 1;
    return this.observationClock;
  }

  reset(): void {
    this.minimumObservation = this.beginObservation();
    this.revisionByResource.clear();
    this.revisionOrderByResource.clear();
  }

  remember(value: unknown, observation: number): void {
    if (observation < this.minimumObservation) return;
    if (Array.isArray(value)) {
      value.forEach((item) => this.remember(item, observation));
      return;
    }
    if (!value || typeof value !== "object") return;
    const record = value as Record<string, unknown>;
    const revisionId = typeof record.revisionId === "string" ? record.revisionId : null;
    if (revisionId && Array.isArray(record.profiles)) {
      this.setRevision(SIZE_PROFILE_RESOURCE, revisionId, observation, false);
    }
    const isDomain =
      record.workspaceFormatVersion === 3 &&
      record.domainRevision === 7;
    const isDomainMutation =
      revisionId !== null &&
      ["character", "action", "removed", "domain", "variant", "exportState"].some(
        (key) => key in record,
      );
    if (revisionId && (isDomain || isDomainMutation)) {
      this.setRevision(DOMAIN_RESOURCE, revisionId, observation, false);
    }
    Object.values(record).forEach((item) => this.remember(item, observation));
  }

  revisionForMutation(path: string): string | null {
    const first = path.split("/").filter(Boolean)[0] ?? "";
    if (first === "size-profiles") {
      return this.revisionByResource.get(SIZE_PROFILE_RESOURCE) ?? null;
    }
    if (["domain", "material-sources", "material-variants", "exports"].includes(first)) {
      return this.revisionByResource.get(DOMAIN_RESOURCE) ?? null;
    }
    return null;
  }

  applyMutationRevision(path: string, revisionId: string, observation: number): void {
    if (observation < this.minimumObservation) return;
    const first = path.split("/").filter(Boolean)[0] ?? "";
    if (first === "size-profiles") {
      this.setRevision(SIZE_PROFILE_RESOURCE, revisionId, observation, true);
    } else if (["domain", "material-sources", "material-variants", "exports"].includes(first)) {
      this.setRevision(DOMAIN_RESOURCE, revisionId, observation, true);
    }
  }

  private setRevision(
    resource: string,
    revisionId: string,
    observation: number,
    authoritative: boolean,
  ): void {
    const order = observation * 2 + (authoritative ? 1 : 0);
    if (order < (this.revisionOrderByResource.get(resource) ?? -1)) return;
    this.revisionOrderByResource.set(resource, order);
    if (revisionId === "deleted") this.revisionByResource.delete(resource);
    else this.revisionByResource.set(resource, revisionId);
  }
}

export const workspaceRevisionState = new WorkspaceRevisionState();
