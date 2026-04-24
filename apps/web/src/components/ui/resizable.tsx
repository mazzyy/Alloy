import {
  useState,
  useCallback,
  useEffect,
  useRef,
  type ReactNode,
  type CSSProperties,
} from "react";
import { cn } from "@/lib/cn";

type Direction = "horizontal" | "vertical";

interface ResizableProps {
  /** The two children: [first panel, second panel]. */
  children: [ReactNode, ReactNode];
  direction?: Direction;
  /** Initial size of the first panel in px. */
  defaultSize?: number;
  /** Min size of the first panel in px. */
  minSize?: number;
  /** Max size of the first panel in px. */
  maxSize?: number;
  className?: string;
  /** Called continuously while resizing. */
  onResize?: (size: number) => void;
}

export function Resizable({
  children,
  direction = "horizontal",
  defaultSize = 260,
  minSize = 120,
  maxSize = 600,
  className,
  onResize,
}: ResizableProps) {
  const [size, setSize] = useState(defaultSize);
  const dragging = useRef(false);
  const startPos = useRef(0);
  const startSize = useRef(0);

  const isHoriz = direction === "horizontal";

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      dragging.current = true;
      startPos.current = isHoriz ? e.clientX : e.clientY;
      startSize.current = size;
      document.body.style.cursor = isHoriz ? "col-resize" : "row-resize";
      document.body.style.userSelect = "none";
    },
    [isHoriz, size],
  );

  useEffect(() => {
    function handleMouseMove(e: MouseEvent) {
      if (!dragging.current) return;
      const delta = (isHoriz ? e.clientX : e.clientY) - startPos.current;
      const newSize = Math.max(minSize, Math.min(maxSize, startSize.current + delta));
      setSize(newSize);
      onResize?.(newSize);
    }
    function handleMouseUp() {
      if (!dragging.current) return;
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [isHoriz, minSize, maxSize, onResize]);

  const containerStyle: CSSProperties = isHoriz
    ? { display: "flex", flexDirection: "row" }
    : { display: "flex", flexDirection: "column" };

  const firstStyle: CSSProperties = isHoriz
    ? { width: size, minWidth: minSize, maxWidth: maxSize, flexShrink: 0 }
    : { height: size, minHeight: minSize, maxHeight: maxSize, flexShrink: 0 };

  return (
    <div className={cn("h-full w-full", className)} style={containerStyle}>
      <div style={firstStyle} className="overflow-hidden">
        {children[0]}
      </div>
      <div
        role="separator"
        onMouseDown={handleMouseDown}
        className={cn(
          "relative z-10 flex-shrink-0 transition-colors",
          isHoriz
            ? "w-[3px] cursor-col-resize hover:bg-accent/40 active:bg-accent/60"
            : "h-[3px] cursor-row-resize hover:bg-accent/40 active:bg-accent/60",
        )}
      />
      <div className="min-h-0 min-w-0 flex-1 overflow-hidden">{children[1]}</div>
    </div>
  );
}
