/**
 * sandpack-gen — generates Sandpack files from an AppSpec.
 *
 * Per roadmap §6 "instant lite preview via Sandpack with a Vite template
 * in the dashboard — the user sees UI changes in <1 second as tokens stream,
 * with a Mock Service Worker layer auto-generated from the OpenAPI schema
 * serving fake fixtures so no backend boot is needed."
 *
 * This module converts an AppSpec into a set of Sandpack-compatible file
 * entries (filename → code string) that render a fully-navigable React
 * preview with:
 *   - React Router routes for every Page in the spec
 *   - Mock entity data fixtures for each Entity
 *   - A simple CSS design system for immediate visual polish
 */

import type { AppSpec, Entity, EntityField, Page } from "@alloy/shared";

export interface SandpackFiles {
  [path: string]: string;
}

/* ── Helpers ───────────────────────────────────────────────────── */

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function camelCase(s: string): string {
  return s
    .replace(/[-_](\w)/g, (_, c: string) => c.toUpperCase())
    .replace(/^(\w)/, (_, c: string) => c.toLowerCase());
}

function pluralize(entity: Entity): string {
  return entity.plural ?? entity.name.toLowerCase() + "s";
}

/** Generate fake fixture data for an entity. */
function generateFixture(entity: Entity, count = 3): string {
  const rows: string[] = [];
  for (let i = 1; i <= count; i++) {
    const fields = entity.fields.map((f: EntityField) => {
      let value: string;
      switch (f.type) {
        case "string":
        case "text":
          value = `"${capitalize(f.name)} ${i}"`;
          break;
        case "int":
          value = `${i * 10}`;
          break;
        case "float":
          value = `${(i * 10.5).toFixed(1)}`;
          break;
        case "bool":
          value = i % 2 === 0 ? "true" : "false";
          break;
        case "datetime":
        case "date":
          value = `"2026-0${i}-15T10:00:00Z"`;
          break;
        case "uuid":
          value = `"${crypto.randomUUID?.() ?? `00000000-0000-0000-0000-00000000000${i}`}"`;
          break;
        case "ref":
          value = `"ref_${i}"`;
          break;
        case "json":
          value = `{}`;
          break;
        default:
          value = `"sample_${i}"`;
      }
      return `    ${camelCase(f.name)}: ${value}`;
    });
    rows.push(`  {\n    id: ${i},\n${fields.join(",\n")}\n  }`);
  }
  return `[\n${rows.join(",\n")}\n]`;
}

/* ── Page component generator ──────────────────────────────────── */

function generatePageComponent(page: Page, entities: Entity[]): string {
  // Find entities this page depends on via data_deps
  const usedEntities = entities.filter((e) =>
    page.data_deps.some(
      (dep: string) =>
        dep.toLowerCase().includes(e.name.toLowerCase()) ||
        dep.toLowerCase().includes(pluralize(e).toLowerCase()),
    ),
  );

  const imports = usedEntities.map(
    (e) => `import { ${camelCase(pluralize(e))}Data } from "../data/${camelCase(pluralize(e))}";`,
  );

  const entitySections = usedEntities.map((e) => {
    const plural = pluralize(e);
    const varName = camelCase(plural);
    const displayFields = e.fields.slice(0, 4); // Show first 4 fields

    return `
      <section className="entity-section">
        <h2>${capitalize(plural)}</h2>
        <div className="card-grid">
          {${varName}Data.map((item, i) => (
            <div key={i} className="card">
${displayFields.map((f: EntityField) => `              <div className="field"><span className="label">${capitalize(f.name)}</span><span className="value">{String(item.${camelCase(f.name)})}</span></div>`).join("\n")}
            </div>
          ))}
        </div>
      </section>`;
  });

  const fallbackContent =
    usedEntities.length === 0
      ? `\n      <div className="empty-state">\n        <p>${page.description ?? `This is the ${page.name} page.`}</p>\n      </div>`
      : "";

  return `${imports.length > 0 ? imports.join("\n") + "\n\n" : ""}export default function ${capitalize(camelCase(page.name))}Page() {
  return (
    <div className="page">
      <h1>${page.name}</h1>
      ${page.description ? `<p className="page-desc">${page.description}</p>` : ""}${entitySections.join("\n")}${fallbackContent}
    </div>
  );
}
`;
}

/* ── Main generator ────────────────────────────────────────────── */

