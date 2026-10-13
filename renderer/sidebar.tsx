import type { ReactNode } from 'react';

/** Shared, fixed sidebar heading. Scrolling belongs to the adjacent sidebar-scroll body. */
export function SidebarHeader({ title, actions }: { title: string; actions?: ReactNode }) {
  return <header className="sidebar-heading"><h2 title={title}>{title}</h2>{actions && <div className="sidebar-heading-actions">{actions}</div>}</header>;
}
