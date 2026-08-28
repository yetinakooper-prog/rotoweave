import type { ComponentProps } from "react";

import { fromPercentDisplay, toPercentDisplay } from "../lib/percent-input-v4";
import { NumericDraftInput } from "./numeric-draft-input";

type NumericProps = ComponentProps<typeof NumericDraftInput>;
type PercentDraftInputV4Props = Omit<NumericProps, "value" | "minimum" | "maximum" | "onCommit"> & {
  value: number;
  minimum?: number;
  maximum?: number;
  onCommit: (value: number) => void;
};

export function PercentDraftInputV4({ value, minimum, maximum, onCommit, ...props }: PercentDraftInputV4Props) {
  return <span className="percent-draft-input-v4">
    <NumericDraftInput
      {...props}
      value={toPercentDisplay(value)}
      minimum={minimum === undefined ? undefined : toPercentDisplay(minimum)}
      maximum={maximum === undefined ? undefined : toPercentDisplay(maximum)}
      onCommit={(next) => onCommit(fromPercentDisplay(next))}
    />
    <span aria-hidden="true">%</span>
  </span>;
}
