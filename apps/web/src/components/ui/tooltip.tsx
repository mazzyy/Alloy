import { type ReactNode, useState, useRef, useEffect, useCallback } from "react";
import { cn } from "@/lib/cn";

interface TooltipProps {
  content: string;
  children: ReactNode;
  side?: "top" | "bottom" | "left" | "right";
  className?: string;
}

export function Tooltip({ content, children, side = "bottom", className }: TooltipProps) {
  const [show, setShow] = useState(false);
  const timeout = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);

  const handleEnter = useCallback(() => {
    timeout.current = setTimeout(() => setShow(true), 400);
  }, []);

  const handleLeave = useCallback(() => {
    clearTimeout(timeout.current);
    setShow(false);
  }, []);

  useEffect(() => () => clearTimeout(timeout.current), []);

  const positionClasses: Record<string, string> = {
    top: "bottom-full left-1/2 -translate-x-1/2 mb-1.5",
    bottom: "top-full left-1/2 -translate-x-1/2 mt-1.5",
    left: "right-full top-1/2 -translate-y-1/2 mr-1.5",
    right: "left-full top-1/2 -translate-y-1/2 ml-1.5",
  };

  return (
    <div
      className={cn("relative inline-flex", className)}
      onMouseEnter={handleEnter}
      onMouseLeave={handleLeave}
    >
      {children}
      {show && (
        <div
          role="tooltip"
          className={cn(
            "pointer-events-none absolute z-50 whitespace-nowrap rounded-md",
            "border border-border bg-card px-2 py-1 text-[11px] text-foreground shadow-md",
            "animate-in fade-in-0 zoom-in-95 duration-150",
            positionClasses[side],
          )}
        >
          {content}
        </div>
      )}
    </div>
  );
}
