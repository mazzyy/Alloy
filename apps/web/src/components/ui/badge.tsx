import { forwardRef, type HTMLAttributes } from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

const badgeVariants = cva(
  "inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider transition-colors",
  {
    variants: {
      variant: {
        default: "border border-border bg-muted text-muted-foreground",
        accent: "bg-accent/15 text-accent border border-accent/20",
        success: "bg-success/15 text-success border border-success/20",
        warning: "bg-warning/15 text-warning border border-warning/20",
        destructive: "bg-destructive/15 text-destructive border border-destructive/20",
        info: "bg-info/15 text-info border border-info/20",
      },
    },
    defaultVariants: { variant: "default" },
  },
);

type BadgeProps = HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants>;

export const Badge = forwardRef<HTMLSpanElement, BadgeProps>(
  ({ className, variant, ...props }, ref) => (
    <span ref={ref} className={cn(badgeVariants({ variant }), className)} {...props} />
  ),
);
Badge.displayName = "Badge";
