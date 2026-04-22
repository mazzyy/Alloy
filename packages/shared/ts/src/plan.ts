// Mirrors packages/shared/python/alloy_shared/plan.py.

export type FileOpKind = "create" | "modify" | "delete" | "move";

export interface FileOp {
  kind: FileOpKind;
  path: string;
  intent: string;
  depends_on: string[];
  id: string;
}

export interface BuildPlan {
  spec_slug: string;
  base_template: "react-fastapi";
  blocks: string[];
  ops: FileOp[];
  schema_version: 1;
}
