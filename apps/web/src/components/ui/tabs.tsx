import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/cn";

/* ── Tab bar container ─────────────────────────────────────────── */

interface TabBarProps {
  children: ReactNode;
  className?: string;
}

export function TabBar({ children, className }: TabBarProps) {
  return (
    <div
      className={cn(
        "flex h-[var(--tab-height)] items-end gap-0 overflow-x-auto border-b border-border bg-background",
        className,
      )}
    >
      {children}
    </div>
  );
}

/* ── Single tab ────────────────────────────────────────────────── */

interface TabProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
  /** Icon rendered before the label. */
  icon?: ReactNode;
  /** Close button callback — when provided a close × is shown. */
  onClose?: (e: React.MouseEvent) => void;
}

export const Tab = forwardRef<HTMLButtonElement, TabProps>(
  ({ active, icon, onClose, className, children, ...props }, ref) => (
    <button
      ref={ref}
      type="button"
      className={cn(
        "group relative flex h-full items-center gap-1.5 px-3 text-xs transition-colors",
        "border-r border-border",
        active
          ? "bg-muted text-foreground"
          : "text-muted-foreground hover:bg-muted/50 hover:text-foreground",
        className,
      )}
      {...props}
    >
      {/* Active indicator line */}
      {active && (
        <span className="absolute inset-x-0 top-0 h-[2px] bg-accent" />
      )}
      {icon && <span className="flex-shrink-0">{icon}</span>}
      <span className="max-w-[140px] truncate">{children}</span>
      {onClose && (
        <span
          role="button"
          tabIndex={-1}
          onClick={(e) => {
            e.stopPropagation();
            onClose(e);
          }}
          className={cn(
            "ml-1 flex h-4 w-4 flex-shrink-0 items-center justify-center rounded-sm",
            "opacity-0 transition-opacity group-hover:opacity-100",
            "hover:bg-foreground/10",
          )}
        >
          ×
        </span>
      )}
    </button>
  ),
);
Tab.displayName = "Tab";
