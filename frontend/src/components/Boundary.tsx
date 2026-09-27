import { Ban, Headset, ShieldCheck } from "lucide-react";
import type { ReactNode } from "react";
import type { BoundaryClassification } from "../types";

type BoundaryMeta = {
  className: "auto" | "handoff" | "blocked";
  icon: ReactNode;
  label: string;
};

export function BoundaryBadge({
  boundary,
  compact = false
}: {
  boundary: BoundaryClassification;
  compact?: boolean;
}) {
  const meta = boundaryMeta(boundary);
  return (
    <span className={`boundary-badge ${meta.className} ${compact ? "compact" : ""}`}>
      {meta.icon}
      {meta.label}
    </span>
  );
}

export function BoundaryStatusCard({ boundary }: { boundary: BoundaryClassification }) {
  const meta = boundaryMeta(boundary);
  return (
    <article className={`boundary-card ${meta.className}`}>
      <div className="boundary-card-head">
        {meta.icon}
        <strong>{meta.label}</strong>
      </div>
      <p>{boundary.display_message}</p>
      <small>{boundary.reason}</small>
    </article>
  );
}

export function BoundaryStatusBar({ boundary }: { boundary: BoundaryClassification }) {
  const meta = boundaryMeta(boundary);
  return (
    <div className={`boundary-status-bar ${meta.className}`}>
      {meta.icon}
      <strong>{meta.label}</strong>
      {boundary.display_message && <span>{boundary.display_message}</span>}
    </div>
  );
}

export function boundaryMeta(boundary: BoundaryClassification): BoundaryMeta {
  if (boundary.classification === "human_handoff_required") {
    return {
      className: "handoff",
      icon: <Headset size={14} />,
      label: "人工接管"
    };
  }
  if (boundary.classification === "out_of_scope") {
    return {
      className: "blocked",
      icon: <Ban size={14} />,
      label: "拒答"
    };
  }
  if (boundary.classification === "security_refusal") {
    return {
      className: "blocked",
      icon: <ShieldCheck size={14} />,
      label: "安全拒绝"
    };
  }
  return {
    className: "auto",
    icon: <ShieldCheck size={14} />,
    label: "自动回答"
  };
}
