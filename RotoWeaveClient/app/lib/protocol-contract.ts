import productContract from "@rotoweave/contracts/product.json";
import protocols from "@rotoweave/contracts/protocols.json";

export class IncompatibleProtocolError extends Error {
  constructor(
    readonly contract: string,
    readonly expected: string | number,
    readonly received: unknown,
  ) {
    super(contract + " 版本不兼容：仅支持 " + String(expected) + "，收到 " + String(received) + "。");
    this.name = "IncompatibleProtocolError";
  }
}

export function requireProtocolVersion(
  contract: string,
  received: unknown,
  expected: string | number,
): void {
  if (received !== expected) {
    throw new IncompatibleProtocolError(contract, expected, received);
  }
}

requireProtocolVersion("产品", protocols.productVersion, "4.0.0");
requireProtocolVersion("本地 API", protocols.localApi.version, productContract.contracts.httpApi);
requireProtocolVersion("远程抠图 API", protocols.remoteMattingApi.version, productContract.contracts.remoteMattingApi);
requireProtocolVersion("本地 API 前缀", protocols.localApi.prefix, "/api/v4");
requireProtocolVersion("远程抠图 API 前缀", protocols.remoteMattingApi.prefix, "/api/matting/v1");

export const LOCAL_API_PREFIX = protocols.localApi.prefix;
export const REMOTE_MATTING_API_PREFIX = protocols.remoteMattingApi.prefix;
export const REMOTE_MATTING_API_VERSION = protocols.remoteMattingApi.version;
export const CANONICAL_PIXELS_PER_UNIT = productContract.contracts.canonicalPixelsPerUnit;
