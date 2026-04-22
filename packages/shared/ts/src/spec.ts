// Mirrors packages/shared/python/alloy_shared/spec.py.
// A CI check in Phase 1 round-trips sample payloads through both sides.

export type AuthProvider = "fastapi_users_jwt" | "custom_jwt" | "clerk";

export interface AuthConfig {
  provider: AuthProvider;
  allow_signup: boolean;
  require_email_verify: boolean;
}

export type FieldType =
  | "string"
  | "text"
  | "int"
  | "float"
  | "bool"
  | "datetime"
  | "date"
  | "uuid"
  | "json"
  | "ref";

export interface EntityField {
  name: string;
  type: FieldType;
  required: boolean;
  unique: boolean;
  indexed: boolean;
  ref?: string | null;
}

export interface Entity {
  name: string;
  plural?: string | null;
  fields: EntityField[];
  auditable: boolean;
}

export type RoutePermission = "public" | "authenticated" | "owner" | "admin";
export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

export interface Route {
  method: HttpMethod;
  path: string;
  handler_name: string;
  permission: RoutePermission;
  description?: string | null;
}

export interface Page {
  name: string;
  path: string;
  description?: string | null;
  data_deps: string[];
}

export type IntegrationKind =
  | "stripe"
  | "r2"
  | "resend"
  | "clerk"
  | "vercel"
  | "github"
  | "daytona";

export interface Integration {
  kind: IntegrationKind;
}

export interface AppSpec {
  name: string;
  slug: string;
  description: string;
  auth: AuthConfig;
  entities: Entity[];
  routes: Route[];
  pages: Page[];
  integrations: Integration[];
  schema_version: 1;
}
