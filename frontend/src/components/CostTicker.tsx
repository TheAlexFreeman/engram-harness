import { useEffect, useRef, useState } from "react";

interface Props {
  totalCostUsd: number;
}

export function CostTicker({ totalCostUsd }: Props) {
  const [display, setDisplay] = useState(totalCostUsd);
  const [ticking, setTicking] = useState(false);
  const prevRef = useRef(totalCostUsd);

  useEffect(() => {
    if (totalCostUsd !== prevRef.current) {
      prevRef.current = totalCostUsd;
      setDisplay(totalCostUsd);
      setTicking(true);
      const t = setTimeout(() => setTicking(false), 400);
      return () => clearTimeout(t);
    }
  }, [totalCostUsd]);

  const color =
    display < 0.05
      ? "text-green-400"
      : display < 0.25
      ? "text-yellow-400"
      : "text-red-400";

  return (
    <span className={`font-mono font-bold ${color} ${ticking ? "cost-tick" : ""}`}>
      ${display.toFixed(4)}
    </span>
  );
}
