/**
 * Build page — the full IDE view.
 *
 * If a `projectId` param is present, loads the existing project.
 * Otherwise starts fresh with the chat prompt.
 */

import { useParams } from "react-router-dom";
import { IDELayout } from "@/components/ide/IDELayout";

export function Build() {
  const { projectId } = useParams<{ projectId?: string }>();

  return <IDELayout initialProjectId={projectId} />;
}