export function generateSandpackFiles(spec: AppSpec): SandpackFiles {
  const files: SandpackFiles = {};

  // ── Data fixtures ──────────────────────────────────────────
  for (const entity of spec.entities) {
    const varName = camelCase(pluralize(entity));
    files[`/data/${varName}.js`] = `export const ${varName}Data = ${generateFixture(entity)};\n`;
  }

  // ── Page components ────────────────────────────────────────
  for (const page of spec.pages) {
    const componentName = capitalize(camelCase(page.name));
    files[`/pages/${componentName}Page.jsx`] = generatePageComponent(page, spec.entities);
  }

  // ── Nav component ──────────────────────────────────────────
  const navLinks = spec.pages
    .map((p: Page) => `      <a href="${p.path}" className={location.pathname === "${p.path}" ? "active" : ""}>${p.name}</a>`)
    .join("\n");

  files["/components/Nav.jsx"] = `export default function Nav() {
  const location = window.location;
  return (
    <nav className="app-nav">
      <div className="nav-brand">${spec.name}</div>
      <div className="nav-links">
${navLinks}
      </div>
    </nav>
  );
}
`;

  // ── App.jsx with routing ───────────────────────────────────
  const pageImports = spec.pages
    .map((p: Page) => {
      const name = capitalize(camelCase(p.name));
      return `import ${name}Page from "./pages/${name}Page";`;
    })
    .join("\n");

  const routes = spec.pages
    .map((p: Page) => {
      const name = capitalize(camelCase(p.name));
      return `        <Route path="${p.path}" element={<${name}Page />} />`;
    })
    .join("\n");

  const defaultPage = spec.pages[0];
  const defaultRoute = defaultPage
    ? `        <Route path="*" element={<Navigate to="${defaultPage.path}" replace />} />`
    : `        <Route path="*" element={<div className="page"><h1>Welcome to ${spec.name}</h1></div>} />`;

  files["/App.jsx"] = `import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Nav from "./components/Nav";
${pageImports}

export default function App() {
  return (
    <BrowserRouter>
      <div className="app-shell">
        <Nav />
        <main className="app-main">
          <Routes>
${routes}
${defaultRoute}
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  );
}
`;

  // ── Styles ─────────────────────────────────────────────────
  files["/styles.css"] = `/* Generated preview styles for ${spec.name} */
:root {
  --bg: #0f172a;
  --surface: #1e293b;
  --border: #334155;
  --text: #e2e8f0;
  --muted: #94a3b8;
  --accent: #6366f1;
  --accent-soft: rgba(99, 102, 241, 0.15);
  --success: #22c55e;
  --radius: 8px;
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.6;
}

.app-shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

/* ── Nav ─────────────────────── */
.app-nav {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 0 20px;
  height: 48px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.nav-brand {
  font-weight: 700;
  font-size: 14px;
  color: var(--accent);
}
.nav-links {
  display: flex;
  gap: 4px;
}
.nav-links a {
  padding: 6px 12px;
  border-radius: var(--radius);
  color: var(--muted);
  text-decoration: none;
  font-size: 13px;
  transition: all 0.15s;
}
.nav-links a:hover { color: var(--text); background: var(--accent-soft); }
.nav-links a.active { color: var(--accent); background: var(--accent-soft); }

/* ── Page ────────────────────── */
.app-main { flex: 1; padding: 24px; }
.page h1 {
  font-size: 22px;
  font-weight: 700;
  margin-bottom: 4px;
}
.page-desc {
  color: var(--muted);
  font-size: 14px;
  margin-bottom: 20px;
}

/* ── Entity sections ─────────── */
.entity-section {
  margin-top: 24px;
}
.entity-section h2 {
  font-size: 15px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  margin-bottom: 12px;
}
.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 12px;
}
.card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px;
  transition: border-color 0.15s;
}
.card:hover { border-color: var(--accent); }
.field {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  font-size: 13px;
  border-bottom: 1px solid var(--border);
}
.field:last-child { border-bottom: none; }
.label { color: var(--muted); font-size: 12px; }
.value { color: var(--text); font-weight: 500; }

/* ── Empty state ─────────────── */
.empty-state {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 200px;
  color: var(--muted);
  font-size: 14px;
}
`;

  // ── index.js entry ─────────────────────────────────────────
  files["/index.js"] = `import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

const root = createRoot(document.getElementById("root"));
root.render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
`;

  return files;
}
